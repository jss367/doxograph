"""The parts of the server the macOS app leans on."""

import asyncio
import os
import types

import pytest
from fastapi.testclient import TestClient

from doxograph import __version__, config, server, store


@pytest.fixture(autouse=True)
def no_stray_jobs():
    """`server._jobs` outlives any one test, and `busy` counts every entry."""
    server._jobs.clear()
    yield
    server._jobs.clear()


def test_health_identifies_the_app_and_is_idle_by_default():
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/api/health").json()
    assert body["app"] == "doxograph"
    assert body["version"] == __version__
    assert body["busy"] == 0
    assert body["jobs"] == 0
    assert body["arriving"] == 0


def test_health_counts_only_unfinished_jobs():
    """The launcher warns before quitting on a nonzero count, so a finished job
    must not keep it warning forever."""
    running = server._new_job("reading.pdf")
    server._set(running, state="reading")
    done = server._new_job("done.pdf")
    server._set(done, state="done")
    failed = server._new_job("failed.pdf")
    server._set(failed, state="error")
    try:
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
            assert client.get("/api/health").json()["busy"] == 1
    finally:
        for job in (running, done, failed):
            server._jobs.pop(job["id"], None)


def test_health_does_not_read_the_corpus(monkeypatch):
    """Polled every quarter second during startup: it must stay cheap."""
    def fail(*args, **kwargs):
        raise AssertionError("health loaded papers")

    monkeypatch.setattr(server.store, "all_papers", fail)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").status_code == 200


def test_code_reports_a_matching_pair_for_an_untouched_checkout():
    """Nothing has been edited under this process, so what it loaded and what is
    on disk are the same, and the launcher adopts it."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/api/code").json()
    assert body["app"] == "doxograph"
    assert body["version"] == __version__
    assert body["running"]
    assert body["onDisk"] == body["running"]


def test_code_reports_what_was_loaded_not_what_is_there_now(monkeypatch):
    """The point of the endpoint: `running` is fixed at import and `onDisk` is
    read fresh, so a checkout that moves under a live server shows as a pair
    that no longer agrees."""
    monkeypatch.setattr(server, "SOURCE_AT_START", "a" * 64)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = client.get("/api/code").json()
    assert body["running"] != body["onDisk"]


def test_code_and_health_name_the_same_release():
    """The launcher reads the two in separate requests, and a port can change
    hands between them. Matching releases are how it notices that the digests it
    just read belong to a different server than the counts, so both endpoints
    have to report the release for that check to mean anything."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/code").json()["version"] == \
            client.get("/api/health").json()["version"]


def test_code_answers_without_a_workspace():
    """The launcher asks this during the port walk, before any corpus has been
    chosen, and has to get an answer even from a registry that is broken."""
    config.workspaces_path().write_text("{not valid json", encoding="utf-8")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.get("/api/code").status_code == 200


def test_fingerprint_follows_the_python_and_ignores_the_rest(tmp_path):
    """Only `.py` files are loaded once and kept. `static/` is read per request,
    so editing `app.js` under a running server changes nothing about it, and
    `__pycache__` is a byproduct of the sources already counted."""
    (tmp_path / "static").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "server.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "static" / "app.js").write_text("one", encoding="utf-8")
    (tmp_path / "__pycache__" / "server.py").write_text("compiled", encoding="utf-8")
    before = server.source_fingerprint(tmp_path)

    (tmp_path / "static" / "app.js").write_text("two", encoding="utf-8")
    (tmp_path / "__pycache__" / "server.py").write_text("recompiled", encoding="utf-8")
    assert server.source_fingerprint(tmp_path) == before

    (tmp_path / "server.py").write_text("x = 2", encoding="utf-8")
    assert server.source_fingerprint(tmp_path) != before


def test_fingerprint_follows_names_as_well_as_contents(tmp_path):
    """A file moved or deleted changes what the process would load as surely as
    an edited one, and its contents alone would not say so."""
    (tmp_path / "ingest.py").write_text("x = 1", encoding="utf-8")
    before = server.source_fingerprint(tmp_path)

    (tmp_path / "ingest.py").rename(tmp_path / "extract.py")
    renamed = server.source_fingerprint(tmp_path)
    assert renamed != before

    (tmp_path / "extract.py").unlink()
    assert server.source_fingerprint(tmp_path) not in (before, renamed)


def test_fingerprint_is_empty_when_the_package_cannot_be_read(tmp_path):
    """An empty digest is the launcher's signal that the question went
    unanswered, which it treats as no reason to refuse rather than as a match."""
    assert server.source_fingerprint(tmp_path / "gone") == ""


