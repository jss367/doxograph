"""Import retries and finished notifications stay within their workspace."""

import httpx
import pytest
from fastapi.testclient import TestClient
from pdfs import minimal_pdf

from doxograph import config, extract, ingest, server, store


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "_jobs", {})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        yield client


@pytest.mark.parametrize("state", ["error", "done"])
def test_dismiss_finished_job_preserves_paper_and_other_jobs(client, state):
    paper = store.new_paper("study", title="A study")
    store.save_paper(paper)
    job = server._new_job("study")
    server._set(job, state=state, key="study", detail="Result to acknowledge")
    other = server._new_job("Another paper")

    response = client.delete(f"/api/jobs/{job['id']}")

    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/jobs").json()["jobs"] == [other]
    assert store.load_paper("study") == paper
    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404


@pytest.mark.parametrize("state", ["queued", "fetching", "reading"])
def test_running_job_cannot_be_dismissed(client, state):
    job = server._new_job("A paper")
    server._set(job, state=state)

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 409
    assert client.get("/api/jobs").json()["jobs"] == [job]


def test_job_cannot_be_dismissed_from_another_workspace(client):
    other = config.create_workspace("Other research")
    with config.use_workspace(other["id"]):
        job = server._new_job("A paper")
        server._set(job, state="error")

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 404
    headers = {"X-Doxograph-Workspace": other["id"]}
    assert client.get("/api/jobs", headers=headers).json()["jobs"] == [job]
    assert client.delete(f"/api/jobs/{job['id']}", headers=headers).status_code == 204
    assert client.get("/api/jobs", headers=headers).json()["jobs"] == []


@pytest.fixture
def immediate_worker(monkeypatch):
    monkeypatch.setattr(server._pool, "submit", lambda fn, *args: fn(*args))


@pytest.fixture
def metadata_timeout(monkeypatch):
    def timeout(*args, **kwargs):
        raise httpx.ReadTimeout("metadata timed out")

    monkeypatch.setattr(ingest, "guess_from_pdf", timeout)


@pytest.mark.parametrize("analyze", [False, True])
def test_retry_recovers_missing_pdf_without_duplicate_paper(client, monkeypatch, immediate_worker, analyze):
    downloads, analyses = [], []

    def download(url, key, api):
        downloads.append(key)
        if len(downloads) == 1:
            raise httpx.ConnectError("offline")
        store.pdf_path(key).write_bytes(minimal_pdf("A recovered paper"))
        return True

    monkeypatch.setattr(ingest, "download_pdf", download)
    monkeypatch.setattr(extract, "extract_paper", lambda key: analyses.append(key))
    assert client.post("/api/ingest", json={
        "text": "https://example.org/study.pdf", "extract": analyze,
    }).json()["queued"] == 1
    job = client.get("/api/jobs").json()["jobs"][0]
    assert job["state"] == "error"
    assert job["retryable"] is True
    assert "PDF is unavailable" in job["explanation"]
    assert "_import" not in job

    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    [finished] = client.get("/api/jobs").json()["jobs"]
    assert finished["id"] == job["id"]
    assert finished["state"] == "done"
    assert finished["key"] == job["key"]
    assert finished["retryable"] is False
    assert store.paper_keys() == [job["key"]]
    assert store.pdf_path(job["key"]).exists()
    assert analyses == ([job["key"]] if analyze else [])


@pytest.mark.parametrize("failure_stage", ["metadata", "analysis"])
def test_uploaded_bytes_survive_failure_and_are_released_after_retry(
        client, monkeypatch, immediate_worker, failure_stage):
    body = minimal_pdf("A study of retrying imports")
    metadata_calls, analysis_calls = [], []

    def guess(path, api, display_name=None):
        assert path.read_bytes() == body
        metadata_calls.append(display_name)
        if failure_stage == "metadata" and len(metadata_calls) == 1:
            raise httpx.ReadTimeout("metadata timed out")
        return {"title": "A study", "authors": [], "year": None,
                "source": {"kind": "file", "id": display_name}}

    def analyze(key):
        analysis_calls.append(key)
        if failure_stage == "analysis" and len(analysis_calls) == 1:
            raise RuntimeError("analysis failed")

    monkeypatch.setattr(ingest, "guess_from_pdf", guess)
    monkeypatch.setattr(extract, "extract_paper", analyze)
    client.post("/api/upload", files={"files": ("study.pdf", body, "application/pdf")})
    [job] = client.get("/api/jobs").json()["jobs"]
    assert job["state"] == "error"
    assert job["retryable"] is True
    assert len(list(config.pdfs_dir().glob(".incoming-*"))) == 1
    expected = "took too long" if failure_stage == "metadata" else "AI analysis failed"
    assert expected in job["explanation"]

    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    [finished] = client.get("/api/jobs").json()["jobs"]
    assert finished["state"] == "done"
    assert store.pdf_path(finished["key"]).read_bytes() == body
    assert len(store.paper_keys()) == 1
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_retry_is_atomic_and_workspace_scoped(client, monkeypatch):
    other = config.create_workspace("Other research")
    with config.use_workspace(other["id"]):
        job = server._new_import_job("study", False,
                                     ref=ingest.Ref("arxiv", "2602.06941", "2602.06941"))
        server._set(job, state="error", detail="offline", explanation="Check your connection.")
    submitted = []
    monkeypatch.setattr(server._pool, "submit", lambda *args: submitted.append(args))
    path = f"/api/jobs/{job['id']}/retry"
    assert client.post(path).status_code == 404
    headers = {"X-Doxograph-Workspace": other["id"]}
    assert client.post(path, headers=headers).status_code == 200
    assert client.post(path, headers=headers).status_code == 409
    assert client.delete(f"/api/jobs/{job['id']}", headers=headers).status_code == 409
    assert len(submitted) == 1
    assert job["state"] == "queued"
    assert job["explanation"] == job["detail"] == ""
    assert submitted[0][1] is job
    assert submitted[0][-1] is False


