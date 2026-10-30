"""Background jobs: the registry, the worker pool, and what each job runs."""

from __future__ import annotations

import contextvars
import json
import functools
import os
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Concatenate

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, Response

from . import activity, config, extract, ingest, job_journal, store

router = APIRouter()


_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="doxograph")
_jobs: dict[int, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_job_counter = time.time_ns() // 1000  # Below JavaScript's integer limit; survives process restarts.
_job_recovery_issues: dict[str, list[str]] = {}


def _finish(job: dict[str, Any], key: str) -> None:
    """Close a job, saying so when the paper arrived without its PDF.

    A paper with no PDF cannot be read, and marking the job plainly done hides
    that: for a new paper the job leaves the strip entirely because its detail is
    empty. The CLI already counts this as a failure; the web UI now says it too.
    """
    if key and not store.pdf_path(key).exists():
        try:
            note = store.load_paper(key).get("error") or "no PDF available"
        except (KeyError, json.JSONDecodeError):
            note = "the paper was removed"
        _set(job, state="error", detail=f"no PDF stored ({note})",
             explanation="The paper's details were saved, but its PDF is unavailable. "
                         "Retry to check again, or upload the PDF yourself.")
        return
    explanation = ""
    source = job.get("_import", {})
    if source and store.needs_extraction(key):
        if not config.ai_enabled():
            explanation = "PDF saved. AI analysis is off, so no claims were extracted."
        elif not source.get("extract"):
            explanation = "PDF saved without analysis because Read on arrival was off."
    _set(job, state="done", explanation=explanation)
    _forget_import(job)


def _new_job(label: str, **fields: Any) -> dict[str, Any]:
    global _job_counter
    # A job made inside a request the middleware already counted is that
    # request's work arriving in its second form, not a second acceptance.
    # A job made by a GET has nothing to have counted it, so it counts itself.
    if not activity._request_counted.get():
        activity._take_work()
    with _jobs_lock:
        _job_counter += 1
        job: dict[str, Any] = {"id": _job_counter, "label": label, "state": "queued",
               "detail": "", "key": None, "started": store.now()}
        job["workspace"] = config.workspace_id()
        job["ai_provider"] = config.ai_provider()
        job.update(fields)
        job_journal.save(job)
        _jobs[job["id"]] = job
        return job


def _set(job: dict[str, Any], **fields: Any) -> None:
    with _jobs_lock:
        job.update(fields)
        job_journal.save(job)


def _new_import_job(label: str, do_extract: bool, **source: Any) -> dict[str, Any]:
    return _new_job(label, kind="import", retryable=True,
                    _import={**source, "extract": do_extract})


def _forget_import(job: dict[str, Any]) -> None:
    """Release retained upload bytes when a notification no longer needs them."""
    with _jobs_lock:
        source = job.pop("_import", {})
        if job.get("kind") == "import":
            job["retryable"] = False
        job_journal.save(job)
    if staged := source.get("staged"):
        staged.unlink(missing_ok=True)


def _close_import_session() -> None:
    """Leave retry inputs and the latest notification available after relaunch."""
    with activity._workspace_activity_lock, _jobs_lock:
        for job in _jobs.values():
            if (job.get("_import")
                    and activity._workspace_identity(job["workspace"]) not in activity._deleting_workspaces):
                try:
                    with config.use_workspace(job["workspace"]), activity._using_workspace(job["workspace"]):
                        job_journal.save(job)
                except HTTPException as exc:
                    if exc.status_code not in (404, 409):
                        raise


def _recover_jobs() -> None:
    global _job_counter
    try:
        workspaces = config.list_workspaces()
    except ValueError:
        workspaces = [{"id": config.DEFAULT_WORKSPACE_ID}]
    with activity._workspace_activity_lock, _jobs_lock:
        for workspace in workspaces:
            with config.use_workspace(workspace["id"]):
                issues: list[str] = []
                try:
                    # Browsing a healthy, read-only library needs no lock file.
                    if any((config.data_dir() / '.jobs').glob('*.json')):
                        with activity._using_workspace(workspace["id"]), store.document_lock("job-recovery"):
                            for job in job_journal.recover(issues):
                                if job["id"] not in _jobs:
                                    _jobs[job["id"]] = job
                                    _job_counter = max(_job_counter, job["id"])
                except OSError as exc:
                    issues.append(f'Previous imports could not be recovered: {exc}. Their saved inputs were left in place.')
                except HTTPException as exc:
                    if exc.status_code not in (404, 409):
                        raise
                _job_recovery_issues[str(config.data_dir())] = issues


def _import_failed(job: dict[str, Any], exc: Exception) -> None:
    if isinstance(exc, (httpx.TimeoutException, anthropic.APITimeoutError)):
        explanation = "The service took too long to respond. Retry to try again."
    elif isinstance(exc, (httpx.RequestError, anthropic.APIConnectionError)):
        explanation = "The service could not be reached. Check your connection, then retry."
    elif isinstance(exc, anthropic.AuthenticationError):
        explanation = "AI analysis could not sign in. Check the selected provider's credentials, then retry."
    elif isinstance(exc, (httpx.HTTPStatusError, anthropic.APIStatusError)):
        status = exc.response.status_code
        if status == 429:
            explanation = "The service is receiving too many requests. Wait a moment, then retry."
        elif status in (401, 403):
            if job["state"] == "reading":
                explanation = "AI analysis could not sign in. Check the selected provider's credentials, then retry."
            else:
                explanation = "The service refused access. You may need to download and upload the PDF yourself."
        elif status == 404:
            explanation = "The paper could not be found at this address. Check the reference, then retry."
        else:
            explanation = "The service could not complete the request. Wait a moment, then retry."
    elif isinstance(exc, ingest.InvalidPDFUpload):
        explanation = "This file is not a PDF. Upload a valid PDF to import this paper."
    elif isinstance(exc, ValueError):
        explanation = "The paper or PDF could not be read. Check the reference or file, then retry."
    elif isinstance(exc, ingest.PaperRemoved):
        explanation = "The paper was removed during import. Retry to add it again."
    elif job["state"] == "reading":
        explanation = "The PDF was saved, but AI analysis failed. Retry to try the analysis again."
    else:
        explanation = "The import could not be completed. Retry to try again."
    if isinstance(exc, ingest.InvalidPDFUpload):
        _forget_import(job)
    _set(job, state="error", detail=f"{type(exc).__name__}: {exc}", explanation=explanation)


def _prune_jobs() -> None:
    # Pruning visits notifications outside the calling worker's workspace.
    # Finish those writes before deletion, and skip a corpus being removed.
    with activity._workspace_activity_lock:
        _prune_workspace_jobs()


def _prune_workspace_jobs() -> None:
    with _jobs_lock:
        by_workspace: dict[str, list[dict[str, Any]]] = {}
        for job in _jobs.values():
            if job["state"] not in ("done", "error") or (job.get("interrupted") and job.get("retryable")):
                continue
            workspace = job.get("workspace", config.DEFAULT_WORKSPACE_ID)
            if activity._workspace_identity(workspace) in activity._deleting_workspaces:
                continue
            by_workspace.setdefault(workspace, []).append(job)
    for workspace, done in by_workspace.items():
        candidates = sorted(done, key=lambda item: item["id"])[:-40]
        if not candidates:
            continue
        try:
            with config.use_workspace(workspace), activity._using_workspace(workspace):
                # A refused lease must leave notifications and retry inputs
                # together, so a later pruning pass can actually clean them up.
                removed = []
                with _jobs_lock:
                    for job in candidates:
                        if (_jobs.get(job["id"]) is job
                                and job["state"] in ("done", "error")
                                and not (job.get("interrupted") and job.get("retryable"))):
                            removed.append(_jobs.pop(job["id"]))
                for job in removed:
                    _forget_import(job)
                    job_journal.remove(job)
        except HTTPException as exc:
            if exc.status_code not in (404, 409):
                raise


def _submit_job(function: Callable[..., None], job: dict[str, Any], *args: Any) -> Future[None]:
    """Capture accepted settings before a job waits in the outer worker pool."""
    with config.use_model_access():
        # Queue submission is the acceptance point for both selections.
        _set(job, ai_provider=config.ai_provider())
        context = contextvars.copy_context()
    workspace = job.get("workspace", config.workspace_id())
    usage = activity._using_workspace(workspace) if workspace != config.DEFAULT_WORKSPACE_ID else None
    if usage is not None:
        usage.__enter__()
    try:
        future = _pool.submit(functools.partial(context.run, function), job, *args)
    except Exception as exc:
        try:
            _set(job, state="error", detail=str(exc), explanation="The operation could not start. Retry to try again.")
        finally:
            if usage is not None:
                usage.__exit__(None, None, None)
        raise
    # Hold the lease while queued and until the entire callable returns, even
    # after its notification becomes terminal; cancellation releases it too.
    if usage is not None:
        future.add_done_callback(lambda _: usage.__exit__(None, None, None))
    return future


def _workspace_job[**P](
        function: Callable[Concatenate[dict[str, Any], P], None],
) -> Callable[Concatenate[dict[str, Any], P], None]:
    """Keep the workspace and model access selected when the job was queued."""
    @functools.wraps(function)
    @config.analysis_session
    def wrapped(job: dict[str, Any], /, *args: P.args, **kwargs: P.kwargs) -> None:
        with (
            config.use_workspace(job.get("workspace", config.DEFAULT_WORKSPACE_ID)),
            config.use_ai_provider(job.get("ai_provider", config.ai_provider())),
            activity._using_workspace(config.workspace_id()),
        ):
            return function(job, *args, **kwargs)
    return wrapped


# --- background work ------------------------------------------------------

@_workspace_job
def _run_ingest(job: dict[str, Any], ref: ingest.Ref, do_extract: bool) -> None:
    try:
        _set(job, state="fetching")
        key, created = ingest.ingest_ref(ref)
        recovered = not created and store.needs_extraction(key)
        _set(job, key=key, label=key,
             detail="" if created else ("PDF recovered" if recovered else "already in the corpus"))
        # Read whenever the paper has a PDF and no claims, rather than only when
        # it was just created: a recovered download needs reading too.
        if do_extract and config.ai_enabled() and store.needs_extraction(key):
            _set(job, state="reading")
            extract.extract_paper(key)
        _finish(job, key)
    except Exception as exc:
        _import_failed(job, exc)
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_upload(job: dict[str, Any], staged: Path, filename: str, do_extract: bool) -> None:
    attempt = staged
    try:
        _set(job, state="fetching")
        # The ingester consumes its input, including on failure. Keep the
        # original for Retry, and give each attempt a disposable hard link.
        if job.get("_import"):
            attempt = staged.with_name(f".incoming-attempt-{uuid.uuid4().hex}.pdf")
            os.link(staged, attempt)
        key, created = ingest.ingest_staged_pdf(attempt, filename)
        _set(job, key=key, label=key, detail="" if created else "already in the corpus")
        if do_extract and config.ai_enabled() and store.needs_extraction(key):
            _set(job, state="reading")
            extract.extract_paper(key)
        _finish(job, key)
    except Exception as exc:
        _import_failed(job, exc)
        traceback.print_exc()
    finally:
        if attempt != staged:
            attempt.unlink(missing_ok=True)
        _prune_jobs()


@_workspace_job
def _run_extract(job: dict[str, Any], key: str, keep_reviewed: bool) -> None:
    try:
        _set(job, state="reading", key=key)
        extract.extract_paper(key, keep_reviewed=keep_reviewed)
        _set(job, state="done")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


def _finish_pass(job: dict[str, Any], total: int, unit: str, failed: int, last_failure: str, summary: str) -> None:
    if failed:
        _set(job, state="error", detail=f"{failed} of {total} {unit} failed, {summary}; {last_failure}")
    else:
        _set(job, state="done", detail=f"{total} {unit}, {summary}")


@_workspace_job
def _run_tensions(job: dict[str, Any], topics: list[str], force: bool = False) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        added = reopened = skipped = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.find_tensions(t, rows, tags, force=force)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                added += result["added"]
                reopened += result["reopened"]
                skipped += 1 if result.get("skipped") else 0
            _set(job, detail=f"{done} of {len(topics)} topics")
        summary = (f"{added} new" + (f", {reopened} reopened" if reopened else "")
                   + (f", {skipped} unchanged" if skipped else ""))
        _finish_pass(job, len(topics), "topics", failed, last_failure, summary)
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_agreements(job: dict[str, Any], topics: list[str], force: bool = False) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        added = grown = skipped = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.find_agreements(t, rows, tags, force=force)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                added += result["added"]
                grown += result["grown"]
                skipped += 1 if result.get("skipped") else 0
            _set(job, detail=f"{done} of {len(topics)} topics")
        summary = (f"{added} new" + (f", {grown} grown" if grown else "")
                   + (f", {skipped} unchanged" if skipped else ""))
        _finish_pass(job, len(topics), "topics", failed, last_failure, summary)
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_syntheses(job: dict[str, Any], topics: list[str], force: bool = False) -> None:
    try:
        rows, tags = store.claim_rows(), store.load_tags()
        written = skipped = failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(topics)} topics")
        for topic, result, exc in extract.run_concurrently(
                topics, lambda t: extract.synthesize_topic(t, rows, tags, force=force)):
            done += 1
            if exc is not None:
                failed += 1
                last_failure = f"{topic}: {type(exc).__name__}: {exc}"
                traceback.print_exception(exc)
            else:
                written += 1 if result["written"] else 0
                skipped += 1 if result.get("skipped") else 0
            _set(job, detail=f"{done} of {len(topics)} topics")
        # The topics nothing had changed in are said either way: a pass that
        # failed somewhere still did not ask about those, and a summary that
        # left them out would read as though it had.
        unchanged = f", {skipped} unchanged" if skipped else ""
        if failed:
            _set(job, state="error",
                 detail=(f"{failed} of {len(topics)} topics failed, "
                         f"{written} written{unchanged}; {last_failure}"))
        else:
            _set(job, state="done", detail=f"{written} of {len(topics)} topics written{unchanged}")
    except Exception as exc:
        _set(job, state="error", detail=f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
    finally:
        _prune_jobs()


@_workspace_job
def _run_retag(job: dict[str, Any], keys: list[str]) -> None:
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


@_workspace_job
def _run_fill_facets(job: dict[str, Any], keys: list[str], force: bool = False) -> None:
    try:
        failed = done = 0
        last_failure = ""
        _set(job, state="reading", detail=f"0 of {len(keys)}")
        for key, _result, exc in extract.run_concurrently(keys, lambda key: extract.fill_facets(key, force=force)):
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


ACTIVE_JOB_STATES = ("queued", "fetching", "reading")


def _require_analysis() -> None:
    if not config.ai_enabled():
        raise HTTPException(403, "AI analysis is disabled. Enable it in Settings to run analysis.")


@router.get("/api/jobs")
def jobs() -> dict[str, Any]:
    """The recent background jobs in this workspace, newest first."""
    with _jobs_lock:
        recent = sorted(
            (job for job in _jobs.values() if job.get("workspace") == config.workspace_id()),
            key=lambda j: j["id"], reverse=True,
        )[:20]
        # Retry sources include local paths and Ref objects; only notification
        # fields belong on the wire.
        recent = [{k: v for k, v in job.items() if not k.startswith("_")} for job in recent]
    return {"jobs": recent, "recovery_issues": _job_recovery_issues.get(str(config.data_dir()), [])}


@router.post("/api/jobs/{job_id}/retry")
def retry_import(job_id: int) -> dict[str, Any]:
    """Retry a failed import in place, once, in its original workspace."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or job.get("workspace") != config.workspace_id():
            raise HTTPException(404, "Job not found")
        if job["state"] != "error":
            raise HTTPException(409, "Only failed imports can be retried")
        source = job.get("_import")
        if not source:
            raise HTTPException(409, "This job cannot be retried. Add the paper again.")
        previous = dict(job)
        try:
            job.update(state="queued", detail="", explanation="", started=store.now(), ai_provider=config.ai_provider(), interrupted=False)
            job_journal.save(job)
        except OSError as exc:
            job.clear()
            job.update(previous)
            raise HTTPException(503, "The retry could not be saved. Check that the data directory is writable.") from exc
    try:
        if "ref" in source:
            _submit_job(_run_ingest, job, source["ref"], source["extract"])
        else:
            _submit_job(_run_upload, job, source["staged"], source["filename"], source["extract"])
    except Exception as exc:
        with _jobs_lock:
            job.clear()
            job.update(previous)
            job_journal.save(job)
        raise HTTPException(503, "The retry could not start. Please try again.") from exc
    return {"id": job_id}


@router.delete("/api/jobs/{job_id}", status_code=204)
def dismiss_job(job_id: int) -> Response:
    """Dismiss a finished notification without cancelling background work."""
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or job.get("workspace") != config.workspace_id():
            raise HTTPException(404, "Job not found")
        if job["state"] not in ("done", "error"):
            raise HTTPException(409, "This job is still running")
        del _jobs[job_id]
    _forget_import(job)
    job_journal.remove(job)
    return Response(status_code=204)
