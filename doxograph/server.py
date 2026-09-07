"""Local web app: drop papers in, review the claims that come back out."""

from __future__ import annotations

import ipaddress
import json
import functools
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import Body, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, field_validator

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
    """Count a request that might become work from the moment it lands.

    A handler cannot count this for itself: Starlette has received the whole
    body before the handler runs, and during that receive the job does not
    exist yet. `/api/health` would say idle while a paper was still arriving,
    and the macOS launcher would stop the server on top of it.

    Counted by method rather than by a list of routes, so a new route that
    creates work is counted without anyone remembering to add it. Reads are
    left out: `/api/health` is itself a GET and would report itself busy.
    Written as raw ASGI so it does not pump `/pdf/{key}` through an extra
    stream to do the counting.
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


#: Carries the address `serve()` bound to into a `--reload` subprocess, which
#: re-imports this module instead of calling `serve()` again and would otherwise
#: fall back to loopback-only and refuse the page's own writes.
BIND_ENV = "DOXOGRAPH_BIND"

#: Where an operator who published this server has said its page lives: a
#: comma-separated list of origins, or bare `host:port` authorities. Needed
#: where the name the browser typed cannot be read off the socket and must not
#: be taken from the request: a wildcard bind, or a Unix socket. Behind a TLS
#: terminator publish both `https://name` (the browser's Origin) and
#: `http://name` (the forwarded Host). See `trust_bind`.
PUBLISHED_ORIGINS_ENV = "DOXOGRAPH_PUBLISHED_ORIGINS"

#: The port a scheme means when an authority does not spell one out. A `Host`
#: header carries no scheme of its own, so the request's scheme does that work.
DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}

#: `(hostname, port)` pairs this server answers for besides the loopback ones,
#: filled in from where `serve()` bound and from `PUBLISHED_ORIGINS_ENV`. Empty
#: under `uvicorn doxograph.server:app`, which binds the loopback interface.
_published_authorities: frozenset[tuple[str, int | None]] = frozenset()

#: True when the bind was a wildcard address. Nothing is trusted on account of
#: it; it only decides whether `serve()` warns that the server has been
#: published without anyone saying under what name.
_bound_to_every_address = False


def _is_loopback(hostname: str) -> bool:
    """Whether a URL's host part names this machine's loopback interface."""
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


#: The loopback names a browser actually produces. `127.0.0.2:8765` is
#: loopback too, but it is a different browser origin from the page this app
#: is served on, so for an Origin check it is another site.
_CANONICAL_LOOPBACK = frozenset({ipaddress.ip_address("127.0.0.1"),
                                 ipaddress.ip_address("::1")})


def _is_canonical_loopback(hostname: str) -> bool:
    """Whether a host part is one of the names a browser gives this machine."""
    if hostname == "localhost":
        return True
    try:
        # Via `ip_address` so the long spellings of `::1` are recognised too.
        return ipaddress.ip_address(hostname) in _CANONICAL_LOOPBACK
    except ValueError:
        return False


def _origin_authority(origin: str) -> tuple[str, int | None]:
    """An origin as the `(hostname, port)` pair it names, the port made explicit.

    A port the origin leaves out is the one its scheme implies. Anything
    `urlsplit` cannot parse, including a half-written IPv6 literal, comes back
    as the empty authority, which nobody trusts.
    """
    try:
        split = urlsplit(origin)
        hostname, port = split.hostname or "", split.port
    except ValueError:
        return "", None
    return hostname, port if port is not None else DEFAULT_PORTS.get(split.scheme)


def _host_authority(host: str, scheme: str) -> tuple[str, int | None]:
    """A `Host` header as the same pair. It has no scheme, so it is lent one."""
    return _origin_authority(f"{scheme}://{host}")


def _is_our_own(
    authority: tuple[str, int | None],
    bound_port: int | None,
    *,
    as_a_browser_origin: bool = False,
) -> bool:
    """Whether an authority is one this server could itself be answering for.

    Trust comes from where this server listens, never from what a request
    says about itself. Loopback is trusted against `bound_port`, read off the
    listening socket, so `http://localhost:3000` stays another origin. The
    scheme is not compared: a Host carries none, and the same server behind a
    TLS terminator is the same server.

    Published authorities are consulted first, so `https://localhost` in front
    of a backend on another port is answered for. `as_a_browser_origin` narrows
    loopback to the names a browser gives this machine, since another loopback
    address is another browser origin. With no port on the socket (a Unix
    socket) a Host can still be judged by name, but an Origin has to be one
    the operator published, or the loopback rule would admit every program on
    the machine.
    """
    hostname, port = authority
    if not hostname:
        return False
    if authority in _published_authorities:
        return True
    if (_is_canonical_loopback if as_a_browser_origin else _is_loopback)(hostname):
        if bound_port is None:
            return not as_a_browser_origin
        return port == bound_port
    return False


def _authorities_from_environment() -> set[tuple[str, int | None]]:
    """The authorities `PUBLISHED_ORIGINS_ENV` names, ignoring the unparseable."""
    published = set()
    for item in os.environ.get(PUBLISHED_ORIGINS_ENV, "").split(","):
        item = item.strip()
        if not item:
            continue
        authority = _origin_authority(item) if "//" in item else _host_authority(item, "http")
        if authority[0]:
            published.add(authority)
    return published


def trust_bind(host: str, port: int) -> None:
    """Record where the server is listening, so its own page is recognised.

    The canonical loopback names need no recording. Another loopback address
    does, or `serve --host 127.0.0.2` would lock out its own page. A wildcard
    bind records nothing: which of this machine's names the browser typed is
    not knowable from the socket and must not be read from the request, so the
    operator says it via `PUBLISHED_ORIGINS_ENV`.
    """
    global _published_authorities, _bound_to_every_address
    host = host.strip("[]").lower()
    _bound_to_every_address = host in {"0.0.0.0", "::", ""}
    published = _authorities_from_environment()
    if not _bound_to_every_address and not _is_canonical_loopback(host):
        published.add((host, port))
    _published_authorities = frozenset(published)


def _trust_bind_from_environment() -> None:
    global _published_authorities
    bind = os.environ.get(BIND_ENV, "")
    host, _, port = bind.rpartition(":")
    if host and port.isdigit():
        trust_bind(host, int(port))
    else:
        _published_authorities = frozenset(_authorities_from_environment())


_trust_bind_from_environment()


class RejectCrossSiteRequests:
    """Refuse a request another site sent, or one addressed to another name.

    The Host check carries the weight. A page on `evil.example.com` whose name
    has been rebound to `127.0.0.1` reaches this server over a connection the
    browser considers same-origin, and it can read every reply. Comparing
    Origin against Host would not catch it, since the page sets both. What it
    cannot change is the name it asked for, which the browser puts in Host, so
    every request has to be addressed to an authority this server answers for,
    as `_is_our_own` works out from the socket.

    The Origin check is the ordinary cross-site one. `/api/upload` is a
    multipart POST, which a browser sends across origins without a preflight;
    every other mutating route takes JSON and is held back by one. A request
    with no Origin (curl, the CLI, the macOS uploader) is left alone.

    Outermost of the middlewares, so a refused request is never counted as
    work in flight and never selects a workspace.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        scheme = scope.get("scheme") or "http"
        bound_port = (scope.get("server") or (None, None))[1]
        host = headers.get(b"host", b"").decode("utf-8", "replace")
        if not _is_our_own(_host_authority(host, scheme), bound_port):
            return await self._refuse(
                f"request addressed to {host!r} refused: not a name this server answers for",
                scope, receive, send,
            )
        if scope.get("method", "GET") in SAFE_METHODS:
            return await self.app(scope, receive, send)
        origin = headers.get(b"origin", b"").decode("utf-8", "replace")
        if origin and not _is_our_own(_origin_authority(origin), bound_port,
                                      as_a_browser_origin=True):
            return await self._refuse(
                f"cross-site request from {origin} refused", scope, receive, send
            )
        return await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(detail: str, scope, receive, send):
        # 403 rather than 421: a 421 invites an HTTP/2 client to retry on a
        # fresh connection, and the objection is to the request itself.
        response = JSONResponse({"detail": detail}, status_code=403)
        return await response(scope, receive, send)


