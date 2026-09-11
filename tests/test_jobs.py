"""Finished notifications can be dismissed within their own workspace."""

import pytest
from fastapi.testclient import TestClient

from doxograph import config, server, store


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
