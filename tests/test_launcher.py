"""The parts of the server the macOS app leans on."""

from fastapi.testclient import TestClient

from doxograph import __version__, server, store


def test_health_identifies_the_app_and_is_idle_by_default():
    with TestClient(server.app) as client:
        body = client.get("/api/health").json()
    assert body["app"] == "doxograph"
    assert body["version"] == __version__
    assert body["busy"] == 0


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
