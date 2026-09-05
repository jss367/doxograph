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

from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
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


#: Carries the address `serve()` bound to into a `--reload` subprocess, which
#: re-imports this module instead of calling `serve()` again and would otherwise
#: fall back to loopback-only and refuse the page's own writes.
BIND_ENV = "DOXOGRAPH_BIND"

#: Where an operator who published this server has said its page really lives:
#: a comma-separated list of origins, or bare `host:port` authorities. Needed
#: wherever the name the browser typed cannot be read off the socket and must
#: not be taken from the request: a wildcard bind, and a Unix socket, which
#: reports no port for the origin rule to compare against. Behind a TLS
#: terminator, publish both spellings -- `https://name` for the browser's
#: `Origin`, and `http://name` for the `Host` it forwards over a plain-scheme
#: scope -- since an authority is matched on its port and the scheme is what
#: supplies the implicit one. See `trust_bind`.
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


#: The spellings a browser actually produces for this machine. `127.0.0.0/8` is
#: loopback all the way up, but a page served from `http://127.0.0.2:8765` is a
#: *different browser origin* from the one this app is served on, so it is
#: another site however local it is. `localhost`, `127.0.0.1` and `::1` stay
#: interchangeable: they all genuinely name here, and the browser picks between
#: them on the user's behalf.
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

    `urlsplit` does the fiddly parts: lowercasing the name, unwrapping the
    brackets around an IPv6 literal, and raising rather than guessing when the
    port is not a number. A port the origin leaves out is the one its scheme
    implies, so `http://localhost` and `http://localhost:80` are the same place.

    `urlsplit` itself raises on a half-written IPv6 literal such as `http://[::1`
    -- before any attribute is touched -- so it is inside the `try` as well.
    Anything unparseable comes back as the empty authority, which is trusted by
    nobody: a typo in an environment variable is skipped rather than being left
    to abort the import, and a malformed `Origin` header is refused rather than
    raising a 500 out of the middleware.
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

    Trust is derived from where this server listens, never from what a request
    says about itself. Loopback is where the app runs unless someone
    deliberately says otherwise, and `bound_port` -- read off the listening
    socket, so no client can influence it -- stops that trust from spreading to
    every other program on this machine: `http://localhost:3000` is a different
    origin, and a page served from it is a different site.

    The scheme is deliberately not part of the comparison. A `Host` header does
    not carry one, and the same server behind a TLS terminator is still the same
    server; the port is what distinguishes it from its neighbours.

    An authority the operator published is consulted first, before the loopback
    rule, so that a name like `https://localhost` in front of a backend on
    another port is answered for rather than silently dropped for failing the
    bound-port comparison. That ordering costs nothing in safety:
    `_published_authorities` is written only from the operator's own
    environment and the host `serve()` was given, never from a request.

    `as_a_browser_origin` narrows the loopback rule to the names a browser
    actually gives this machine. An `Origin` is judged that way: the whole of
    `127.0.0.0/8` answers here, but `http://127.0.0.2:8765` is a separate
    browser origin from the page this app is served on, so a page bound there
    is another site and its uploads are somebody else's. A `Host` keeps the
    broad rule -- it is asking which addresses reach this server rather than
    which page sent the request, and `curl http://127.0.0.5:8765` against a
    wildcard bind is a perfectly ordinary way to arrive. An alternate loopback
    address that `serve()` was actually given is recorded by `trust_bind`, so
    its own page keeps working under the narrow rule too.

    A scope carrying no port -- a Unix socket, which uvicorn reports as
    `(socket_path, None)` -- leaves nothing to compare against. A `Host` can
    still be judged on the name alone, since it only asks which addresses reach
    here. A browser origin cannot: with the port gone there is nothing left to
    separate this server's own page from `http://localhost:3000`, and the
    loopback rule would wave through every program on the machine. So an origin
    arriving that way has to be one the operator published. Nothing this CLI
    starts lands here -- `serve()` binds a TCP port and there is no `--uds`
    option -- but `uvicorn doxograph.server:app --uds ...` run by hand does.
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

    The loopback names a browser uses -- `localhost`, `127.0.0.1`, `::1` -- need
    no recording; they are trusted always, against whatever port the socket
    reports. Anywhere else in `127.0.0.0/8` does get recorded, because an origin
    is judged on the narrow list and `serve --host 127.0.0.2` would otherwise
    lock its own page out. A wildcard bind cannot be recorded either: `0.0.0.0` and
    `::` are the whole point of `--host`, publishing on every address this
    machine has, and which of them the browser then typed is not knowable from
    the socket. It is emphatically not knowable from the request's own headers,
    which is where a rebound page would like it to be read from, so nothing is
    inferred and the operator says it themselves via `PUBLISHED_ORIGINS_ENV`.
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

    Two checks, and the first is the one that carries the weight. A page on
    `evil.example.com` whose name has been rebound to `127.0.0.1` reaches this
    server over a connection the browser itself considers same-origin -- that is
    precisely what DNS rebinding buys, and it means the same-origin policy does
    not save us: the page can read every reply, so `/api/state` hands it the
    corpus and `/pdf/{key}` hands it the papers. Nor can it be caught by
    comparing `Origin` against `Host`, since both come from the request and the
    rebound page sets both to its own name; that comparison only asks a caller
    to agree with itself.

    What gives the page away is the one thing it cannot change: the name it
    asked for. The browser puts that in `Host`, so every request, reads
    included, has to be addressed to an authority this server actually answers
    for -- and `_is_our_own` works that out from the listening socket rather
    than from the request.

    The second check is the ordinary cross-site one, for a page that is honest
    about who it is. `/api/upload` is the route reachable that way: a multipart
    POST is a "simple" request, so a browser sends it across origins without
    asking permission first, while every other mutating route here takes a JSON
    body and is held back by the preflight it cannot answer. A browser sets
    `Origin` on every request that is not a plain read, and an origin is a
    scheme, a host and a port, so a page on another loopback port -- or on
    another loopback address, such as `http://127.0.0.2:8765` -- is another site
    however local it is. A request with no `Origin` is left alone: curl,
    the CLI and the macOS app's uploader are not a browser acting for somebody
    else's page, which is the only thing that check is for.

    Outermost of the middlewares, so a refused request is never counted as work
    in flight and never selects a workspace.
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
        # 403 rather than 421 Misdirected Request, which fits the wording but
        # invites an HTTP/2 client to retry on a fresh connection -- reaching
        # the same refusal, since the objection is to the request, not to the
        # route it took. Both refusals here say the same thing anyway: this came
        # from somewhere this server does not serve.
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


