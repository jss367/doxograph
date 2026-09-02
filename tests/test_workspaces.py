"""Workspace isolation across storage, requests, and queued work."""

from fastapi.testclient import TestClient

from doxograph import config, server, store


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
