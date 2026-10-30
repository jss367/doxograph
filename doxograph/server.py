"""Local web app: drop papers in, review the claims that come back out."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import (__version__, activity, analysis_api, backups, bib, chat_api, chats, codex_cli, config, export,
               fingerprint, job_journal, job_queue, notebook, origins, papers_api, query, records,
               research_notes, storage, store, workspace_archive)
from .model_errors import ModelSetupError

logger = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"

# Read what this process has imported, now, while importing the app module.
# This is the hook point rather than `serve()` because not every serving
# process goes through `serve()`: `--reload` hands uvicorn the string
# "doxograph.server:app", and the worker it spawns imports this module
# directly. A worker that skipped this would take its first reading at the
# first `/api/code`, by which time a dependency upgraded under it would be
# remembered as the version it is running.
fingerprint.snapshot()

@contextlib.asynccontextmanager
async def _lifespan(_: FastAPI):
    """Read the dependencies once more, now that the server is fully loaded.

    `import uvicorn` is not the end of Uvicorn's importing: `uvicorn.run` builds
    a `Config` and loads it, which imports the event loop and HTTP protocol
    implementations it was configured with — h11 and websockets here. Those
    arrive after the snapshot beside the import and before this, which runs once
    the server is built and before it accepts anything.

    Every serving process gets here, including the `--reload` worker, which
    never calls `serve()` at all.
    """
    fingerprint.snapshot()
    job_queue._recover_jobs()
    try:
        yield
    finally:
        job_queue._close_import_session()


app = FastAPI(title="Doxograph", lifespan=_lifespan)
app.include_router(notebook.router)
app.include_router(research_notes.router)
app.include_router(chats.router)
app.include_router(query.router)
app.include_router(backups.router)
app.include_router(workspace_archive.router)
app.include_router(job_queue.router)
app.include_router(papers_api.router)
app.include_router(chat_api.router)
app.include_router(analysis_api.router)
app.mount("/vendor", StaticFiles(directory=STATIC / "vendor"), name="reader-assets")


@app.exception_handler(store.StorageRecoveryError)
async def storage_recovery_error(request: Request, exc: store.StorageRecoveryError):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(store.PaperReadError)
async def paper_read_error(request: Request, exc: store.PaperReadError):
    return JSONResponse({"detail": str(exc)}, status_code=500)


@app.exception_handler(store.TopicConflictError)
async def topic_conflict_error(request: Request, exc: store.TopicConflictError):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(storage.StorageReadError)
async def storage_read_error(request: Request, exc: storage.StorageReadError):
    return JSONResponse({"detail": str(exc)}, status_code=503)


def _broken_registry(exc: ValueError) -> str:
    """Say what is wrong with the registry and what to do about it, since only
    a person can: the file is never rewritten over what it holds."""
    return (f"{exc}. Fix or move aside {config.workspaces_path()} to reach any "
            "workspace but the default.")


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
            "/", "/app.css", "/app.js", "/appearance.js", "/paper-map.js",
            "/workspace-client.js", "/workspace-controller.js", "/poller.js",
            "/favicon.png", "/api/health", "/api/code",
            "/api/workspaces",
        }:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        selected = headers.get(b"x-doxograph-workspace", b"").decode("utf-8", "replace")
        if not selected:
            query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
            selected = (query.get("workspace") or [config.DEFAULT_WORKSPACE_ID])[0]
        # Deletion takes an exclusive lease in config.delete_workspace. Taking
        # a shared request lease here would make it conflict with itself.
        if scope.get("method") == "DELETE" and re.fullmatch(r"/api/workspaces/[^/]+", scope.get("path", "")):
            with config.use_workspace(selected):
                return await self.app(scope, receive, send)
        try:
            # Acquire before validation so deletion cannot slip between the
            # registry lookup and the first read or write of corpus files.
            usage = activity._using_workspace(selected)
            usage.__enter__()
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            return await response(scope, receive, send)
        except ValueError as exc:
            # The default tolerates a broken registry and keeps its corpus
            # accessible under the built-in display name.
            response = JSONResponse({"detail": _broken_registry(exc)}, status_code=500)
            return await response(scope, receive, send)
        try:
            with config.use_workspace(selected):
                return await self.app(scope, receive, send)
        finally:
            usage.__exit__(None, None, None)


app.add_middleware(activity.CountArrivingRequests)
app.add_middleware(SelectWorkspace)
# Added last, so it wraps the other two and turns a cross-site request away
# before either of them sees it.
app.add_middleware(origins.RejectCrossSiteRequests)


class SettingsBody(BaseModel):
    model_config = {"extra": "forbid"}
    ai_enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    base_url: str | None = None
    codex_model: str | None = Field(default=None, max_length=200)


class ProviderBody(BaseModel):
    ai_provider: Literal["api", "claude_code", "codex"]


class ExportBody(BaseModel):
    # No `path`: the page never sent one, and a request naming any file on disk
    # to create and overwrite is more than the page needs. The CLI still takes
    # `--out`; over HTTP the file goes where the workspace keeps its export.
    title: str = "Doxograph"


class WorkspaceBody(BaseModel):
    name: str


# --- routes ---------------------------------------------------------------

def _frontend_response(name: str, media_type: str) -> FileResponse:
    # WebKit must not reuse one module from an earlier installation while
    # loading app.js from the current one.
    return FileResponse(STATIC / name, media_type=media_type,
                        headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    source = (STATIC / "index.html").read_text(encoding="utf-8")
    assets = sorted([*STATIC.glob("*.js"), *STATIC.glob("*.css")])
    digest = hashlib.sha256(source.encode())
    for asset in assets:
        digest.update(asset.name.encode())
        digest.update(asset.read_bytes())
    version = digest.hexdigest()[:20]
    # A new URL also bypasses previously cached responses that predate the
    # no-store policy. Vendor assets keep their existing URLs.
    source = re.sub(r'((?:src|href)="/)([\w-]+\.(?:js|css))"',
                    lambda match: f'{match[1]}{match[2]}?v={version}"', source)
    return HTMLResponse(source, headers={"Cache-Control": "no-store"})


def _serve_frontend_asset(asset: Path) -> None:
    media_type = "text/css" if asset.suffix == ".css" else "text/javascript"

    def serve() -> FileResponse:
        return _frontend_response(asset.name, media_type)

    app.add_api_route(f"/{asset.name}", serve, methods=["GET"], include_in_schema=False)


# Every script and stylesheet the shell loads, so a new one needs no route.
for _asset in sorted([*STATIC.glob("*.js"), *STATIC.glob("*.css")]):
    _serve_frontend_asset(_asset)


@app.get("/favicon.png")
def favicon() -> FileResponse:
    return FileResponse(STATIC / "favicon.png", media_type="image/png")


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

    `instance` says which process is answering. The launcher reads this and
    `/api/code` in separate requests and a port can change hands between them,
    so without it one server's work counts could be paired with another's code
    digest and the two would describe a server that never existed.

    `taken` is everything this server has ever accepted, counted once each and
    never decremented. The launcher needs it because the gauges cannot say
    whether work is the *same* work: an alert saying "1 paper in flight" can be
    answered ten minutes later by a server still reporting one paper, and it can
    be a different paper, and nobody agreed to lose that one. Two readings of
    `taken` that agree are the same work; two that differ are not — which is why
    a paper must not be counted once as a request and again as a job.
    """
    with activity._arrivals_lock:
        arriving = activity._requests_arriving
    with job_queue._jobs_lock:
        jobs = sum(1 for job in job_queue._jobs.values() if job["state"] in job_queue.ACTIVE_JOB_STATES)
    with activity._work_lock:
        taken = activity._work_taken
    return {
        "app": "doxograph",
        "version": __version__,
        "instance": fingerprint.INSTANCE,
        "busy": jobs + arriving,
        "jobs": jobs,
        "arriving": arriving,
        "taken": taken,
    }


