"""Local web app: drop papers in, review the claims that come back out."""

from __future__ import annotations

import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from . import bib, config, export, extract, ingest, store

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Doxograph")

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="doxograph")
_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0


def _new_job(label: str) -> dict:
    global _job_counter
    with _jobs_lock:
        _job_counter += 1
        job = {"id": _job_counter, "label": label, "state": "queued",
               "detail": "", "key": None, "started": store.now()}
        _jobs[job["id"]] = job
        return job


def _set(job: dict, **fields) -> None:
    with _jobs_lock:
        job.update(fields)


def _prune_jobs() -> None:
    with _jobs_lock:
        done = [j for j in _jobs.values() if j["state"] in ("done", "error")]
        for job in sorted(done, key=lambda j: j["id"])[:-40]:
            _jobs.pop(job["id"], None)


# --- background work ------------------------------------------------------

def _run_ingest(job: dict, ref: ingest.Ref, do_extract: bool) -> None:
    try:
        _set(job, state="fetching")
        key, created = ingest.ingest_ref(ref)
        _set(job, key=key, label=key, detail="" if created else "already in the corpus")
        if not created:
            _set(job, state="done")
            return
        if do_extract:
            _set(job, state="reading")
            extract.extract_paper(key)
        _set(job, state="done")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


def _run_upload(job: dict, data: bytes, filename: str, do_extract: bool) -> None:
    try:
        _set(job, state="fetching")
        key, created = ingest.ingest_pdf_bytes(data, filename)
        _set(job, key=key, label=key, detail="" if created else "already in the corpus")
        if created and do_extract:
            _set(job, state="reading")
            extract.extract_paper(key)
        _set(job, state="done")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


def _run_extract(job: dict, key: str, keep_reviewed: bool) -> None:
    try:
        _set(job, state="reading", key=key)
        extract.extract_paper(key, keep_reviewed=keep_reviewed)
        _set(job, state="done")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


def _run_retag(job: dict, keys: list[str]) -> None:
    try:
        for index, key in enumerate(keys, 1):
            _set(job, state="reading", key=key, detail=f"{index} of {len(keys)}")
            extract.retag_paper(key)
        _set(job, state="done", detail=f"{len(keys)} papers")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


# --- request bodies -------------------------------------------------------

class IngestBody(BaseModel):
    text: str = ""
    extract: bool = True


class TagBody(BaseModel):
    name: str
    description: str = ""


class RenameBody(BaseModel):
    name: str


class ProposedTagsBody(BaseModel):
    accept: list[str] = []
    discard: list[str] = []


class RetagBody(BaseModel):
    keys: list[str] | None = None


class ExportBody(BaseModel):
    path: str | None = None
    title: str = "Doxograph"


# --- routes ---------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/app.css", response_class=PlainTextResponse)
def app_css() -> PlainTextResponse:
    return PlainTextResponse((STATIC / "app.css").read_text(encoding="utf-8"), media_type="text/css")


@app.get("/app.js", response_class=PlainTextResponse)
def app_js() -> PlainTextResponse:
    return PlainTextResponse(
        (STATIC / "app.js").read_text(encoding="utf-8"), media_type="application/javascript"
    )


@app.get("/api/state")
def state() -> dict:
    papers = store.all_papers()
    rows = store.claim_rows(papers)
    with _jobs_lock:
        jobs = sorted(_jobs.values(), key=lambda j: j["id"], reverse=True)[:20]
    return {
        "papers": [store.summarize(p) for p in papers],
        "claims": rows,
        "tags": store.load_tags(),
        "tag_counts": store.tag_counts(rows),
        "ledger": store.load_ledger(),
        "kinds": config.CLAIM_KINDS,
        "strengths": config.CLAIM_STRENGTHS,
        "relations": config.LEDGER_RELATIONS,
        "jobs": jobs,
        "data_dir": str(config.data_dir()),
        "model": config.MODEL,
        "has_key": config.api_key() is not None,
    }


@app.post("/api/ingest")
def api_ingest(body: IngestBody) -> dict:
    refs, unknown = ingest.parse_refs(body.text)
    for ref in refs:
        job = _new_job(ref.value)
        _pool.submit(_run_ingest, job, ref, body.extract)
    return {"queued": len(refs), "unknown": unknown}


@app.post("/api/upload")
async def api_upload(files: list[UploadFile], extract_now: bool = True) -> dict:
    queued = 0
    for upload in files:
        data = await upload.read()
        job = _new_job(upload.filename or "upload.pdf")
        _pool.submit(_run_upload, job, data, upload.filename or "upload.pdf", extract_now)
        queued += 1
    return {"queued": queued}