class SelectWorkspace:
    """Bind each request to one corpus without changing any process-global path.

    The page uses a header for API requests. Downloads opened in a new browser
    tab cannot add a custom header, so PDF and BibTeX links carry the same value
    as a query parameter instead.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # These routes either do not read a corpus or are themselves how the
        # workspace registry is inspected. In particular, the native launcher
        # must be able to reach health and load the app shell even when a broken
        # registry needs to be reported in the UI.
        if scope.get("path") in {
            "/", "/app.css", "/app.js", "/favicon.png", "/api/health", "/api/workspaces",
        }:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        selected = headers.get(b"x-doxograph-workspace", b"").decode("utf-8", "replace")
        if not selected:
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            selected = (query.get("workspace") or [config.DEFAULT_WORKSPACE_ID])[0]
        if config.get_workspace(selected) is None:
            response = JSONResponse({"detail": f"no workspace {selected}"}, status_code=404)
            return await response(scope, receive, send)
        with config.use_workspace(selected):
            return await self.app(scope, receive, send)


app.add_middleware(CountArrivingRequests)
app.add_middleware(SelectWorkspace)
# Added last, so it wraps the other two and turns a cross-site request away
# before either of them sees it.
app.add_middleware(RejectCrossSiteRequests)


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
        job["workspace"] = config.workspace_id()
        _jobs[job["id"]] = job
        return job


def _set(job: dict, **fields) -> None:
    with _jobs_lock:
        job.update(fields)


def _prune_jobs() -> None:
    with _jobs_lock:
        by_workspace: dict[str, list[dict]] = {}
        for job in _jobs.values():
            if job["state"] not in ("done", "error"):
                continue
            workspace = job.get("workspace", config.DEFAULT_WORKSPACE_ID)
            by_workspace.setdefault(workspace, []).append(job)
        for done in by_workspace.values():
            for job in sorted(done, key=lambda item: item["id"])[:-40]:
                _jobs.pop(job["id"], None)


def _workspace_job(function):
    """Keep a queued job in the corpus from which it was submitted."""
    @functools.wraps(function)
    def wrapped(job: dict, *args, **kwargs):
        with config.use_workspace(job.get("workspace", config.DEFAULT_WORKSPACE_ID)):
            return function(job, *args, **kwargs)
    return wrapped


# --- background work ------------------------------------------------------

@_workspace_job
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


@_workspace_job
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


@_workspace_job
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


def _finish_pass(job: dict, total: int, unit: str, failed: int, last_failure: str, summary: str) -> None:
    if failed:
        _set(job, state="error", detail=f"{failed} of {total} {unit} failed, {summary}; {last_failure}")
    else:
        _set(job, state="done", detail=f"{total} {unit}, {summary}")


@_workspace_job
def _run_tensions(job: dict, topics: list[str]) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        added = reopened = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.find_tensions(t, rows, tags)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                added += result["added"]
                reopened += result["reopened"]
            _set(job, detail=f"{done} of {len(topics)} topics")
        summary = f"{added} new" + (f", {reopened} reopened" if reopened else "")
        _finish_pass(job, len(topics), "topics", failed, last_failure, summary)
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_agreements(job: dict, topics: list[str]) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        added = grown = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.find_agreements(t, rows, tags)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                added += result["added"]
                grown += result["grown"]
            _set(job, detail=f"{done} of {len(topics)} topics")
        summary = f"{added} new" + (f", {grown} grown" if grown else "")
        _finish_pass(job, len(topics), "topics", failed, last_failure, summary)
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_syntheses(job: dict, topics: list[str]) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        written = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.synthesize_topic(t, rows, tags)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                written += 1 if result["written"] else 0
            _set(job, detail=f"{done} of {len(topics)} topics")
        if failed:
            _set(job, state="error",
                 detail=f"{failed} of {len(topics)} topics failed, {written} written; {last_failure}")
        else:
            _set(job, state="done", detail=f"{written} of {len(topics)} topics written")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_retag(job: dict, keys: list[str]) -> None:
    try:
        failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(keys)}")
        for key, _result, exc in extract.run_concurrently(keys, extract.retag_paper):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{key}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            _set(job, key=key, detail=f"{done} of {len(keys)}")
        if failed:
            _set(job, state="error",
                 detail=f"{failed} of {len(keys)} papers failed; {last_failure}")
        else:
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


class AgreementsBody(BaseModel):
    topics: list[str] | None = None


class AgreementStatusBody(BaseModel):
    status: str


class SynthesesBody(BaseModel):
    topics: list[str] | None = None


class SynthesisTextBody(BaseModel):
    text: str


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
    reviewed: bool | None = None

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

    def fields(self) -> dict:
        """The fields the caller sent, as plain values the store writes."""
        return self.model_dump(exclude_unset=True)


class PaperPatch(BaseModel):
    """The paper fields a person may correct, and the types they must hold.

    A year of `"2020"` was once accepted as a string and broke the export,
    which sorts papers by year. Only the fields the caller sent are applied:
    `None` is a value to write (clearing a year), an absent field is left alone.
    """

    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    summary: str | None = None
    relevance: str | None = None
    notes: str | None = None


class LedgerClaim(BaseModel):
    id: str
    text: str = ""


class LedgerBody(BaseModel):
    claims: list[LedgerClaim]


class ContextBody(BaseModel):
    text: str


class ExportBody(BaseModel):
    path: str | None = None
    title: str = "Doxograph"


class WorkspaceBody(BaseModel):
    name: str


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


@app.get("/favicon.png")
def favicon() -> FileResponse:
    return FileResponse(STATIC / "favicon.png", media_type="image/png")


ACTIVE_JOB_STATES = ("queued", "fetching", "reading")


@app.get("/api/health")
def health() -> dict:
    """Say the server is up, and whether it is in the middle of something.

    The macOS launcher polls this while it waits for uvicorn to bind and asks
    again on quit. `/api/state` would answer but loads the corpus to do it.

    Read `arriving` before `jobs`. A request stops being counted only once its
    response has gone out, and it makes its jobs before it answers, so an
    `arriving` of zero means anything in flight a moment ago has already left
    its jobs behind for the read that follows. The other order lets a paper
    landing between the two reads fall through both. Do not swap these.

    The two are reported apart because they are lost by different events:
    `jobs` dies with the server, `arriving` with the client sending it. `busy`
    is their sum. A request in `arriving` is counted twice for the sliver
    between job creation and response, which errs towards busy.
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