def _fake_module(path):
    """A `sys.modules` entry, which is all `imported_files` reads."""
    return types.SimpleNamespace(__file__=str(path))


def test_dependencies_are_counted_too(tmp_path):
    """`pip install -e .` on a changed pyproject.toml upgrades FastAPI or
    Pydantic without touching a doxograph source file, and the server goes on
    running the version it imported. A digest over the package alone would call
    that current."""
    dependency = tmp_path / "starlette.py"
    dependency.write_text("__version__ = '0.1'", encoding="utf-8")
    os.utime(dependency, (server.STARTED_AT - 3600, server.STARTED_AT - 3600))
    modules = {"starlette": _fake_module(dependency)}

    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded == on_disk

    dependency.write_text("__version__ = '0.2'  # reinstalled", encoding="utf-8")
    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded != on_disk


def test_a_dependency_imported_later_is_counted_from_then(tmp_path):
    """A process goes on importing. Uvicorn arrives after `server.py` is done
    and pypdf only when a PDF does, so a list fixed at import would leave out
    the HTTP server itself. A file joining the walk is remembered as it is then,
    which is a match, and watched from there."""
    early = tmp_path / "fastapi.py"
    early.write_text("x = 1", encoding="utf-8")
    late = tmp_path / "uvicorn.py"
    late.write_text("y = 1", encoding="utf-8")
    # Written before this process, as every file a real server imports is.
    for path in (early, late):
        os.utime(path, (server.STARTED_AT - 3600, server.STARTED_AT - 3600))

    modules = {"fastapi": _fake_module(early)}
    server.dependency_state(modules)

    modules["uvicorn"] = _fake_module(late)
    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded == on_disk

    late.write_text("y = 2  # reinstalled after the server loaded it", encoding="utf-8")
    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded != on_disk


def test_a_dependency_replaced_before_it_was_ever_asked_about(tmp_path):
    """The gap the first-seen map leaves on its own. A `doxograph serve` in a
    terminal imports Uvicorn seconds after startup and may never be asked about
    its code until an app launches days later — by which time `pip install -e .`
    has upgraded Uvicorn, and the map would remember the replacement as the
    original. A file written since the process started counts as replaced
    whatever the map says."""
    late = tmp_path / "uvicorn.py"
    late.write_text("y = 2  # the upgrade, installed after the server started",
                    encoding="utf-8")
    modules = {"uvicorn": _fake_module(late)}

    # First sight of this path is also the first request, long after the module
    # behind it was imported and after the file under it was replaced.
    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded != on_disk


def test_a_dependency_older_than_the_process_is_left_alone(tmp_path):
    """The other side of it: everything a server runs was written before it
    started, so the check has to be quiet about the ordinary case."""
    settled = tmp_path / "fastapi.py"
    settled.write_text("x = 1", encoding="utf-8")
    os.utime(settled, (server.STARTED_AT - 3600, server.STARTED_AT - 3600))
    modules = {"fastapi": _fake_module(settled)}

    as_loaded, on_disk = server.dependency_state(modules)
    assert as_loaded == on_disk


def test_a_dependency_that_has_gone_is_a_change_not_a_silence(tmp_path):
    """Its module is still loaded here; it is the disk that no longer has it.
    An unreadable *package* is a question nobody answered, but this is an
    answer."""
    dependency = tmp_path / "httpx.py"
    dependency.write_text("x = 1", encoding="utf-8")
    os.utime(dependency, (server.STARTED_AT - 3600, server.STARTED_AT - 3600))
    modules = {"httpx": _fake_module(dependency)}
    server.dependency_state(modules)

    dependency.unlink()
    as_loaded, on_disk = server.dependency_state(modules)
    assert on_disk and as_loaded != on_disk


def test_the_package_is_left_out_of_the_dependency_half():
    """Its own files are digested by contents instead. A checkout is rewritten
    by every branch switch and rebase, mostly back to bytes it already held, and
    size-and-time would report each of those as a server gone stale."""
    counted = server.imported_files()
    assert counted, "a live server has imported something"
    inside = str(server.PACKAGE) + os.sep
    assert not [path for path in counted if path.startswith(inside)]


def test_an_unreadable_package_answers_nothing_at_all():
    """The launcher refuses to adopt on a mismatch, so a digest it cannot trace
    back to real files has to be unusable rather than merely different —
    otherwise the half that did answer would decide on its own."""
    assert server.code_fingerprint("", "dependencies-answered-fine") == ""
    assert server.code_fingerprint("sources", "") != ""


