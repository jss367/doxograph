"""Workspace isolation across storage, requests, and queued work."""

import multiprocessing
import os

import pytest
from fastapi.testclient import TestClient

from doxograph import config, server, store


def _create_workspace_in_process(data_dir: str, start, results) -> None:
    os.environ["DOXOGRAPH_DATA"] = data_dir
    start.wait()
    try:
        results.put(("ok", config.create_workspace("Consciousness")["id"]))
    except BaseException as exc:
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def test_existing_corpus_is_the_default_and_new_workspaces_are_isolated():
    store.save_paper(store.new_paper("shared-key", title="A consciousness paper"))

    animal = config.create_workspace("Animal locomotion")

    assert animal["id"] == "animal-locomotion"
    assert [paper["title"] for paper in store.all_papers()] == ["A consciousness paper"]

    with config.use_workspace(animal["id"]):
        assert store.all_papers() == []
        store.save_paper(store.new_paper("shared-key", title="An animal locomotion paper"))
        assert [paper["title"] for paper in store.all_papers()] == ["An animal locomotion paper"]
        assert config.data_dir() == config.base_data_dir() / "workspaces" / animal["id"]

    assert [paper["title"] for paper in store.all_papers()] == ["A consciousness paper"]


def test_workspace_api_scopes_state_and_downloads():
    store.save_paper(store.new_paper("mind", title="Consciousness"))
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        store.save_paper(store.new_paper("gait", title="Gait"))
        store.pdf_path("gait").write_bytes(b"%PDF-1.4\n")

    client = TestClient(server.app)
    listed = client.get("/api/workspaces").json()["workspaces"]
    assert [(item["id"], item["name"]) for item in listed] == [
        ("default", "Default workspace"),
        ("animal-locomotion", "Animal locomotion"),
    ]
    assert [paper["key"] for paper in client.get("/api/state").json()["papers"]] == ["mind"]

    headers = {"X-Doxograph-Workspace": animal["id"]}
    state = client.get("/api/state", headers=headers).json()
    assert state["workspace"]["name"] == "Animal locomotion"
    assert [paper["key"] for paper in state["papers"]] == ["gait"]
    assert client.get(f"/pdf/gait?workspace={animal['id']}").status_code == 200
    assert client.get("/pdf/gait").status_code == 404
    assert client.get("/api/state", headers={"X-Doxograph-Workspace": "../outside"}).status_code == 404


def test_background_job_keeps_the_workspace_where_it_was_queued():
    animal = config.create_workspace("Animal locomotion")

    @server._workspace_job
    def write_paper(job):
        store.save_paper(store.new_paper("queued", title="Queued in animal locomotion"))

    with config.use_workspace(animal["id"]):
        job = server._new_job("queued paper")

    # Run after the submitting request's context has gone back to default.
    write_paper(job)

    assert store.all_papers() == []
    with config.use_workspace(animal["id"]):
        assert [paper["key"] for paper in store.all_papers()] == ["queued"]
    server._jobs.pop(job["id"], None)


def test_completed_job_retention_is_applied_per_workspace():
    animal = config.create_workspace("Animal locomotion")
    server._jobs.clear()
    try:
        default_job = server._new_job("default result")
        server._set(default_job, state="done")

        animal_jobs = []
        with config.use_workspace(animal["id"]):
            for index in range(41):
                job = server._new_job(f"animal result {index}")
                server._set(job, state="done")
                animal_jobs.append(job)

        server._prune_jobs()

        assert default_job["id"] in server._jobs
        assert animal_jobs[0]["id"] not in server._jobs
        assert all(job["id"] in server._jobs for job in animal_jobs[1:])
    finally:
        server._jobs.clear()


def test_duplicate_workspace_names_receive_distinct_stable_ids():
    first = config.create_workspace("Consciousness")
    second = config.create_workspace("Consciousness")

    assert first["id"] == "consciousness"
    assert second["id"] == "consciousness-2"


def test_concurrent_processes_cannot_overwrite_workspace_registry():
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_create_workspace_in_process,
            args=(str(config.base_data_dir()), start, results),
        )
        for _ in range(2)
    ]
    for process in processes:
        process.start()
    start.set()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert sorted(outcomes) == [("ok", "consciousness"), ("ok", "consciousness-2")]
    assert [workspace["id"] for workspace in config.list_workspaces()] == [
        "default", "consciousness", "consciousness-2",
    ]


def test_named_workspace_ignores_default_export_override(monkeypatch, tmp_path):
    override = tmp_path / "configured.html"
    monkeypatch.setenv("DOXOGRAPH_EXPORT", str(override))
    assert config.export_path() == override

    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        assert config.export_path() == config.data_dir() / "export" / "doxograph.html"


def test_malformed_registry_is_not_overwritten_by_workspace_creation():
    animal = config.create_workspace("Animal locomotion")
    registry = config.workspaces_path()
    registry.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(ValueError, match="workspace registry is not valid JSON"):
        config.create_workspace("Consciousness")

    assert registry.read_text(encoding="utf-8") == "{not valid json"
    assert (config.base_data_dir() / "workspaces" / animal["id"]).is_dir()
    assert not (config.base_data_dir() / "workspaces" / "consciousness").exists()


def test_malformed_registry_does_not_block_health_or_app_shell():
    config.workspaces_path().write_text("{not valid json", encoding="utf-8")
    client = TestClient(server.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    response = client.post("/api/workspaces", json={"name": "Consciousness"})
    assert response.status_code == 422
    assert "workspace registry is not valid JSON" in response.json()["detail"]
