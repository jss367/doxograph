"""Routes for importing papers and editing their claims."""

from __future__ import annotations

import logging
from typing import Any, Literal

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, field_validator

from . import chats, cites, config, extract, ingest, job_journal, job_queue, pairs, search, store
from .model_errors import ModelSetupError

logger = logging.getLogger(__name__)

router = APIRouter()


class IngestBody(BaseModel):
    text: str = ""
    extract: bool = True


class ProposedTagsBody(BaseModel):
    accept: list[str] = []
    discard: list[str] = []


class ReviewBody(BaseModel):
    """A bulk review decision. `claims` restricts it to those claims; without
    it every claim on the paper is set."""

    reviewed: bool = True
    claims: list[str] | None = None


class LedgerLink(BaseModel):
    claim: str
    relation: str = config.LEDGER_RELATIONS[-1]
    note: str = ""

    @field_validator("relation")
    @classmethod
    def _known_relation(cls, value: str) -> str:
        if value not in config.LEDGER_RELATIONS:
            raise ValueError(f"relation must be one of {config.LEDGER_RELATIONS}")
        return value


class ClaimPatch(BaseModel):
    """The claim fields a person may set, with the types they must hold.

    Only the fields the caller sent are applied, so a patch stays a patch. A
    `kind` or `strength` outside the known set is refused here rather than
    stored and shown as a blank badge.
    """

    text: str | None = None
    kind: str | None = None
    strength: str | None = None
    tags: list[str] | None = None
    evidence: str | None = None
    quote: str | None = None
    locator: str | None = None
    ledger_links: list[LedgerLink] | None = None
    # The reader's own note on the claim. Free text: it is the one field here
    # nothing but a person writes, so there is nothing to validate it against.
    note: str | None = None
    reviewed: bool | None = None
    conditions: str | None = Field(default=None, max_length=20000)
    limitations: str | None = Field(default=None, max_length=20000)
    evidence_origin: str | None = Field(default=None, max_length=300)
    evidence_relationship: Literal["unassessed", "original", "independent_replication", "shared_data"] | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str | None) -> str | None:
        if value is not None and value not in config.CLAIM_KINDS:
            raise ValueError(f"kind must be one of {config.CLAIM_KINDS}")
        return value

    @field_validator("strength")
    @classmethod
    def _known_strength(cls, value: str | None) -> str | None:
        if value is not None and value not in config.CLAIM_STRENGTHS:
            raise ValueError(f"strength must be one of {config.CLAIM_STRENGTHS}")
        return value

    @field_validator("tags")
    @classmethod
    def _slug_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return sorted({store.slugify(t) for t in value if store.slugify(t)})

    def fields(self) -> dict[str, Any]:
        """The fields the caller sent, as plain values the store writes.

        A null is treated as not sent. No claim field is cleared by null (an
        empty string or list does that), and writing None would leave a claim
        that `tag_counts` and the export cannot read.
        """
        return {k: v for k, v in self.model_dump(exclude_unset=True).items() if v is not None}


class PaperPatch(BaseModel):
    """The paper fields a person may correct, and the types they must hold.

    A year of `"2020"` was once accepted as a string and broke the export,
    which sorts papers by year. Only the fields the caller sent are applied:
    `None` is a value to write (clearing a year), an absent field is left alone.

    Title, authors and notes take no null. Every reader of them expects text
    or a list — the export, BibTeX, the notebook, `doxograph list` — and a
    null written to disk broke each of those. An empty string or list clears
    them instead. The defaults are never written, since only sent fields are.
    """

    title: str = ""
    authors: list[str] = []
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    summary: str | None = None
    relevance: str | None = None
    # The reader's own note on the paper. Why the ingest has no PDF is
    # `error`, which is the machine's to write and not offered here.
    notes: str = ""
    labels: list[str] | None = None

    @field_validator("labels")
    @classmethod
    def _normalize_labels(cls, value: list[str] | None) -> list[str] | None:
        # A null clears them. Every reader of the field treats it as a list —
        # `label_counts` walks it, the page joins it — and writing None would
        # leave a paper none of them can read.
        return store.normalize_labels(value or [])


def _settled(key: str | None, do_extract: bool) -> str | None:
    """A paper already here that a job would have nothing to do for.

    Queuing one puts a job on the strip that fetches nothing and reports what
    was already true, a poll later, after the reader has moved on. A paper
    waiting to be read is not settled: the job is what reads it.
    """
    if not key or (do_extract and store.needs_extraction(key)):
        return None
    return key