@_workspace_job
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


@_workspace_job
def _run_syntheses(job: dict, topics: list[str]) -> None:
    try:
        written = failed = 0
        last_failure = ""
        for index, topic in enumerate(topics, 1):
            _set(job, state="reading", detail=f"{index} of {len(topics)}: {topic}")
            # As with tensions: one topic's failure must not cost the topics
            # after it. Go on, and say how many did not finish.
            try:
                result = extract.synthesize_topic(topic)
            except Exception as exc:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exc()
                continue
            written += 1 if result["written"] else 0
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
        failed = 0
        last_failure = ""
        for index, key in enumerate(keys, 1):
            _set(job, state="reading", key=key, detail=f"{index} of {len(keys)}")
            # As with tensions and syntheses, and as the command line does: one
            # paper's refusal or bad answer must not cost the papers after it.
            # Retag all runs the whole corpus in a fixed order, so a single
            # early failure used to leave every later paper unretagged.
            try:
                extract.retag_paper(key)
            except Exception as exc:
                failed += 1
                last_failure = f"{key}: {type(exc).__name__}: {exc}"
                traceback.print_exc()
                continue
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


class SynthesesBody(BaseModel):
    topics: list[str] | None = None


class SynthesisTextBody(BaseModel):
    text: str


class PaperPatch(BaseModel):
    """The paper fields a person may correct, and the types they must hold.

    Every other write here goes through a model; this route took a bare dict
    and wrote whatever was in it. A year of `"2020"` was accepted as a string
    and then broke the export, which sorts papers by year and cannot compare a
    string with the numbers every other paper carries — a corpus wedged by one
    request, with no way back except editing the JSON by hand.

    Every field is optional and the caller's own fields are the only ones
    applied, so a patch stays a patch: `None` is a value to write (clearing a
    year), and an absent field is left alone.
    """

    title: str | None = None
    authors: list[str] | None = None
    year: int | None = None
    venue: str | None = None
    doi: str | None = None
    summary: str | None = None
    relevance: str | None = None
    notes: str | None = None


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
        jobs = sorted(
            (job for job in _jobs.values() if job.get("workspace") == config.workspace_id()),
            key=lambda j: j["id"], reverse=True,
        )[:20]
    return {
        "workspace": config.get_workspace(),
        "papers": [store.summarize(p) for p in papers],
        "claims": rows,
        "tags": store.load_tags(),
        "tag_counts": store.tag_counts(rows),
        "ledger": store.load_ledger(),
        "tensions": store.tension_rows(rows),
        "syntheses": store.synthesis_rows(rows),
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

    # The CLI turns this away first, with a usage message; this catches the
    # direct caller. Port 0 means "any free port" to uvicorn, which picks one
    # after `trust_bind` has already written the authority down -- so the
    # server would answer on a port nobody named while trusting a port nobody
    # is listening on, and every request from off this machine would be refused
    # with no way to work out why. Nor can the trust be repaired once the socket
    # exists: with a published `--host`, the operator has to know the port in
    # advance to reach the page at all.
    if not 1 <= port <= 65535:
        raise ValueError(
            f"cannot serve on port {port}: ports run 1-65535, and 0 asks the "
            "kernel for whichever happens to be free, leaving nobody -- this "
            "server included -- able to say where the page is"
        )
    config.ensure_dirs()
    trust_bind(host, port)
    os.environ[BIND_ENV] = f"{host}:{port}"
    if _bound_to_every_address and not _published_authorities:
        # Silence here would be the wrong kind: the operator asked to be
        # reachable from anywhere, and writes from anywhere but this machine
        # will be refused until they say what name the page is served under.
        print(
            f"warning: --host {host} publishes on every address, but the name a browser\n"
            f"         will use cannot be read off the socket and is not taken from the\n"
            f"         request. Only loopback is trusted until you set, for example,\n"
            f"         {PUBLISHED_ORIGINS_ENV}=http://this-machine.local:{port}"
        )
    uvicorn.run("doxograph.server:app" if reload else app, host=host, port=port, reload=reload)