@app.get("/api/code")
def code() -> dict:
    """Whether this server is still running the code that is on disk.

    `/api/health` reports a release number, and a release number is coarser than
    the question the macOS launcher is really asking. `Info.plist` and
    `doxograph/__init__.py` move together and only on a release, so a server
    orphaned in the middle of one reports exactly what the app reports, is
    adopted on the strength of it, and goes on serving Python from before every
    commit since. What the launcher wants to know is narrower and more useful:
    would restarting this server change what it runs.

    `instance` is here for the same reason it is on `/api/health`: it is the one
    thing that says this answer and that one came from the same process.

    `running` is the digest taken while this process was importing its modules.
    `onDisk` is the same digest taken now. They differ exactly when the files
    have been edited, pulled or reinstalled underneath a process that had
    already loaded them. Either can come back empty, from a package this process
    cannot read, and an empty side means unknown rather than unchanged — so a
    caller should conclude nothing unless it has both.

    Both halves of what this server runs go in: its own sources, and the files
    behind everything it imported from outside itself. `pip install -e .` on a
    changed `pyproject.toml` upgrades the second without touching the first.
    The second half is read fresh each time rather than from a list fixed at
    import, because a process goes on importing: Uvicorn arrives after this
    module is done, and pypdf only when a PDF does.

    Kept off `/api/health` on purpose. That one is polled four times a second
    during startup and asked again on quit, and its promise is that it costs
    nothing; this reads every source file in the package. The launcher needs it
    once, on the one port that answered.
    """
    running, on_disk = fingerprint.report()
    return {
        "app": "doxograph",
        "version": __version__,
        "instance": fingerprint.INSTANCE,
        "running": running,
        "onDisk": on_disk,
    }


