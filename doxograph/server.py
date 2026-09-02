"""Local web app: drop papers in, review the claims that come back out."""

from __future__ import annotations

import json
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import __version__, bib, config, export, extract, ingest, store

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Doxograph")

_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="doxograph")
_jobs: dict[int, dict] = {}
_jobs_lock = threading.Lock()
_job_counter = 0

#: Methods that cannot make work, and so are not counted. Everything else is.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

_arrivals_lock = threading.Lock()
_requests_arriving = 0


class CountArrivingRequests:
    """Counts a request that might become work from the moment it lands.

    A handler cannot count this for itself. `api_upload`'s
    `files: list[UploadFile]` parameter means Starlette has received and parsed
    the whole multipart body before the handler runs, and that receive is
    exactly the window nobody can see: the job does not exist yet, so
    `/api/health` answers `busy: 0` while a paper is still arriving, and the
    macOS launcher stops the server it started on top of it. The same hole,
    narrower, is in front of every other handler — a JSON body is still a body
    that has to arrive, and `/api/ingest` posted by the page can be receiving or
    parsing when the user quits. The app keeps its own count of the papers it is
    sending, but only for the drops it makes itself; anything the page posts
    goes straight to the server and never passes through it.

    Counting here covers every client: the page inside the app, the page in a
    browser, curl, anything later. The two counts overlap rather than leaving a
    seam, since this one starts before the app's can end and outlasts the job's
    creation.

    The net is cast by method rather than by a list of routes, and that is the
    point. "Routes that can create work" is a list that goes stale the first
    time someone adds one — upload, ingest, extract and retag today, whatever
    comes next tomorrow — and the failure is silent: the new route simply is not
    counted, and a quit lands on top of it. A method is a property of the
    request itself, so the rule stays true as the route table grows. It costs
    nothing to be broad, either. The number's only job is to be nonzero while
    the server is holding something that might become work, and a small JSON
    POST is inside this count for microseconds — far too briefly for a quit to
    catch it and ask a question about nothing.

    Reads are what is left out, and they have to be: `/api/health` is itself a
    GET, so counting reads would have it report itself as busy, and the page
    polls `/api/state` twice a second.

    Written against ASGI rather than as an `@app.middleware("http")` function so
    that it does not pump every other response — a whole paper, on `/pdf/{key}`
    — through an extra stream to do the counting.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "GET") in SAFE_METHODS:
            return await self.app(scope, receive, send)
        global _requests_arriving
        with _arrivals_lock:
            _requests_arriving += 1
        try:
            await self.app(scope, receive, send)
        finally:
            with _arrivals_lock:
                _requests_arriving -= 1


app.add_middleware(CountArrivingRequests)


def _finish(job: dict, key: str) -> None:
    """Close a job, saying so when the paper arrived without its PDF.

    A paper with no PDF cannot be read, and marking the job plainly done hides
    that: for a new paper the job leaves the strip entirely because its detail is
    empty. The CLI already counts this as a failure; the web UI now says it too.
    """
    if key and not store.pdf_path(key).exists():
        try:
            note = store.load_paper(key).get("notes") or "no PDF available"
        except (KeyError, json.JSONDecodeError):
            note = "the paper was removed"
        _set(job, state="error", detail=f"no PDF stored ({note}). Add it again to retry.")
        return
    _set(job, state="done")


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
        recovered = not created and store.needs_extraction(key)
        _set(job, key=key, label=key,
             detail="" if created else ("PDF recovered" if recovered else "already in the corpus"))
        # Read whenever the paper has a PDF and no claims, rather than only when
        # it was just created: a recovered download needs reading too.
        if do_extract and store.needs_extraction(key):
            _set(job, state="reading")
            extract.extract_paper(key)
        _finish(job, key)
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


def _run_upload(job: dict, staged: Path, filename: str, do_extract: bool) -> None:
    try:
        _set(job, state="fetching")
        key, created = ingest.ingest_staged_pdf(staged, filename)
        _set(job, key=key, label=key, detail="" if created else "already in the corpus")
        if do_extract and store.needs_extraction(key):
            _set(job, state="reading")
            extract.extract_paper(key)
        _finish(job, key)
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


def _run_tensions(job: dict, topics: list[str]) -> None:
    try:
        added = reopened = failed = 0
        last_failure = ""
        for index, topic in enumerate(topics, 1):
            _set(job, state="reading", detail=f"{index} of {len(topics)}: {topic}")
            # One topic's refusal or bad answer must not cost the topics after
            # it: they run in a fixed order, so an early topic that always
            # fails would keep the later ones from ever being read. Go on, as
            # the command line does, and say how many did not finish.
            try:
                result = extract.find_tensions(topic)
            except Exception as exc:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exc()
                continue
            added += result["added"]
            reopened += result["reopened"]
        summary = f"{added} new" + (f", {reopened} reopened" if reopened else "")
        if failed:
            _set(job, state="error",
                 detail=f"{failed} of {len(topics)} topics failed, {summary}; {last_failure}")
        else:
            _set(job, state="done", detail=f"{len(topics)} topics, {summary}")
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


class TensionsBody(BaseModel):
    topics: list[str] | None = None


class TensionStatusBody(BaseModel):
    status: str


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


ACTIVE_JOB_STATES = ("queued", "fetching", "reading")


@app.get("/api/health")
def health() -> dict:
    """Say the server is up, and whether it is in the middle of something.

    The macOS launcher polls this while it waits for uvicorn to bind, and asks
    it again on quit to find out whether anything would be lost. `/api/state`
    answers both questions but loads the whole corpus to do it, which is the
    wrong price for a readiness probe.

    A request that could make work counts from the moment it arrives, before it
    is a job at all. For the sliver between the job being made and the response
    going out the same paper is counted twice, which reads as one paper too many
    rather than one too few — the safe direction for a number whose only job is
    to stop someone quitting on top of work. This route is a GET, so it is never
    counted and never reports itself.

    The two counters are sampled under separate locks, so the order they are
    read in is the whole of what makes this snapshot safe: read `arriving`
    first. A request only stops being counted once its response has gone out,
    and it makes its jobs before it answers, so an `arriving` of zero already
    implies that anything in flight a moment ago has left its jobs behind for
    the read that follows. The other order guarantees nothing: `jobs` would be
    sampled at an instant `arriving` cannot vouch for, and a paper landing
    between the two reads would fall through both — an idle answer, and a
    launcher stopping the server on top of a queued job. Do not swap these.

    The two kinds of work are also reported apart, because they are not lost by
    the same event. `jobs` dies with the server: stop it mid-extraction and the
    paper stays unread. `arriving` dies with the client that is sending it, and
    a client can stop while the server keeps running — the macOS app quitting
    tears down its web view's upload even when the server it adopted lives on.
    An app deciding whether it may quit needs to tell those apart. `busy` stays
    their sum, which is what it has always meant, so nothing that reads only
    that number has to change.

    `arriving` is every request on the wire that is not a read, not only the
    uploads it began as; see `CountArrivingRequests` for why the net is that
    wide. The field name is unchanged because its meaning is: work in flight
    that belongs to whoever is sending it. An app that reads it as "papers being
    added" is right about the only thing it does with the number — whether it is
    zero — and wrong only about the wording of a dialog it shows in the
    microseconds a small POST is on the wire.
    """
    with _arrivals_lock:
        arriving = _requests_arriving
    with _jobs_lock:
        jobs = sum(1 for job in _jobs.values() if job["state"] in ACTIVE_JOB_STATES)
    return {
        "app": "doxograph",
        "version": __version__,
        "busy": jobs + arriving,
        "jobs": jobs,
        "arriving": arriving,
    }


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
        "tensions": store.tension_rows(rows),
        "tension_kinds": store.TENSION_KINDS,
        "tension_statuses": store.TENSION_STATUSES,
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
    """Stage every upload on disk, then hand the worker its path.

    Reading each file into memory kept the whole batch resident: three workers
    run at a time and every queued job held its own `bytes` until its turn came,
    so a drop of ten large PDFs cost ten PDFs of memory rather than three.
    """
    queued = 0
    for upload in files:
        name = upload.filename or "upload.pdf"
        # On a worker thread: copying a large drop on the event loop would stop
        # the page polling, saving or doing anything else until it finished.
        staged = await run_in_threadpool(ingest.stage_upload, upload.file, name)
        job = _new_job(name)
        try:
            _pool.submit(_run_upload, job, staged, name, extract_now)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise
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
    # Vocabulary lock before paper lock: `rename_tag` takes them in that order,
    # and accepting a tag takes both, so the reverse order here would deadlock.
    with store.vocab_lock(), store.paper_lock(key):
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


@app.post("/api/tensions")
def find_tensions(body: TensionsBody) -> dict:
    """Queue a pass over every topic where two papers could disagree.

    Topics named in the body are deduplicated and kept only where a tension is
    possible; a topic with claims from one paper has nothing to find, so it
    would cost a model call to learn nothing.
    """
    possible = store.tension_topics()
    topics = [t for t in possible if t in set(body.topics)] if body.topics else possible
    if not topics:
        return {"queued": 0}
    job = _new_job(f"tensions in {len(topics)} topics")
    _pool.submit(_run_tensions, job, topics)
    return {"queued": len(topics)}


@app.patch("/api/tensions/{tension_id}")
def patch_tension(tension_id: str, body: TensionStatusBody) -> dict:
    try:
        return store.set_tension_status(tension_id, body.status)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except KeyError:
        raise HTTPException(404, f"no tension {tension_id}")


@app.delete("/api/tensions/{tension_id}")
def remove_tension(tension_id: str) -> dict:
    try:
        store.delete_tension(tension_id)
    except KeyError:
        raise HTTPException(404, f"no tension {tension_id}")
    return {"deleted": tension_id}


@app.post("/api/export")
def api_export(body: ExportBody) -> dict:
    path = export.write(Path(body.path).expanduser() if body.path else None, title=body.title)
    return {"path": str(path)}


@app.get("/api/bibtex", response_class=PlainTextResponse)
def api_bibtex() -> PlainTextResponse:
    return PlainTextResponse(bib.render(), media_type="text/plain")


@app.get("/pdf/{key}")
def serve_pdf(key: str, inline: bool = False) -> FileResponse:
    """Hand back a paper's PDF, as an attachment unless asked otherwise.

    A browser keeps the attachment it has always been given. The Mac app asks
    for `?inline=1`, because a WKWebView reads `Content-Disposition: attachment`
    as an instruction to download the file rather than draw it, and a window
    opened to read the paper that instead saves it somewhere is no use.
    """
    path = store.pdf_path(key)
    if not path.exists():
        raise HTTPException(404, f"no PDF for {key}")
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{key}.pdf",
        content_disposition_type="inline" if inline else "attachment",
    )


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    config.ensure_dirs()
    uvicorn.run("doxograph.server:app" if reload else app, host=host, port=port, reload=reload)