def _multipart(body: bytes, boundary: str = "b0undary") -> bytes:
    """One PDF, as a browser's `FormData` would post it."""
    return (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files"; filename="paper.pdf"\r\n'
        "Content-Type: application/pdf\r\n\r\n"
    ).encode() + body + f"\r\n--{boundary}--\r\n".encode()


def test_health_counts_an_upload_whose_body_is_still_arriving(monkeypatch):
    """The window a quit used to fall into.

    A PDF dropped on the page posts straight to this route without going near
    the Mac app's own upload counter, so nothing else can see it. Until the
    request arrives in full there is no job either, and the launcher reading
    `busy: 0` stops the server it started on top of a paper mid-flight.
    """
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)
    seen = []

    def body():
        # Read on the way in, before the server has been handed a single byte.
        seen.append(server.health()["busy"])
        yield _multipart(b"%PDF-1.4\n" + b"x" * 50_000)

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload?extract_now=false", content=body(),
            headers={"content-type": "multipart/form-data; boundary=b0undary"})

    assert response.json() == {"queued": 1}
    assert seen == [1], "the arriving upload was invisible to health"
    for staged in config.pdfs_dir().glob(".incoming-*"):
        staged.unlink()


def test_health_reports_reading_and_arriving_apart(monkeypatch):
    """What the two extra fields mean, and why they cannot be one number.

    `jobs` is work that dies with the *server*: a reading job survives the Mac
    app quitting on top of an adopted server. `arriving` is work that dies with
    the *client*: the upload is being sent by the app's own web view, so it is
    lost whether the server was adopted or not. The app asks about `arriving`
    always and about `jobs` only when it owns the server, so folding them
    together would make it either quit on top of an upload or refuse to quit
    over a reading job that was never at risk.

    `busy` stays the sum of the two, unchanged, for anything reading only that.
    """
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)
    reading = server._new_job("reading.pdf")
    server._set(reading, state="reading")
    seen = []

    def body():
        seen.append(server.health())
        yield _multipart(b"%PDF-1.4\n" + b"x" * 50_000)

    try:
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
            client.post(
                "/api/upload?extract_now=false", content=body(),
                headers={"content-type": "multipart/form-data; boundary=b0undary"})
            idle = client.get("/api/health").json()
    finally:
        server._jobs.pop(reading["id"], None)

    assert seen[0]["jobs"] == 1, "the reading job was not reported on its own"
    assert seen[0]["arriving"] == 1, "the upload on the wire was not reported on its own"
    assert seen[0]["busy"] == 2, "busy is still the sum of the two"
    # The request is over, so nothing is arriving; the reading job and the
    # queued one the upload made are both still work the server would lose.
    assert (idle["jobs"], idle["arriving"], idle["busy"]) == (2, 0, 2)
    for staged in config.pdfs_dir().glob(".incoming-*"):
        staged.unlink()


def test_health_reads_the_arrival_count_before_the_job_count(monkeypatch):
    """The order of the two samples is the whole of what makes them safe.

    They are taken under different locks, so an upload can finish in the gap
    between them. An upload on its way out creates its job first and only stops
    being counted once its response has gone, so reading `arriving` first is
    enough: a zero there means anything in flight has already left a job for the
    second read to find. Read `jobs` first and the same paper falls through both
    numbers — no job yet when `jobs` was read, no longer arriving when
    `arriving` was — and an owned launcher quits on top of a queued job.
    """
    fired = []

    def finish_the_upload():
        server._new_job("paper.pdf")
        with server._arrivals_lock:
            server._requests_arriving -= 1

    class SlippingLock:
        """Lets that upload land in the gap between health's two samples."""

        def __init__(self, inner):
            self._inner = inner

        def __enter__(self):
            return self._inner.__enter__()

        def __exit__(self, *exception):
            released = self._inner.__exit__(*exception)
            # After the release, so the upload can take either lock itself.
            if not fired:
                fired.append(True)
                finish_the_upload()
            return released

    monkeypatch.setattr(server, "_requests_arriving", 1)
    monkeypatch.setattr(server, "_jobs_lock", SlippingLock(server._jobs_lock))
    monkeypatch.setattr(server, "_arrivals_lock", SlippingLock(server._arrivals_lock))

    body = server.health()

    assert fired, "health took no sample at all"
    assert server._requests_arriving == 0, "the upload never finished"
    assert len(server._jobs) == 1, "the upload left no job behind"
    assert (body["jobs"], body["arriving"], body["busy"]) == (1, 1, 2), (
        "health sampled the job count before the arrival count, so the paper "
        "that landed between the two reads was invisible to both"
    )