#: The last `/api/state` answer per workspace, with the corpus signature it
#: was built from. The page polls twice a second and the corpus changes far
#: less often than that; the signature is a directory listing, the answer is
#: every paper file read and joined.
_state_cache: dict[str, tuple[str, dict]] = {}
_state_cache_lock = threading.Lock()


def _state_section[T](read: Callable[[], T], fallback: T,
                      issues: list[records.StorageIssue]) -> T:
    """Keep healthy research visible; strict readers still guard every write."""
    try:
        return read()
    except storage.StorageReadError as exc:
        issues.append({"path": str(exc.path), "detail": str(exc)})
        return fallback


def _build_state() -> dict:
    issues: list[records.StorageIssue] = []
    papers = store.all_papers(issues=issues)
    rows = store.claim_rows(papers)
    saved_notebook = _state_section(notebook.load, notebook.Notebook().model_dump(), issues)
    tensions = _state_section(lambda: store.tension_rows(rows), [], issues)
    agreements = _state_section(lambda: store.agreement_rows(rows), [], issues)
    syntheses = _state_section(lambda: store.synthesis_rows(rows, tensions=tensions), [], issues)
    tags = _state_section(store.load_tags, [], issues)
    ledger = _state_section(store.load_ledger, [], issues)
    context = _state_section(store.load_context, "", issues)
    _state_section(store.retired_keys, set(), issues)
    if any(issue["path"] == str(store.tensions_path()) for issue in issues):
        # Their text is still useful, but freshness cannot be established while
        # the judgments that contributed to it are unavailable.
        for synthesis in syntheses:
            synthesis["stale"] = True
    return {
        "workspace": config.get_workspace(),
        "notebook": saved_notebook,
        "storage_issues": issues,
        "papers": [store.summarize(p) for p in papers],
        "claims": rows,
        "tags": tags,
        "tag_counts": store.tag_counts(rows),
        "label_counts": store.label_counts(papers),
        "label_keys": store.label_keys(papers),
        "ledger": ledger,
        "context": context,
        "tensions": tensions,
        "agreements": agreements,
        "syntheses": syntheses,
        "tension_kinds": store.TENSION_KINDS,
        "tension_statuses": store.TENSION_STATUSES,
        "kinds": config.CLAIM_KINDS,
        "strengths": config.CLAIM_STRENGTHS,
        "relations": config.LEDGER_RELATIONS,
        "data_dir": str(config.data_dir()),
        **config.analysis_settings(),
        "model": config.model(),
        "ai_provider": config.ai_provider(),
        "codex_model": config.codex_model(),
        "ai_enabled": config.ai_enabled(),
    }


