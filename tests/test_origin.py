"""Requests from other sites, and requests addressed to other names, are refused."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from doxograph import __main__, config, server, store


# --- a page on another site cannot post into the corpus -------------------
#
# A multipart POST is a "simple" request, so a browser sends it across origins
# with no preflight to stop it: any page in any tab could drop a PDF into the
# corpus of a running server. Everything else here takes a JSON body and is
# held back by a preflight it cannot answer.

@pytest.fixture
def queued_jobs(monkeypatch):
    """The work a request queued, without letting the pool run any of it.

    These two routes reach the network — arXiv, then the model — as soon as
    their job starts, so a check that they are refused must not depend on being
    refused to stay offline.
    """
    submitted: list[tuple] = []
    monkeypatch.setattr(server._pool, "submit", lambda *args, **kwargs: submitted.append(args))
    return submitted


def test_a_cross_site_upload_is_refused(queued_jobs):
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "https://evil.example.com"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a page on another site queued an upload"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_a_cross_site_json_post_is_refused(queued_jobs):
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/ingest", json={"text": "2501.00001"},
                               headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert queued_jobs == []


def test_the_pages_own_requests_are_allowed():
    """The app posts from the page the server served, so its origin is the host."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"title": "A Better Study"},
                                headers={"Origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200


def test_a_request_with_no_origin_is_allowed():
    """curl, the CLI and the macOS app's uploader are not a browser acting for
    somebody else's page, which is the only thing the check is for."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"}).status_code == 200


def test_a_cross_site_read_with_an_honest_host_is_left_alone():
    """A page that asked for this server by its real name cannot read the reply.

    `Origin` says another site sent this, but `Host` says the browser resolved
    `127.0.0.1` itself, which is the same-origin policy's own case: the response
    goes nowhere the sender can see it. Only a rebound `Host` escapes that, and
    the check below is what catches it.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health",
                          headers={"Origin": "https://evil.example.com"}).status_code == 200


# --- the same-origin test cannot be made of the request's own headers -----
#
# Comparing `Origin` against `Host` only asks a caller to agree with itself. A
# page on a hostname whose DNS is rebound to 127.0.0.1 reaches this server with
# both headers set to that hostname, so the comparison passes and the check
# that exists to stop it waves it through. Trust comes from where the server
# listens instead, which the caller cannot influence.
#
# And rebinding is not stopped by the same-origin policy either -- defeating it
# is the whole trick. The browser believes `evil.example.com` and this server
# are one origin, so the page reads every reply it gets. That is why `Host` is
# checked on reads too, and not only on the writes.

@pytest.fixture
def bound_nowhere_in_particular(monkeypatch):
    """The default: nothing published beyond the loopback interface."""
    monkeypatch.setattr(server, "_published_authorities", frozenset())
    monkeypatch.setattr(server, "_bound_to_every_address", False)


def test_a_rebound_hostname_cannot_pose_as_the_page(queued_jobs, bound_nowhere_in_particular):
    """`Host` says what the attacker wants it to say, so it cannot be evidence."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://evil.example.com", "Host": "evil.example.com"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a rebound hostname queued an upload"


def test_a_rebound_hostname_cannot_delete_a_paper(bound_nowhere_in_particular):
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.request(
            "DELETE", "/api/papers/doe2026study",
            headers={"Origin": "http://evil.example.com", "Host": "evil.example.com"},
        )
    assert response.status_code == 403
    assert store.load_paper("doe2026study")["title"] == "A Study"


def test_a_rebound_hostname_cannot_read_the_corpus(bound_nowhere_in_particular):
    """The rebound page is same-origin to the browser, so it reads the answer.

    Which is the whole reason a read has to be checked: refusing only the writes
    leaves every title, tag and note in the corpus readable by any page whose
    DNS points here. The request carries no `Origin` -- a plain `GET` never does
    -- so `Host` is the only thing there is to go on.
    """
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/state", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert "A Study" not in response.text


def test_a_rebound_hostname_cannot_read_a_pdf(bound_nowhere_in_particular):
    """The papers themselves, not just what the corpus says about them."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4 the paper itself")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/pdf/doe2026study", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert b"the paper itself" not in response.content