def test_health_counts_an_upload_that_is_staged_but_not_yet_a_job(monkeypatch):
    """Staging a large PDF takes a while, and `_new_job` only runs after it."""
    seen = []
    real_stage = server.ingest.stage_upload

    def watched(stream, name):
        seen.append(server.health()["busy"])
        return real_stage(stream, name)

    monkeypatch.setattr(server.ingest, "stage_upload", watched)
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.post("/api/upload?extract_now=false",
                    files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")})

    assert seen == [1]
    for staged in config.pdfs_dir().glob(".incoming-*"):
        staged.unlink()


def test_a_finished_upload_stops_being_counted(monkeypatch):
    """Otherwise the launcher would warn about a paper forever after."""
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.post("/api/upload?extract_now=false",
                    files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")})
        # The job it made outlives the request, so settle that separately: what
        # is being checked here is that the request stopped being counted.
        for job in list(server._jobs.values()):
            server._set(job, state="done")
        assert client.get("/api/health").json()["busy"] == 0
    for staged in config.pdfs_dir().glob(".incoming-*"):
        staged.unlink()


def test_a_failed_upload_stops_being_counted(monkeypatch):
    """A count that only falls on success would wedge quitting after one error."""
    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(server.ingest, "stage_upload", explode)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        with pytest.raises(RuntimeError):
            client.post("/api/upload?extract_now=false",
                        files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")})
        assert client.get("/api/health").json()["busy"] == 0


def test_health_counts_an_ingest_that_is_still_being_handled(monkeypatch):
    """The same window as the upload one, on the route the page posts links to.

    `/api/ingest` makes its jobs at the end of the handler, so from the moment
    the request lands until then there is nothing for `busy` to see. The body is
    small, so the window is short, but a quit that falls in it stops an owned
    server and the papers are never fetched.
    """
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)
    seen = []
    real_parse = server.ingest.parse_refs

    def watched(text):
        # Inside the handler, before a single job exists.
        seen.append(server.health())
        return real_parse(text)

    monkeypatch.setattr(server.ingest, "parse_refs", watched)

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/ingest", json={"text": "arxiv.org/abs/2401.00001", "extract": False})

    assert response.json()["queued"] == 1
    assert seen[0]["jobs"] == 0, "the job does not exist yet — that is the point"
    assert seen[0]["arriving"] == 1, "the ingest request was invisible to health"
    assert seen[0]["busy"] == 1


def test_only_reads_escape_the_arrival_count():
    """What is counted is decided by the method, not by a list of routes.

    A list of the routes that can create work goes stale the first time someone
    adds one, and silently: the new route is simply not counted and a quit lands
    on top of it. So the rule is the request's own method — the `/api/whatever`
    below does not exist, and is still counted, which is exactly the property
    that keeps this true as the route table grows.

    Reads have to be left out: `/api/health` is a GET and would otherwise report
    itself as busy forever.
    """
    seen = {}

    async def app(scope, receive, send):
        seen[scope["method"]] = server._requests_arriving

    counter = server.CountArrivingRequests(app)
    for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"):
        asyncio.run(counter({"type": "http", "method": method, "path": "/api/whatever"},
                            None, None))

    assert sorted(m for m, count in seen.items() if count == 1) == [
        "DELETE", "PATCH", "POST", "PUT"]
    assert sorted(m for m, count in seen.items() if count == 0) == ["GET", "HEAD", "OPTIONS"]
    assert server._requests_arriving == 0, "the count did not come back down"


def test_health_does_not_count_itself():
    """It is a GET, and it reports the count from inside its own request."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health").json()["busy"] == 0


def test_a_finished_mutating_request_stops_being_counted():
    """A count that did not fall would have the app warning about work forever."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/ingest", json={"text": "", "extract": False}).status_code == 200
        assert client.get("/api/health").json()["busy"] == 0
        assert server._requests_arriving == 0


def test_pdf_is_an_attachment_by_default():
    """What a browser has always been handed, unchanged."""
    store.pdf_path("smith2024recovery").write_bytes(b"%PDF-1.4\n")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/pdf/smith2024recovery")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


def test_pdf_can_be_asked_for_inline():
    """The Mac app opens the paper in a web view, which downloads an attachment
    instead of rendering it."""
    store.pdf_path("smith2024recovery").write_bytes(b"%PDF-1.4\n")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/pdf/smith2024recovery?inline=1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert "smith2024recovery.pdf" in response.headers["content-disposition"]


def test_missing_pdf_is_still_a_404_when_asked_for_inline():
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/pdf/nobody?inline=1").status_code == 404
