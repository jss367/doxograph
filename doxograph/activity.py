"""Workspace leases and the count of work this server has accepted."""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections.abc import Iterator

from fastapi import HTTPException
from starlette.types import ASGIApp, Receive, Scope, Send

from . import config, origins


_workspace_activity_lock = threading.RLock()
_workspace_activity: dict[tuple[str, str], int] = {}
_deleting_workspaces: set[tuple[str, str]] = set()


def _workspace_identity(value: str) -> tuple[str, str]:
    return (str(config.base_data_dir().resolve()), value)


@contextlib.contextmanager
def _using_workspace(value: str) -> Iterator[None]:
    """Keep deletion out of requests and the entire lifetime of worker calls."""
    identity = _workspace_identity(value)
    with contextlib.ExitStack() as leases:
        with _workspace_activity_lock:
            if identity in _deleting_workspaces:
                raise HTTPException(409, "This workspace is being deleted")
            try:
                leases.enter_context(config.workspace_activity(value))
            except BlockingIOError:
                raise HTTPException(409, "This workspace is busy or being deleted") from None
            except KeyError:
                raise HTTPException(404, f"no workspace {value}") from None
            if config.get_workspace(value) is None:
                raise HTTPException(404, f"no workspace {value}")
            _workspace_activity[identity] = _workspace_activity.get(identity, 0) + 1
        try:
            yield
        finally:
            with _workspace_activity_lock:
                remaining = _workspace_activity[identity] - 1
                if remaining:
                    _workspace_activity[identity] = remaining
                else:
                    del _workspace_activity[identity]


_arrivals_lock = threading.Lock()
_requests_arriving = 0

#: Everything this server has ever accepted, counted once each and never
#: decremented. Only goes up, so a reading of it is an identity for "the work
#: this server had taken on by then" in a way the gauges can never be: two
#: readings of `1 in flight` can be two different papers.
#:
#: Once each is the whole difficulty. A paper arrives as a request and becomes
#: a job, which are two moments in the life of one acceptance, and counting both
#: would move this number without anything new having been accepted — so the
#: launcher would ask again about a paper the user had already agreed to lose.
#: A request counts itself and says so; a job counts itself only when nothing
#: has counted it already, which is how the handful of jobs made by GETs are
#: not missed.
_work_taken = 0
_work_lock = threading.Lock()
#: Whether the request this job is being made inside has already been counted.
#: A `ContextVar` because the counting happens in ASGI middleware and the
#: counted-or-not has to reach a handler several frames down without being
#: passed through every one of them, and without leaking into the next request.
_request_counted: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "doxograph_request_counted", default=False)


def _take_work() -> None:
    global _work_taken
    with _work_lock:
        _work_taken += 1


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

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET") in origins.SAFE_METHODS:
            return await self.app(scope, receive, send)
        global _requests_arriving
        _request_counted.set(True)
        _take_work()
        with _arrivals_lock:
            _requests_arriving += 1
        try:
            await self.app(scope, receive, send)
        finally:
            with _arrivals_lock:
                _requests_arriving -= 1