@router.post("/api/ingest")
def api_ingest(body: IngestBody) -> dict[str, Any]:
    """Queue what has to be fetched, and say outright what is already here."""
    refs, unknown = ingest.parse_refs(body.text)
    do_extract = body.extract and config.ai_enabled()
    queued, known = 0, []
    for ref in refs:
        settled = _settled(ingest.already_stored(ref), do_extract)
        if settled:
            known.append({"ref": ref.raw, "key": settled})
            continue
        job = job_queue._new_import_job(ref.value, do_extract, ref=ref)
        job_queue._submit_job(job_queue._run_ingest, job, ref, do_extract)
        queued += 1
    return {"queued": queued, "known": known, "unknown": unknown}


@router.post("/api/upload")
async def api_upload(files: list[UploadFile], extract_now: bool = True) -> dict[str, Any]:
    """Stage every upload on disk, then hand the worker its path.

    Reading each file into memory kept the whole batch resident: three workers
    run at a time and every queued job held its own `bytes` until its turn came,
    so a drop of ten large PDFs cost ten PDFs of memory rather than three.
    """
    do_extract = extract_now and config.ai_enabled()
    queued = 0
    for upload in files:
        name = upload.filename or "upload.pdf"
        # On a worker thread: copying a large drop on the event loop would stop
        # the page polling, saving or doing anything else until it finished.
        staged = await run_in_threadpool(ingest.stage_upload, upload.file, name)
        # The bytes name the paper, so a file already here needs no worker: it
        # has nothing to parse, look up or download. It is still reported as a
        # finished job rather than in this response, because the Mac app posts
        # here too — a PDF dropped on the Dock or opened from Finder — and the
        # job strip in the window is the only report it has.
        settled = _settled(await run_in_threadpool(ingest.already_uploaded, staged), do_extract)
        if settled:
            staged.unlink(missing_ok=True)
            job_queue._set(job_queue._new_job(name), kind="import", key=settled, label=settled,
                 state="done", detail="already in the corpus")
            # Nothing else will: the workers prune on their way out, and this
            # notice never reaches one. Without it a session that keeps
            # re-dropping papers it already has grows `_jobs` for ever.
            job_queue._prune_jobs()
            continue
        job = job_queue._new_import_job(name, do_extract, staged=staged, filename=name)
        try:
            job_queue._submit_job(job_queue._run_upload, job, staged, name, do_extract)
        except BaseException:
            with job_queue._jobs_lock:
                job_queue._jobs.pop(job["id"], None)
            job_queue._forget_import(job)
            job_journal.remove(job)
            raise
        queued += 1
    return {"queued": queued}


@router.get("/api/papers/{key}")
def get_paper(key: str) -> dict[str, Any]:
    try:
        return store.load_paper(key)
    except KeyError:
        raise HTTPException(404, f"no paper {key}") from None


@router.patch("/api/papers/{key}")
def patch_paper(key: str, patch: PaperPatch) -> dict[str, Any]:
    # Only the fields the caller sent: an unset field is not a field set to
    # None, and writing the whole model would blank everything left out.
    fields = patch.model_dump(exclude_unset=True)
    with store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except KeyError:
            raise HTTPException(404, f"no paper {key}") from None
        paper.update(fields)
        store.save_paper(paper)
    return paper


@router.delete("/api/papers/{key}")
def remove_paper(key: str) -> dict[str, Any]:
    # A key with nothing on disk has nothing to remove, and retiring it would
    # still spend the citekey for good: a stale or mistyped request must not.
    if not key.strip() or not any(path.exists() for path in
                                  (store.paper_path(key), store.pdf_path(key), store.text_path(key))):
        raise HTTPException(404, "No such paper.")
    try:
        extract.delete_paper(key)
        chats.delete_paper_chats(key)
    except ModelSetupError as exc:
        raise HTTPException(502, str(exc)[:500]) from exc
    except (anthropic.APIError, httpx.HTTPError) as exc:
        logger.exception("Removing a paper's uploads failed")
        raise HTTPException(502, "Anthropic could not be reached to delete this paper's uploaded PDF. "
                                 "Check your connection, then remove the paper again.") from exc
    return {"deleted": key}


@router.post("/api/papers/{key}/extract")
def reextract(key: str, keep_reviewed: bool = True) -> dict[str, Any]:
    job_queue._require_analysis()
    if not store.paper_path(key).exists():
        raise HTTPException(404, f"no paper {key}")
    job = job_queue._new_job(key)
    job_queue._submit_job(job_queue._run_extract, job, key, keep_reviewed)
    return {"queued": job["id"]}


@router.post("/api/papers/{key}/review")
def review_paper_claims(key: str, body: ReviewBody) -> dict[str, Any]:
    """Mark a paper's claims reviewed in one write.

    Reviewing is the app's main work, and a paper arrives with a dozen claims
    at once. The ids that changed come back so the page can offer an undo that
    unreviews exactly those, leaving claims reviewed earlier alone.
    """
    try:
        changed = store.review_claims(key, body.reviewed, body.claims)
    except KeyError:
        raise HTTPException(404, f"no paper {key}") from None
    return {"key": key, "reviewed": body.reviewed, "changed": changed}