def test_every_spelling_of_the_loopback_page_is_allowed(bound_nowhere_in_particular):
    """The browser may have been pointed at any of these; all are this machine."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
            response = client.patch("/api/papers/doe2026study", json={"notes": origin},
                                    headers={"Origin": origin})
        assert response.status_code == 200, origin


def test_a_hostname_that_merely_contains_localhost_is_refused(bound_nowhere_in_particular):
    """`localhost.evil.example.com` is a name the attacker owns, not this machine."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": "http://localhost.evil.example.com"})
    assert response.status_code == 403


def test_another_page_on_this_machine_is_still_another_site(queued_jobs,
                                                            bound_nowhere_in_particular):
    """An origin is a scheme, a host *and a port*.

    Whatever else is running on this machine -- a dev server on 3000, something
    a package script started -- is not this app, and a page it serves can post a
    multipart form here without a preflight to stop it. It cannot read the
    reply, but enqueueing an upload needs no reply.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://localhost:3000"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a page on another loopback port queued an upload"


def test_a_request_addressed_to_another_port_is_refused(bound_nowhere_in_particular):
    """The port comes off the listening socket, so `Host` cannot talk it round."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health", headers={"Host": "127.0.0.1:9999"}).status_code == 403


def test_the_address_serve_published_is_trusted(bound_nowhere_in_particular):
    """`serve --host` is documented, so the page it serves has to keep working."""
    server.trust_bind("192.168.1.5", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.5:8765",
                                     "Host": "192.168.1.5:8765"}).status_code == 200
        # Neither another address on the same network nor the same address on
        # another port is where this page came from.
        for origin in ("http://192.168.1.6:8765", "http://192.168.1.5:9999"):
            assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": origin}).status_code == 403, origin
        # And a name that is not published is refused before the origin is even
        # looked at, whatever the page claims about itself.
        assert client.get("/api/state", headers={"Host": "192.168.1.6:8765"}).status_code == 403


def test_a_wildcard_bind_trusts_nothing_the_request_says(bound_nowhere_in_particular):
    """`--host 0.0.0.0` answers on every address, and names none of them.

    The address the browser typed cannot be read off the socket, and the one
    place it is written down -- the request -- is the one place a rebound page
    controls. So matching `Origin` against `Host` is not a fallback here; it is
    the very comparison this whole check exists to refuse.
    """
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": "http://evil.example.com",
                                         "Host": "evil.example.com"})
    assert response.status_code == 403
    assert store.load_paper("doe2026study").get("notes") != "n"


def test_a_wildcard_bind_still_serves_the_operators_own_machine(bound_nowhere_in_particular):
    """Loopback keeps working, so the app on the host itself is unaffected."""
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_a_wildcard_bind_trusts_the_name_the_operator_published(monkeypatch,
                                                                bound_nowhere_in_particular):
    """Someone has to say what the page is served as; only the operator can."""
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "http://192.168.1.5:8765")
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.5:8765",
                                     "Host": "192.168.1.5:8765"}).status_code == 200
        # Everything else on that network is still somebody else.
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.6:8765",
                                     "Host": "192.168.1.6:8765"}).status_code == 403


