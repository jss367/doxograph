"""The readiness probe the macOS launcher polls."""

from fastapi.testclient import TestClient

from doxograph import __version__, server


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