@router.post("/api/papers/{key}/verify")
def verify_quotes(key: str) -> dict[str, Any]:
    """Re-check every quote on a paper against its PDF. Fast enough to run
    inline: the PDF text is read once and cached."""
    try:
        paper = store.verify_quotes(key)
    except KeyError:
        raise HTTPException(404, f"no paper {key}") from None
    return {"key": key, "n_unverified": store.summarize(paper)["n_unverified"]}


@router.get("/api/citations")
def citations() -> dict[str, Any]:
    """Which papers in the corpus cite which, from their reference lists.

    Read off the PDFs already on disk. The map asks for this when it opens
    rather than with the rest of the state, since it is the only thing that
    wants it and the whole corpus has to be read to answer.
    """
    return {"edges": cites.edges()}


@router.get("/api/search")
def search_text(q: str = "", limit: int = 20, offset: int = 0) -> dict[str, Any]:
    """The papers whose own text holds every word of the query.

    The claims are searched in the page, which needs no server at all. This is
    the other half: a word in a paper that no claim mentions, and a paper
    nothing has been extracted from yet.
    """
    page_size = max(1, min(limit, 100))
    start = max(0, offset)
    hits = search.search_papers(q, limit=page_size + 1, offset=start)
    known = {paper["key"]: paper for paper in store.all_papers()}
    return {
        "q": q,
        "terms": search.terms(q),
        "next_offset": start + page_size if len(hits) > page_size else None,
        "papers": [
            {
                **hit,
                "title": known[hit["key"]].get("title", ""),
                "authors": known[hit["key"]].get("authors") or [],
                "year": known[hit["key"]].get("year"),
            }
            for hit in hits[:page_size] if hit["key"] in known
        ],
    }


@router.get("/api/papers/{key}/claims/{claim_id}/similar")
def similar_claims(key: str, claim_id: str, limit: int = 5) -> dict[str, Any]:
    """Claims from other papers that resemble this one, by their words.

    No model: it compares the claims already on disk. A suggestion for reading,
    not a judgment — whether two claims actually bear on each other is what the
    tensions and agreements passes are for.
    """
    rows = store.claim_rows()
    by_id = {row["id"]: row for row in rows}
    if by_id.get(claim_id, {}).get("paper") != key:
        raise HTTPException(404, f"no claim {claim_id} on {key}")
    found = pairs.similar_to(claim_id, rows, limit=max(1, min(limit, 20)))
    return {
        "claim": claim_id,
        "similar": [
            {
                **hit,
                "text": by_id[hit["claim"]].get("text", ""),
                "paper": by_id[hit["claim"]]["paper"],
                "paper_authors": by_id[hit["claim"]].get("paper_authors", []),
                "paper_year": by_id[hit["claim"]].get("paper_year"),
            }
            for hit in found
        ],
    }


@router.get("/api/papers/{key}/claims/{claim_id}/quote-context")
def quote_context(key: str, claim_id: str) -> dict[str, Any]:
    """The passage of the PDF a claim's quote was matched against."""
    if not store.paper_path(key).exists():
        raise HTTPException(404, f"no paper {key}")
    try:
        return store.quote_context(key, claim_id)
    except KeyError:
        raise HTTPException(404, f"no claim {claim_id} on {key}") from None


@router.post("/api/papers/{key}/claims")
def create_claim(key: str, patch: ClaimPatch | None = None) -> dict[str, Any]:
    try:
        return store.add_claim(key, (patch or ClaimPatch()).fields())
    except KeyError:
        raise HTTPException(404, f"no paper {key}") from None


@router.patch("/api/papers/{key}/claims/{claim_id}")
def patch_claim(key: str, claim_id: str, patch: ClaimPatch) -> dict[str, Any]:
    try:
        return store.update_claim(key, claim_id, patch.fields())
    except KeyError:
        raise HTTPException(404, f"no claim {claim_id} on {key}") from None


@router.delete("/api/papers/{key}/claims/{claim_id}")
def remove_claim(key: str, claim_id: str) -> dict[str, Any]:
    try:
        store.delete_claim(key, claim_id)
    except KeyError:
        raise HTTPException(404, f"no claim {claim_id} on {key}") from None
    return {"deleted": claim_id}


@router.post("/api/papers/{key}/proposed-tags")
def resolve_proposed_tags(key: str, body: ProposedTagsBody) -> dict[str, Any]:
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
            raise HTTPException(404, f"no paper {key}") from None
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


@router.get("/pdf/{key}")
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
