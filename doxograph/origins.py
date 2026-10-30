"""Which names and origins this server answers for, and refusing the rest."""

from __future__ import annotations

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse


#: Methods that cannot make work, and so are not counted. Everything else is.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


#: Carries the address `serve()` bound to into a `--reload` subprocess, which
#: re-imports the server instead of calling `serve()` again and would otherwise
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