def test_retry_submission_failure_keeps_failure_and_source(client, monkeypatch):
    job = server._new_import_job("study", False, ref=ingest.Ref("arxiv", "2602.06941", ""))
    server._set(job, state="error", detail="offline", explanation="Check your connection.")

    def refuse(*args):
        raise RuntimeError("pool unavailable")

    monkeypatch.setattr(server._pool, "submit", refuse)
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 503
    [failed] = client.get("/api/jobs").json()["jobs"]
    assert failed["state"] == "error"
    assert failed["detail"] == "offline"
    assert failed["explanation"] == "Check your connection."
    assert failed["retryable"] is True


def test_retry_respects_analysis_disabled_since_failure(client, monkeypatch, immediate_worker):
    key = "study"
    store.save_paper(store.new_paper(key))
    store.pdf_path(key).write_bytes(minimal_pdf("A study"))
    job = server._new_import_job("study", True, ref=ingest.Ref("arxiv", "2602.06941", ""))
    server._set(job, state="error", key=key)
    monkeypatch.setattr(ingest, "ingest_ref", lambda ref: (key, False))
    monkeypatch.setattr(extract, "extract_paper", lambda key: pytest.fail("analysis is disabled"))
    config.set_ai_enabled(False)

    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 200
    assert job["state"] == "done"


@pytest.mark.parametrize("state", ["queued", "fetching", "reading", "done", "error"])
def test_nonretryable_job_is_rejected(client, state):
    job = server._new_job("Analysis job")
    server._set(job, state=state)
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409
    assert client.post("/api/jobs/999999/retry").status_code == 404


@pytest.mark.parametrize("cleanup", ["dismiss", "prune"])
def test_failed_upload_cleanup_releases_retained_bytes(client, immediate_worker, metadata_timeout, cleanup):
    client.post("/api/upload?extract_now=false",
                files={"files": ("study.pdf", minimal_pdf("A study"), "application/pdf")})
    [job] = client.get("/api/jobs").json()["jobs"]
    assert list(config.pdfs_dir().glob(".incoming-*"))
    if cleanup == "dismiss":
        assert client.delete(f"/api/jobs/{job['id']}").status_code == 204
    else:
        for _ in range(40):
            server._set(server._new_job("Finished"), state="done")
        server._prune_jobs()
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_shutdown_releases_failed_uploads_in_every_workspace(monkeypatch, immediate_worker, metadata_timeout):
    monkeypatch.setattr(server, "_jobs", {})
    other = config.create_workspace("Other research")
    retained = []
    store.save_paper(store.new_paper("existing"))
    body = minimal_pdf("An existing paper")
    store.pdf_path("existing").write_bytes(body)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        for workspace in (config.DEFAULT_WORKSPACE_ID, other["id"]):
            response = client.post("/api/upload?extract_now=false",
                headers={"X-Doxograph-Workspace": workspace},
                files={"files": ("study.pdf", minimal_pdf("A new study"), "application/pdf")})
            assert response.status_code == 200
            with config.use_workspace(workspace):
                [staged] = config.pdfs_dir().glob(".incoming-*")
                retained.append(staged)
        assert all(path.exists() for path in retained)

    assert all(not path.exists() for path in retained)
    assert store.pdf_path("existing").read_bytes() == body
    assert store.load_paper("existing")["key"] == "existing"


def test_worker_finishing_after_shutdown_releases_its_retry_input(monkeypatch, metadata_timeout):
    monkeypatch.setattr(server, "_jobs", {})
    submitted = []
    monkeypatch.setattr(server._pool, "submit", lambda *args: submitted.append(args))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.post("/api/upload?extract_now=false",
                    files={"files": ("study.pdf", minimal_pdf("A study"), "application/pdf")})
        [staged] = config.pdfs_dir().glob(".incoming-*")

    # Shutdown must not delete the input out from under a queued worker.
    assert staged.exists()
    function, *args = submitted[0]
    function(*args)
    assert args[0]["state"] == "error"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_non_pdf_upload_cannot_retry_the_same_invalid_bytes(client, immediate_worker):
    client.post("/api/upload?extract_now=false",
                files={"files": ("broken.pdf", b"not a PDF", "application/pdf")})
    [job] = client.get("/api/jobs").json()["jobs"]
    assert job["state"] == "error"
    assert job["retryable"] is False
    assert "Upload a valid PDF" in job["explanation"]
    assert client.post(f"/api/jobs/{job['id']}/retry").status_code == 409
    assert list(config.pdfs_dir().glob(".incoming-*")) == []