@app.get("/api/state")
def state(request: Request) -> Response:
    """The corpus as the page shows it. Jobs are not in here; they change
    every second while a paper is read and have their own route.

    The answer carries the workspace and the corpus signature as its ETag,
    and a request that presents the same one back gets a 304 with no body.
    The workspace is in it because the signature is of the corpus files
    alone, and two empty workspaces have the same one.
    """
    workspace = config.workspace_id()
    recovery_error = None
    try:
        store.recover_storage()
    except store.StorageRecoveryError as exc:
        recovery_error = str(exc)
    # Credentials and installation settings live outside the corpus, but
    # changes to either must reach every workspace on the next poll.
    ai_enabled = config.ai_enabled()
    settings_signature = hashlib.sha256(json.dumps(
        {**config.analysis_settings(), "codex_model": config.codex_model()}, sort_keys=True).encode()).hexdigest()
    workspace_record = config.get_workspace()
    name_signature = hashlib.sha1(workspace_record["name"].encode()).hexdigest()
    signature = f"{store.corpus_signature()}-{settings_signature}-{int(ai_enabled)}-{config.ai_provider()}-{name_signature}"
    with _state_cache_lock:
        cached = _state_cache.get(workspace)
    # Retry unreadable files even when their stat is unchanged: a temporary
    # I/O failure or an ACL repair need not change content or its timestamp.
    if (recovery_error is not None or cached is None or cached[0] != signature
            or cached[1].get("storage_issues")):
        payload = _build_state()
        # Match the signature even if the switch changed during the build.
        payload["ai_enabled"] = ai_enabled
        payload["workspace"] = workspace_record
        if recovery_error is not None:
            payload["storage_issues"].append({"path": str(store.tag_change_path()), "detail": recovery_error})
        else:
            with _state_cache_lock:
                _state_cache[workspace] = (signature, payload)
    else:
        payload = cached[1]
    issues_signature = hashlib.sha1(json.dumps(payload["storage_issues"], sort_keys=True).encode()).hexdigest()
    etag = f'"{workspace}-{signature}-{issues_signature}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(payload, headers={"ETag": etag})