def test_a_published_loopback_name_beats_the_bound_port_rule(monkeypatch,
                                                             bound_nowhere_in_particular):
    """A TLS terminator on `localhost:443` in front of a backend on 8765.

    The loopback rule compares against the port the socket reports, which is
    the backend's, so the published name fails it -- and the operator's own
    `PUBLISHED_ORIGINS_ENV` has to be consulted first, or it is accepted at
    startup and then quietly ignored on every request.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "https://localhost, http://localhost")
    server.trust_bind("127.0.0.1", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "https://localhost"}).status_code == 200
        # And the same name as the `Host` the proxy passed along, which carries
        # no scheme and so is read against the request's own.
        assert client.get("/api/state", headers={"Host": "localhost"}).status_code == 200


def test_an_unpublished_loopback_port_is_still_refused(monkeypatch,
                                                       bound_nowhere_in_particular):
    """Consulting the published names first must not widen loopback generally.

    `localhost:3000` is another program on this machine and another origin; it
    is trusted only if the operator named it, which is what pins the published
    check to going *before* the bound-port rule rather than instead of it.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "https://localhost")
    server.trust_bind("127.0.0.1", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://localhost:3000"}).status_code == 403
        assert client.get("/api/state", headers={"Host": "localhost:3000"}).status_code == 403
    assert store.load_paper("doe2026study").get("notes") != "n"


def test_another_loopback_address_is_another_origin(queued_jobs,
                                                    bound_nowhere_in_particular):
    """`127.0.0.0/8` is all loopback, but it is not all one origin.

    A page served from `http://127.0.0.2:8765` is a separate browser origin from
    the app's own, so its simple multipart POST is a cross-site upload even
    though the port matches and the address is local. It presupposes something
    already listening on that address, which is why this is a cheap fence rather
    than a likely attack -- but keeping the origin rule to the names a browser
    really produces costs nothing.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://127.0.0.2:8765"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a page on another loopback address queued an upload"


def test_the_loopback_spellings_of_this_machine_stay_interchangeable(
        bound_nowhere_in_particular):
    """Narrowing the origin rule must not separate `::1` from `127.0.0.1`.

    The socket reports one of them and the user typed whichever they typed; both
    genuinely name this machine and the browser chooses between them unasked, so
    comparing the origin against the bound address would lock the app out of its
    own page. The long spelling of `::1` counts too.
    """
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    spellings = ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765",
                 "http://[0:0:0:0:0:0:0:1]:8765")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        for origin in spellings:
            assert client.patch("/api/papers/doe2026study", json={"notes": origin},
                                headers={"Origin": origin}).status_code == 200, origin


def test_serving_on_an_alternate_loopback_address_still_trusts_its_own_page(
        bound_nowhere_in_particular):
    """`serve --host 127.0.0.2` has to keep serving the page it just published.

    The narrow origin rule does not cover that address, so `trust_bind` records
    it explicitly -- from the host `serve()` was given, never from a request.
    """
    server.trust_bind("127.0.0.2", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://127.0.0.2:8765",
                                     "Host": "127.0.0.2:8765"}).status_code == 200
        # Its neighbour on the same interface is still somebody else.
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://127.0.0.3:8765"}).status_code == 403


def test_a_request_may_still_be_addressed_to_any_loopback_address(
        bound_nowhere_in_particular):
    """The `Host` check asks what reaches this server, not what page sent this.

    `curl http://127.0.0.5:8765` is an ordinary way to arrive, so narrowing the
    origin rule must not narrow the address rule along with it.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health",
                          headers={"Host": "127.0.0.5:8765"}).status_code == 200


def test_a_malformed_published_origin_is_skipped_rather_than_fatal(monkeypatch):
    """A typo in `PUBLISHED_ORIGINS_ENV` must not stop the server from starting.

    `urlsplit("http://[::1")` raises `ValueError: Invalid IPv6 URL` on its own,
    outside the parse's own `try`, and this list is read at import time -- so an
    unclosed bracket used to abort the import with a traceback naming neither
    the variable nor the offending entry. The good entry beside it still counts.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV,
                       "http://[::1, http://192.168.1.5:8765")
    assert server._authorities_from_environment() == {("192.168.1.5", 8765)}


def test_a_malformed_origin_header_is_refused_rather_than_a_500(
        queued_jobs, bound_nowhere_in_particular):
    """The same parse runs on every request, so a bad `Origin` is a refusal."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://[::1"},
        )
    assert response.status_code == 403
    assert queued_jobs == []


# --- a socket with no port, and a port nobody chose -----------------------