@app.get("/api/papers/{key}")
def get_paper(key: str) -> dict:
    try:
        return store.load_paper(key)
    except KeyError:
        raise HTTPException(404, f"no paper {key}")


PAPER_FIELDS = {"title", "authors", "year", "venue", "doi", "summary", "relevance", "notes"}


@app.patch("/api/papers/{key}")
def patch_paper(key: str, patch: dict = Body(...)) -> dict:
    with store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except KeyError:
            raise HTTPException(404, f"no paper {key}")
        for field, value in patch.items():
            if field in PAPER_FIELDS:
                paper[field] = value
        store.save_paper(paper)
    return paper


@app.delete("/api/papers/{key}")
def remove_paper(key: str) -> dict:
    store.delete_paper(key)
    return {"deleted": key}


@app.post("/api/papers/{key}/extract")
def reextract(key: str, keep_reviewed: bool = True) -> dict:
    if not store.paper_path(key).exists():
        raise HTTPException(404, f"no paper {key}")
    job = _new_job(key)
    _pool.submit(_run_extract, job, key, keep_reviewed)
    return {"queued": job["id"]}


@app.post("/api/retag")
def retag(body: RetagBody) -> dict:
    keys = body.keys or [p["key"] for p in store.all_papers() if p.get("claims")]
    if not keys:
        return {"queued": 0}
    job = _new_job(f"retag {len(keys)} papers")
    _pool.submit(_run_retag, job, keys)
    return {"queued": len(keys)}


@app.post("/api/papers/{key}/claims")
def create_claim(key: str, patch: dict = Body(default={})) -> dict:
    try:
        return store.add_claim(key, patch)
    except KeyError:
        raise HTTPException(404, f"no paper {key}")


@app.patch("/api/papers/{key}/claims/{claim_id}")
def patch_claim(key: str, claim_id: str, patch: dict = Body(...)) -> dict:
    try:
        return store.update_claim(key, claim_id, patch)
    except KeyError:
        raise HTTPException(404, f"no claim {claim_id} on {key}")


@app.delete("/api/papers/{key}/claims/{claim_id}")
def remove_claim(key: str, claim_id: str) -> dict:
    try:
        store.delete_claim(key, claim_id)
    except KeyError:
        raise HTTPException(404, f"no claim {claim_id} on {key}")
    return {"deleted": claim_id}


@app.post("/api/papers/{key}/proposed-tags")
def resolve_proposed_tags(key: str, body: ProposedTagsBody) -> dict:
    """Accept proposed topics into the vocabulary, or discard them.

    Discarding only clears the proposal — a discarded name must not end up in
    the vocabulary, which is the whole point of proposals being separate.
    """
    with store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except KeyError:
            raise HTTPException(404, f"no paper {key}")
        proposed = {t["name"]: t.get("description", "") for t in paper.get("proposed_tags", [])}
        accepted = [name for name in body.accept if name in proposed]
        for name in accepted:
            store.add_tag(name, proposed[name])
        resolved = set(accepted) | {name for name in body.discard if name in proposed}
        paper["proposed_tags"] = [t for t in paper.get("proposed_tags", []) if t["name"] not in resolved]
        store.save_paper(paper)
    return {
        "accepted": accepted,
        "discarded": sorted(resolved - set(accepted)),
        "tags": store.load_tags(),
    }


@app.post("/api/tags")
def create_tag(body: TagBody) -> dict:
    return {"tags": store.add_tag(body.name, body.description)}


@app.patch("/api/tags/{name}")
def patch_tag(name: str, body: RenameBody) -> dict:
    store.rename_tag(name, body.name)
    return {"tags": store.load_tags()}


@app.delete("/api/tags/{name}")
def remove_tag(name: str) -> dict:
    store.delete_tag(name)
    return {"tags": store.load_tags()}


@app.post("/api/export")
def api_export(body: ExportBody) -> dict:
    path = export.write(Path(body.path).expanduser() if body.path else None, title=body.title)
    return {"path": str(path)}


@app.get("/api/bibtex", response_class=PlainTextResponse)
def api_bibtex() -> PlainTextResponse:
    return PlainTextResponse(bib.render(), media_type="text/plain")


@app.get("/pdf/{key}")
def serve_pdf(key: str) -> FileResponse:
    path = store.pdf_path(key)
    if not path.exists():
        raise HTTPException(404, f"no PDF for {key}")
    return FileResponse(path, media_type="application/pdf", filename=f"{key}.pdf")


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    config.ensure_dirs()
    uvicorn.run("doxograph.server:app" if reload else app, host=host, port=port, reload=reload)