@app.put("/api/settings")
def update_settings(body: SettingsBody) -> dict:
    if body.codex_model is not None:
        try:
            config.save_codex_model(body.codex_model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    changing_provider = any(value is not None for value in (body.provider, body.model, body.base_url))
    if changing_provider:
        current = config.analysis_config()
        provider = body.provider if body.provider is not None else current.provider
        defaults = config.analysis_settings()["providers"].get(provider, {})
        try:
            config.save_analysis_settings(
                provider,
                body.model if body.model is not None else defaults.get("model", ""),
                body.base_url if body.base_url is not None else defaults.get("base_url", ""),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if body.ai_enabled is not None:
        config.set_ai_enabled(body.ai_enabled)
    if changing_provider:
        return {"ai_enabled": config.ai_enabled(), **config.analysis_settings()}
    if body.codex_model is not None:
        return {"ai_enabled": config.ai_enabled(), "codex_model": config.codex_model(), "model": config.model()}
    return {"ai_enabled": config.ai_enabled()}


@app.get("/api/settings/codex/models")
def codex_models() -> dict:
    try:
        return {"models": codex_cli.models()}
    except (ModelSetupError, RuntimeError) as exc:
        raise HTTPException(503, str(exc)) from exc


@app.put("/api/settings/provider")
def update_provider(body: ProviderBody) -> dict:
    config.set_ai_provider(body.ai_provider)
    return {"ai_provider": config.ai_provider()}


@app.get("/api/workspaces")
def workspaces() -> dict:
    try:
        return {"workspaces": config.list_workspaces()}
    except ValueError as exc:
        raise HTTPException(500, _broken_registry(exc)) from exc


@app.post("/api/workspaces", status_code=201)
def create_workspace(body: WorkspaceBody) -> dict:
    try:
        workspace = config.create_workspace(body.name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"workspace": workspace, "workspaces": config.list_workspaces()}


@app.patch("/api/workspaces/{workspace_id}")
def rename_workspace(workspace_id: str, body: WorkspaceBody) -> dict:
    try:
        workspace = config.rename_workspace(workspace_id, body.name)
    except KeyError:
        raise HTTPException(404, f"no workspace {workspace_id}") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"workspace": workspace, "workspaces": config.list_workspaces()}


@app.delete("/api/workspaces/{workspace_id}")
def delete_workspace(workspace_id: str) -> dict:
    if workspace_id == config.DEFAULT_WORKSPACE_ID:
        raise HTTPException(422, "The default workspace cannot be deleted")
    identity = activity._workspace_identity(workspace_id)
    with activity._workspace_activity_lock, job_queue._jobs_lock:
        # Deletion requests do not take shared leases themselves. Every other
        # request and any queued job must settle before taking the exclusive one.
        if (identity in activity._deleting_workspaces
                or activity._workspace_activity.get(identity, 0) > 0
                or any(job.get("workspace") == workspace_id and job["state"] not in ("done", "error")
                       for job in job_queue._jobs.values())):
            raise HTTPException(409, "This workspace is busy. Wait for its imports and other activity to finish, then try again.")
        activity._deleting_workspaces.add(identity)
    try:
        try:
            cleanup_allowed = config.delete_workspace(workspace_id)
        except KeyError:
            raise HTTPException(404, f"no workspace {workspace_id}") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except BlockingIOError:
            raise HTTPException(409, "This workspace is busy in another server. Close that server or wait for its activity to finish, then try again.") from None
        except OSError as exc:
            raise HTTPException(503, "The workspace could not be deleted. Check that its data directory is writable.") from exc
        with job_queue._jobs_lock:
            for key in [key for key, job in job_queue._jobs.items() if job.get("workspace") == workspace_id]:
                job = job_queue._jobs.pop(key)
                try:
                    job_journal.remove(job, keep_file=not cleanup_allowed)
                except OSError:
                    # Retirement is already committed. Cleanup must not turn
                    # that success into a failure that strands the old UI.
                    logger.exception("Could not remove journal for deleted workspace job %s", key)
        with _state_cache_lock:
            _state_cache.pop(workspace_id, None)
        job_queue._job_recovery_issues.pop(str(config.base_data_dir() / "workspaces" / workspace_id), None)
        return {"workspaces": config.list_workspaces()}
    finally:
        with activity._workspace_activity_lock:
            activity._deleting_workspaces.discard(identity)


@app.post("/api/export")
def api_export(body: ExportBody) -> dict:
    path = export.write(title=body.title)
    return {"path": str(path)}


@app.get("/export")
def serve_export() -> FileResponse:
    """Hand back the HTML this workspace last exported, so the page can offer
    to open it instead of naming a path the reader has to go and find."""
    path = config.export_path()
    if not path.exists():
        raise HTTPException(404, "nothing has been exported from this workspace yet")
    return FileResponse(path, media_type="text/html", filename=path.name,
                        content_disposition_type="inline")


@app.get("/api/bibtex", response_class=PlainTextResponse)
def api_bibtex() -> PlainTextResponse:
    return PlainTextResponse(bib.render(), media_type="text/plain")


def serve(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    import uvicorn

    # Read now, beside the import, and not at the next `/api/code`. Uvicorn is
    # the HTTP server itself and arrives after this module finished importing,
    # so a reading taken later could be of a version installed after this
    # process loaded the one it is running. The `--reload` worker gets Uvicorn
    # from the module-level snapshot instead, having imported it before this
    # module rather than after.
    fingerprint.snapshot()

    # The CLI refuses this first; this catches a direct caller. Port 0 means
    # "any free port", chosen after `trust_bind` has recorded the authority, so
    # the server would trust a port nobody listens on and refuse everything.
    if not 1 <= port <= 65535:
        raise ValueError(
            f"cannot serve on port {port}: ports run 1-65535, and 0 asks the "
            "kernel for whichever is free, so nobody can say where the page is"
        )
    config.ensure_dirs()
    origins.trust_bind(host, port)
    os.environ[origins.BIND_ENV] = f"{host}:{port}"
    if origins._bound_to_every_address and not origins._published_authorities:
        # Writes from anywhere but this machine will be refused until the
        # operator says what name the page is served under.
        print(
            f"warning: --host {host} publishes on every address, but the name a browser\n"
            f"         will use cannot be read off the socket and is not taken from the\n"
            f"         request. Only loopback is trusted until you set, for example,\n"
            f"         {origins.PUBLISHED_ORIGINS_ENV}=http://this-machine.local:{port}"
        )
    uvicorn.run("doxograph.server:app" if reload else app, host=host, port=port, reload=reload)