#: The last `/api/state` answer per workspace, with the corpus signature it
#: was built from. The page polls twice a second and the corpus changes far
#: less often than that; the signature is a directory listing, the answer is
#: every paper file read and joined.
_state_cache: dict[str, tuple[str, dict]] = {}
_state_cache_lock = threading.Lock()


def _build_state() -> dict:
    papers = store.all_papers()
    rows = store.claim_rows(papers)
    return {
        "workspace": config.get_workspace(),
        "papers": [store.summarize(p) for p in papers],
        "claims": rows,
        "tags": store.load_tags(),
        "tag_counts": store.tag_counts(rows),
        "ledger": store.load_ledger(),
        "context": store.load_context(),
        "tensions": store.tension_rows(rows),
        "agreements": store.agreement_rows(rows),
        "syntheses": store.synthesis_rows(rows),
        "tension_kinds": store.TENSION_KINDS,
        "tension_statuses": store.TENSION_STATUSES,
        "kinds": config.CLAIM_KINDS,
        "strengths": config.CLAIM_STRENGTHS,
        "relations": config.LEDGER_RELATIONS,
        "data_dir": str(config.data_dir()),
        "model": config.MODEL,
        "has_key": config.api_key() is not None,
    }


@app.get("/api/state")
def state(request: Request) -> Response:
    """The corpus as the page shows it. Jobs are not in here; they change
    every second while a paper is read and have their own route.

    The answer carries the corpus signature as its ETag, and a request that
    presents the same one back gets a 304 with no body.
    """
    workspace = config.workspace_id()
    signature = store.corpus_signature()
    etag = f'"{signature}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    with _state_cache_lock:
        cached = _state_cache.get(workspace)
    if cached is None or cached[0] != signature:
        payload = _build_state()
        with _state_cache_lock:
            _state_cache[workspace] = (signature, payload)
    else:
        payload = cached[1]
    return JSONResponse(payload, headers={"ETag": etag})