def _over_a_unix_socket(headers: dict[str, str]) -> int:
    """The status one POST gets when `scope["server"]` carries no port.

    Uvicorn's `--uds` mode reports `(socket_path, None)`: a path where the port
    would be. Nothing this project starts produces that -- `serve()` calls
    `uvicorn.run(host=..., port=...)` and there is no `--uds` option -- so the
    scope is built here rather than by standing a server up over a socket.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/upload",
        "raw_path": b"/api/upload",
        "query_string": b"",
        "root_path": "",
        "server": ("/run/doxograph.sock", None),
        "client": None,
        "headers": [(name.lower().encode(), value.encode())
                    for name, value in headers.items()],
    }
    statuses: list[int] = []

    async def reached(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    asyncio.run(server.RejectCrossSiteRequests(reached)(scope, receive, send))
    return statuses[0]


def test_a_socket_with_no_port_does_not_widen_the_origin_rule(
        bound_nowhere_in_particular):
    """Without a port, nothing tells the app's own page from its neighbours.

    The loopback rule leans on the bound port to say that `localhost:3000` is a
    different origin from `localhost:8765`. Take the port away and the rule
    would accept every canonical-loopback origin there is, so a page on any
    other local port could post a multipart form here. An origin arriving that
    way has to be one the operator published instead.
    """
    assert _over_a_unix_socket({"Host": "localhost",
                                "Origin": "http://localhost:3000"}) == 403
    # Not even the bare name the `Host` used: it is unverifiable for the same
    # reason, and a socket cannot say which local page it belongs to.
    assert _over_a_unix_socket({"Host": "localhost",
                                "Origin": "http://localhost"}) == 403


def test_a_socket_with_no_port_still_answers_for_its_own_name(
        bound_nowhere_in_particular):
    """The `Host` check asks which addresses reach here, which a socket can answer.

    So `curl --unix-socket` keeps working, and a request with no `Origin` -- no
    browser acting for somebody else's page -- is not the thing being refused.
    """
    assert _over_a_unix_socket({"Host": "localhost"}) == 200


def test_a_socket_with_no_port_trusts_the_name_the_operator_published(
        monkeypatch, bound_nowhere_in_particular):
    """The proxy in front of the socket knows the name; the socket does not.

    Which is exactly what `PUBLISHED_ORIGINS_ENV` is for, so the deployment is
    narrowed rather than shut off. Both spellings are published because the
    proxy terminates the TLS: the browser's `Origin` says `https`, while the
    `Host` it forwards over the socket is read against the scope's own `http`.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV,
                       "https://papers.example.com, http://papers.example.com")
    monkeypatch.setattr(server, "_published_authorities",
                        frozenset(server._authorities_from_environment()))
    assert _over_a_unix_socket({"Host": "papers.example.com",
                                "Origin": "https://papers.example.com"}) == 200
    assert _over_a_unix_socket({"Host": "papers.example.com",
                                "Origin": "http://localhost:3000"}) == 403


def test_an_ordinary_request_always_carries_the_port_the_rule_needs():
    """The narrowing above must not reach anything this project can produce.

    Uvicorn fills `scope["server"]` with `(host, port)` on a TCP bind and
    Starlette's `TestClient` reports a port too, so `bound_port` is `None` only
    on a socket opened by hand outside the CLI.
    """
    seen = []

    async def recording(scope, receive, send):
        if scope["type"] == "http":
            seen.append(scope.get("server"))
        await server.app(scope, receive, send)

    with TestClient(recording, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").status_code == 200
    assert seen and all(pair is not None and pair[1] is not None for pair in seen), seen


def test_serve_refuses_port_zero_at_the_command_line(capsys):
    """`--port 0` and a published `--host` cannot both be honoured.

    Uvicorn reads 0 as "any free port" and picks one after the trusted authority
    has been written down, so the server would answer on a port nobody named
    while trusting a port nobody is listening on -- and every request from off
    this machine would be refused with nothing to explain it. Reading the port
    back off the socket afterwards would not help: the operator has to know it
    in advance to reach the page at all. So it is a usage error, said at the
    point the operator can still fix it.
    """
    for bad in ("0", "-1", "65536"):
        with pytest.raises(SystemExit) as raised:
            __main__.build_parser().parse_args(["serve", "--host", "192.168.1.5",
                                                "--port", bad])
        assert raised.value.code == 2, bad
    assert "1-65535" in capsys.readouterr().err
    # An ordinary port is untouched.
    assert __main__.build_parser().parse_args(["serve", "--port", "8765"]).port == 8765


def test_serve_refuses_port_zero_before_recording_the_authority(
        bound_nowhere_in_particular):
    """The same guard for a caller that skips the parser: a script, or a test."""
    with pytest.raises(ValueError, match="cannot serve on port 0"):
        server.serve(host="192.168.1.5", port=0)
    assert server._published_authorities == frozenset(), \
        "a port that cannot be served was recorded as trusted anyway"
