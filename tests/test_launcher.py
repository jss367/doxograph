"""The parts of the server the macOS app leans on."""

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
    with TestClient(server.app) as client:
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
        with TestClient(server.app) as client:
            assert client.get("/api/health").json()["busy"] == 1
    finally:
        for job in (running, done, failed):
            server._jobs.pop(job["id"], None)


def test_health_does_not_read_the_corpus(monkeypatch):
    """Polled every quarter second during startup: it must stay cheap."""
    def fail(*args, **kwargs):
        raise AssertionError("health loaded papers")

    monkeypatch.setattr(server.store, "all_papers", fail)
    with TestClient(server.app) as client:
        assert client.get("/api/health").status_code == 200


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

    with TestClient(server.app) as client:
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
        with TestClient(server.app) as client:
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


def test_health_counts_an_upload_that_is_staged_but_not_yet_a_job(monkeypatch):
    """Staging a large PDF takes a while, and `_new_job` only runs after it."""
    seen = []
    real_stage = server.ingest.stage_upload

    def watched(stream, name):
        seen.append(server.health()["busy"])
        return real_stage(stream, name)

    monkeypatch.setattr(server.ingest, "stage_upload", watched)
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)

    with TestClient(server.app) as client:
        client.post("/api/upload?extract_now=false",
                    files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")})

    assert seen == [1]
    for staged in config.pdfs_dir().glob(".incoming-*"):
        staged.unlink()


def test_a_finished_upload_stops_being_counted(monkeypatch):
    """Otherwise the launcher would warn about a paper forever after."""
    monkeypatch.setattr(server._pool, "submit", lambda *a, **k: None)
    with TestClient(server.app) as client:
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
    with TestClient(server.app) as client:
        with pytest.raises(RuntimeError):
            client.post("/api/upload?extract_now=false",
                        files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")})
        assert client.get("/api/health").json()["busy"] == 0


def test_health_is_not_charged_for_other_requests():
    """Only `/api/upload` is counted: every other route would inflate `busy`
    and make the app ask about work that does not exist."""
    with TestClient(server.app) as client:
        assert client.get("/api/health").json()["busy"] == 0
        assert client.post("/api/ingest", json={"text": "", "extract": False}).status_code == 200
        assert client.get("/api/health").json()["busy"] == 0


def test_pdf_is_an_attachment_by_default():
    """What a browser has always been handed, unchanged."""
    store.pdf_path("smith2024recovery").write_bytes(b"%PDF-1.4\n")
    with TestClient(server.app) as client:
        response = client.get("/pdf/smith2024recovery")
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith("attachment")


def test_pdf_can_be_asked_for_inline():
    """The Mac app opens the paper in a web view, which downloads an attachment
    instead of rendering it."""
    store.pdf_path("smith2024recovery").write_bytes(b"%PDF-1.4\n")
    with TestClient(server.app) as client:
        response = client.get("/pdf/smith2024recovery?inline=1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("inline")
    assert "smith2024recovery.pdf" in response.headers["content-disposition"]


def test_missing_pdf_is_still_a_404_when_asked_for_inline():
    with TestClient(server.app) as client:
        assert client.get("/pdf/nobody?inline=1").status_code == 404