@app.get("/api/jobs")
def jobs() -> dict:
    """The recent background jobs in this workspace, newest first."""
    with _jobs_lock:
        recent = sorted(
            (job for job in _jobs.values() if job.get("workspace") == config.workspace_id()),
            key=lambda j: j["id"], reverse=True,
        )[:20]
    return {"jobs": recent}


@app.get("/api/workspaces")
def workspaces() -> dict:
    return {"workspaces": config.list_workspaces()}


@app.post("/api/workspaces", status_code=201)
def create_workspace(body: WorkspaceBody) -> dict:
    try:
        workspace = config.create_workspace(body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return {"workspace": workspace, "workspaces": config.list_workspaces()}


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


@app.patch("/api/papers/{key}")
def patch_paper(key: str, patch: PaperPatch) -> dict:
    # Only the fields the caller sent: an unset field is not a field set to
    # None, and writing the whole model would blank everything left out.
    fields = patch.model_dump(exclude_unset=True)
    with store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except KeyError:
            raise HTTPException(404, f"no paper {key}")
        paper.update(fields)
        store.save_paper(paper)
    return paper


@app.delete("/api/papers/{key}")
def remove_paper(key: str) -> dict:
    extract.delete_paper(key)
    return {"deleted": key}


@app.post("/api/papers/{key}/extract")
def reextract(key: str, keep_reviewed: bool = True) -> dict:
    if not store.paper_path(key).exists():
        raise HTTPException(404, f"no paper {key}")
    job = _new_job(key)
    _pool.submit(_run_extract, job, key, keep_reviewed)
    return {"queued": job["id"]}


@app.post("/api/papers/{key}/verify")
def verify_quotes(key: str) -> dict:
    """Re-check every quote on a paper against its PDF. Fast enough to run
    inline: the PDF text is read once and cached."""
    try:
        paper = store.verify_quotes(key)
    except KeyError:
        raise HTTPException(404, f"no paper {key}")
    return {"key": key, "n_unverified": store.summarize(paper)["n_unverified"]}


@app.post("/api/retag")
def retag(body: RetagBody) -> dict:
    keys = body.keys or [p["key"] for p in store.all_papers() if p.get("claims")]
    if not keys:
        return {"queued": 0}
    job = _new_job(f"retag {len(keys)} papers")
    _pool.submit(_run_retag, job, keys)
    return {"queued": len(keys)}


@app.post("/api/papers/{key}/claims")
def create_claim(key: str, patch: ClaimPatch = Body(default=ClaimPatch())) -> dict:
    try:
        return store.add_claim(key, patch.fields())
    except KeyError:
        raise HTTPException(404, f"no paper {key}")


@app.patch("/api/papers/{key}/claims/{claim_id}")
def patch_claim(key: str, claim_id: str, patch: ClaimPatch) -> dict:
    try:
        return store.update_claim(key, claim_id, patch.fields())
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


@app.post("/api/agreements")
def find_agreements(body: AgreementsBody) -> dict:
    """Queue a pass over every topic where two papers could agree: the same
    topics a tension is possible in."""
    possible = store.tension_topics()
    topics = [t for t in possible if t in set(body.topics)] if body.topics else possible
    if not topics:
        return {"queued": 0}
    job = _new_job(f"agreements in {len(topics)} topics")
    _pool.submit(_run_agreements, job, topics)
    return {"queued": len(topics)}


@app.patch("/api/agreements/{agreement_id}")
def patch_agreement(agreement_id: str, body: AgreementStatusBody) -> dict:
    try:
        return store.set_agreement_status(agreement_id, body.status)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except KeyError:
        raise HTTPException(404, f"no agreement {agreement_id}")


@app.delete("/api/agreements/{agreement_id}")
def remove_agreement(agreement_id: str) -> dict:
    try:
        store.delete_agreement(agreement_id)
    except KeyError:
        raise HTTPException(404, f"no agreement {agreement_id}")
    return {"deleted": agreement_id}


@app.post("/api/syntheses")
def synthesize(body: SynthesesBody) -> dict:
    """Queue a synthesis for each topic.

    Named topics are deduplicated and kept only where they have a claim; with
    none named, every topic with claims from two papers is written, since one
    paper's claims add up to little on their own.
    """
    if body.topics:
        rows = store.claim_rows()
        topics = sorted({t for t in body.topics if store.topic_claims(t, rows)})
    else:
        topics = store.synthesis_topics()
    if not topics:
        return {"queued": 0}
    job = _new_job(f"synthesis of {len(topics)} topics")
    _pool.submit(_run_syntheses, job, topics)
    return {"queued": len(topics)}


@app.patch("/api/syntheses/{topic}")
def patch_synthesis(topic: str, body: SynthesisTextBody) -> dict:
    try:
        return store.set_synthesis_text(topic, body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except KeyError:
        raise HTTPException(404, f"no synthesis for {topic}")


@app.delete("/api/syntheses/{topic}")
def remove_synthesis(topic: str) -> dict:
    try:
        store.delete_synthesis(topic)
    except KeyError:
        raise HTTPException(404, f"no synthesis for {topic}")
    return {"deleted": topic}


@app.put("/api/ledger")
def put_ledger(body: LedgerBody) -> dict:
    """Replace the ledger: the user's own claims, which extraction links to.

    Ids must be present and unique, since a link names a claim by id alone.
    Links on paper claims that name a removed id are left in place; the card
    shows the bare id, and the user can take the link off or restore the claim.
    """
    claims = []
    seen = set()
    for item in body.claims:
        claim_id = item.id.strip()
        if not claim_id:
            raise HTTPException(422, "every ledger claim needs an id")
        if claim_id in seen:
            raise HTTPException(422, f"ledger id {claim_id!r} is used twice")
        seen.add(claim_id)
        claims.append({"id": claim_id, "text": item.text.strip()})
    store.save_ledger(claims)
    return {"ledger": store.load_ledger()}


@app.put("/api/context")
def put_context(body: ContextBody) -> dict:
    """Replace the research context given to every model pass."""
    store.save_context(body.text)
    return {"context": store.load_context()}


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

    # The CLI refuses this first; this catches a direct caller. Port 0 means
    # "any free port", chosen after `trust_bind` has recorded the authority, so
    # the server would trust a port nobody listens on and refuse everything.
    if not 1 <= port <= 65535:
        raise ValueError(
            f"cannot serve on port {port}: ports run 1-65535, and 0 asks the "
            "kernel for whichever is free, so nobody can say where the page is"
        )
    config.ensure_dirs()
    trust_bind(host, port)
    os.environ[BIND_ENV] = f"{host}:{port}"
    if _bound_to_every_address and not _published_authorities:
        # Writes from anywhere but this machine will be refused until the
        # operator says what name the page is served under.
        print(
            f"warning: --host {host} publishes on every address, but the name a browser\n"
            f"         will use cannot be read off the socket and is not taken from the\n"
            f"         request. Only loopback is trusted until you set, for example,\n"
            f"         {PUBLISHED_ORIGINS_ENV}=http://this-machine.local:{port}"
        )
    uvicorn.run("doxograph.server:app" if reload else app, host=host, port=port, reload=reload)
