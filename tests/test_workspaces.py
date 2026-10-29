"""Workspace isolation across storage, requests, and queued work."""

import contextlib
import multiprocessing
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor

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


def _interrupt_deletion(data_dir: str, workspace: str, committed: bool) -> None:
    os.environ["DOXOGRAPH_DATA"] = data_dir
    original = config._write_workspace_records

    def interrupted(records):
        if committed:
            original(records)
        os._exit(17)

    config._write_workspace_records = interrupted
    config.delete_workspace(workspace)


def _hold_workspace_in_another_server(data_dir, workspace, mode, ready, release):
    os.environ["DOXOGRAPH_DATA"] = data_dir
    with config.use_workspace(workspace):
        if mode == "request":
            with server._using_workspace(workspace):
                ready.set()
                assert release.wait(10)
        elif mode == "worker":
            @server._workspace_job
            def work(job):
                server._set(job, state="done")
                ready.set()
                assert release.wait(10)
                store.save_context("Worker finished")

            work(server._new_job("Finishing worker"))
        else:
            with ThreadPoolExecutor(1) as pool:
                server._pool = pool
                blocker = pool.submit(release.wait, 10)

                @server._workspace_job
                def work(job):
                    store.save_context("Queued worker finished")
                    server._set(job, state="done")

                future = server._submit_job(work, server._new_job("Queued worker"))
                ready.set()
                assert blocker.result(timeout=10)
                future.result(timeout=10)


def _pause_foreign_deletion(data_dir, workspace, ready, release):
    os.environ["DOXOGRAPH_DATA"] = data_dir
    original = config._write_workspace_records

    def paused(records):
        ready.set()
        assert release.wait(10)
        original(records)

    config._write_workspace_records = paused
    config.delete_workspace(workspace)


def _hold_foreign_job_owner(data_dir, workspace, ready, release):
    os.environ["DOXOGRAPH_DATA"] = data_dir
    with config.use_workspace(workspace):
        job = server._new_job("Finished import in another server")
        server._set(job, state="done")
        ready.set()
        assert release.wait(10)


def test_delete_refuses_live_foreign_journal_owner_then_removes_corpus():
    target = config.create_workspace("Research")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=_hold_foreign_job_owner,
                            args=(str(config.base_data_dir()), target["id"], ready, release))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    child.start()
    try:
        assert ready.wait(10)
        response = client.delete(f"/api/workspaces/{target['id']}")
        assert response.status_code == 409
        assert "Close that server" in response.json()["detail"]
        assert config.get_workspace(target["id"]) == target
        release.set()
        child.join(timeout=10)
        assert child.exitcode == 0
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200
        assert not (config.base_data_dir() / "workspaces" / target["id"]).exists()
    finally:
        release.set()
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


def test_deletion_releases_local_owner_handles_after_retirement_before_removal(monkeypatch):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        job = server._new_job("Finished import")
        server._set(job, state="done")
        folder = config.data_dir()
    assert any(path.parent == folder / '.jobs' for path in server.job_journal._leases)
    rmtree = config.shutil.rmtree

    def require_closed_handles(path):
        assert config.get_workspace(target["id"]) is None
        assert not any(lease.parent == folder / '.jobs' for lease in server.job_journal._leases)
        # Keep other requests excluded through the final filesystem cleanup.
        with pytest.raises(BlockingIOError):
            with config.workspace_activity(target["id"]):
                pytest.fail("Deletion released its activity lease before cleanup")
        rmtree(path)

    monkeypatch.setattr(config.shutil, "rmtree", require_closed_handles)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200
    assert not folder.exists()


@pytest.mark.parametrize("mode", ["request", "worker", "queued"])
def test_delete_waits_for_activity_in_another_server_process(mode):
    target = config.create_workspace("Research")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=_hold_workspace_in_another_server,
                            args=(str(config.base_data_dir()), target["id"], mode, ready, release))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    child.start()
    try:
        assert ready.wait(10)
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 409
        assert config.get_workspace(target["id"]) == target
        release.set()
        child.join(timeout=10)
        assert child.exitcode == 0
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200
        assert not (config.base_data_dir() / "workspaces" / target["id"]).exists()
    finally:
        release.set()
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


def test_foreign_deletion_refuses_new_requests_and_does_not_recreate_corpus():
    target = config.create_workspace("Research")
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=_pause_foreign_deletion,
                            args=(str(config.base_data_dir()), target["id"], ready, release))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = {"X-Doxograph-Workspace": target["id"]}
    child.start()
    try:
        assert ready.wait(10)
        assert client.get("/api/state", headers=headers).status_code == 409
        assert client.post("/api/ingest", headers=headers, json={"text": "anything"}).status_code == 409
        assert client.delete(f"/api/workspaces/{target['id']}", headers=headers).status_code == 409
        assert client.get("/api/state").status_code == 200
        release.set()
        child.join(timeout=10)
        assert child.exitcode == 0
        assert client.get("/api/state", headers=headers).status_code == 404
        assert not (config.base_data_dir() / "workspaces" / target["id"]).exists()
    finally:
        release.set()
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


@pytest.mark.parametrize("committed", [False, True])
def test_abrupt_deletion_keeps_uncommitted_corpus_at_original_path(committed):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        store.save_paper(store.new_paper("keep", title="Keep this paper"))
        path = store.paper_path("keep")
        before = path.read_bytes()
    child = multiprocessing.get_context("spawn").Process(
        target=_interrupt_deletion,
        args=(str(config.base_data_dir()), target["id"], committed),
    )
    child.start()
    child.join(timeout=10)
    try:
        assert not child.is_alive()
        assert child.exitcode == 17
        assert path.read_bytes() == before
        assert not list(path.parent.parent.parent.glob(".deleted-*"))
        assert (config.get_workspace(target["id"]) is None) == committed
        if committed:
            assert config.create_workspace("Research")["id"] == "research-2"
        else:
            with config.use_workspace(target["id"]):
                assert store.load_paper("keep")["title"] == "Keep this paper"
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=5)


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


@pytest.mark.skipif(config.fcntl is None, reason="POSIX read-only directory leases")
def test_named_workspace_browsing_does_not_require_writable_data_root(monkeypatch):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        store.save_paper(store.new_paper("paper", title="Read-only research"))
    root = config.base_data_dir().resolve()
    original_open, original_mkdir, descriptor_open = type(root).open, type(root).mkdir, os.open
    writes = []

    def readonly_open(path, mode="r", *args, **kwargs):
        if path.is_relative_to(root) and any(flag in mode for flag in "wax+"):
            writes.append(path)
            raise PermissionError("Synthetic read-only library")
        return original_open(path, mode, *args, **kwargs)

    def readonly_mkdir(path, *args, **kwargs):
        if path.is_relative_to(root):
            writes.append(path)
            raise PermissionError("Synthetic read-only library")
        return original_mkdir(path, *args, **kwargs)

    def readonly_descriptor(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            writes.append(path)
            raise PermissionError("Synthetic read-only library")
        return descriptor_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(type(root), "open", readonly_open)
    monkeypatch.setattr(type(root), "mkdir", readonly_mkdir)
    monkeypatch.setattr(os, "open", readonly_descriptor)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.get("/api/state", headers={"X-Doxograph-Workspace": target["id"]})
    assert response.status_code == 200
    assert response.json()["papers"][0]["title"] == "Read-only research"
    assert writes == []
    # A read-only handle still participates in deletion exclusion.
    with config.workspace_activity(target["id"]):
        with pytest.raises(BlockingIOError):
            with config.workspace_activity(target["id"], exclusive=True):
                pytest.fail("Deletion bypassed a read-only request lease")

def test_workspace_api_scopes_state_and_downloads():
    store.save_paper(store.new_paper("mind", title="Consciousness"))
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        store.save_paper(store.new_paper("gait", title="Gait"))
        store.pdf_path("gait").write_bytes(b"%PDF-1.4\n")

    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
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


def test_delete_removes_only_selected_corpus_and_retires_its_id():
    store.save_paper(store.new_paper("keep", title="Default paper"))
    config.set_ai_enabled(False)
    target = config.create_workspace("Research")
    other = config.create_workspace("Other research")
    with config.use_workspace(target["id"]):
        store.save_paper(store.new_paper("remove", title="Deleted paper"))
        store.pdf_path("remove").write_bytes(b"%PDF-1.4\n")
        store.save_context("Private research notes")
        folder = config.data_dir()
        finished = server._new_job("Finished import")
        server._set(finished, state="done")
    with config.use_workspace(other["id"]):
        store.save_paper(store.new_paper("other", title="Other paper"))
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = {"X-Doxograph-Workspace": target["id"]}
    assert client.get("/api/state", headers=headers).status_code == 200

    response = client.delete(f"/api/workspaces/{target['id']}", headers=headers)

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["workspaces"]] == ["default", other["id"]]
    assert not folder.exists()
    assert not list(folder.parent.glob(".deleted-*"))
    assert config.get_workspace(target["id"]) is None
    assert finished["id"] not in server._jobs
    assert target["id"] not in server._state_cache
    assert client.get("/api/state", headers=headers).status_code == 404
    assert client.post("/api/ingest", headers=headers, json={"text": "anything"}).status_code == 404
    assert [p["key"] for p in store.all_papers()] == ["keep"]
    assert not config.ai_enabled()
    with config.use_workspace(other["id"]):
        assert [p["key"] for p in store.all_papers()] == ["other"]
    assert config.create_workspace("Research")["id"] == "research-2"
    assert client.get("/api/state", headers=headers).status_code == 404


def test_delete_rejects_default_and_missing_workspace():
    store.save_paper(store.new_paper("keep", title="Default paper"))
    config.rename_workspace("default", "My library")
    before = config.workspaces_path().read_bytes()
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.delete("/api/workspaces/default").status_code == 422
    assert client.delete("/api/workspaces/missing").status_code == 404
    assert config.workspaces_path().read_bytes() == before
    assert store.load_paper("keep")["title"] == "Default paper"


def test_committed_deletion_succeeds_when_corpus_and_journal_cleanup_fail(monkeypatch, caplog):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        first = server._new_job("First finished import")
        second = server._new_job("Second finished import")
        server._set(first, state="done")
        server._set(second, state="done")
        first_path = server.job_journal._paths[first["id"]]
        second_path = server.job_journal._paths[second["id"]]
    original_unlink = type(first_path).unlink

    def fail_removal(folder):
        raise PermissionError("Corpus cleanup blocked")

    def fail_first_journal(path, *args, **kwargs):
        if path == first_path:
            raise PermissionError("Journal cleanup blocked")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(config.shutil, "rmtree", fail_removal)
    monkeypatch.setattr(type(first_path), "unlink", fail_first_journal)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.delete(f"/api/workspaces/{target['id']}")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["workspaces"]] == ["default"]
    assert config.get_workspace(target["id"]) is None
    assert first["id"] not in server._jobs
    assert second["id"] not in server._jobs
    assert first["id"] not in server.job_journal._paths
    assert second["id"] not in server.job_journal._paths
    assert first_path.exists()
    assert not second_path.exists()
    assert client.get("/api/state", headers={"X-Doxograph-Workspace": target["id"]}).status_code == 404
    assert "Corpus cleanup blocked" in caplog.text
    assert "Journal cleanup blocked" in caplog.text


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory durability barrier")
def test_retirement_syncs_registry_directory_before_removing_corpus(monkeypatch):
    target = config.create_workspace("Research")
    events = []
    fsync, replace, rmtree = os.fsync, os.replace, config.shutil.rmtree

    def synced(descriptor):
        events.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")
        fsync(descriptor)

    def replaced(source, destination):
        replace(source, destination)
        events.append("replace")

    def removed(folder):
        assert config.get_workspace(target["id"]) is None
        events.append("remove")
        rmtree(folder)

    monkeypatch.setattr(os, "fsync", synced)
    monkeypatch.setattr(os, "replace", replaced)
    monkeypatch.setattr(config.shutil, "rmtree", removed)
    config.delete_workspace(target["id"])
    assert events == ["file", "replace", "directory", "remove"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX directory durability barrier")
def test_failed_retirement_directory_sync_keeps_original_corpus(monkeypatch, caplog):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        store.save_paper(store.new_paper("keep", title="Original paper"))
        path = store.paper_path("keep")
        before = path.read_bytes()
        retained = config.pdfs_dir() / ".incoming-retry.pdf"
        retained.write_bytes(b"%PDF-1.4\nSaved retry input")
        job = server._new_import_job("Retry upload", False, staged=retained, filename="input.pdf")
        server._set(job, state="error")
        folder = config.data_dir()
        files_before = {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    fsync = os.fsync

    def refuse_directory(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("Directory sync unavailable")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refuse_directory)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.delete(f"/api/workspaces/{target['id']}")
    assert response.status_code == 200
    assert config.get_workspace(target["id"]) is None
    assert path.read_bytes() == before
    assert {p.relative_to(folder): p.read_bytes() for p in folder.rglob("*") if p.is_file()} == files_before
    assert job["id"] not in server._jobs
    assert job["id"] not in server.job_journal._paths
    assert "corpus kept" in caplog.text
    assert "Directory sync unavailable" in caplog.text


def test_delete_rolls_back_corpus_if_registry_write_fails(monkeypatch):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        store.save_paper(store.new_paper("keep", title="Keep this paper"))
        path = store.paper_path("keep")
        before = path.read_bytes()
    registry = config.workspaces_path().read_bytes()

    def fail(records):
        raise OSError("Read-only registry")

    monkeypatch.setattr(config, "_write_workspace_records", fail)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.delete(f"/api/workspaces/{target['id']}").status_code == 503
    assert path.read_bytes() == before
    assert config.workspaces_path().read_bytes() == registry
    assert config.get_workspace(target["id"]) == target


def test_delete_rejects_broken_registry_without_touching_data():
    target = config.create_workspace("Research")
    folder = config.base_data_dir() / "workspaces" / target["id"]
    config.workspaces_path().write_text("{invalid json")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.delete(f"/api/workspaces/{target['id']}").status_code == 422
    assert folder.is_dir()
    assert config.workspaces_path().read_text() == "{invalid json"


@pytest.mark.parametrize("state", ["queued", "fetching", "reading"])
def test_delete_waits_for_queued_and_running_jobs(state):
    target = config.create_workspace("Research")
    with config.use_workspace(target["id"]):
        job = server._new_job("Active import", state=state)
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 409
        assert config.get_workspace(target["id"]) == target
        server._set(job, state="done")
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200
    finally:
        server._jobs.pop(job["id"], None)


def test_delete_waits_for_inflight_requests(monkeypatch):
    target = config.create_workspace("Research")
    held, release = threading.Event(), threading.Event()
    original = server._build_state

    def paused():
        held.set()
        assert release.wait(5)
        return original()

    monkeypatch.setattr(server, "_build_state", paused)
    server._state_cache.pop(target["id"], None)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.get, "/api/state", headers={"X-Doxograph-Workspace": target["id"]})
        try:
            assert held.wait(5)
            assert client.delete(f"/api/workspaces/{target['id']}").status_code == 409
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200


def test_new_requests_are_refused_while_deletion_is_in_progress(monkeypatch):
    target = config.create_workspace("Research")
    held, release = threading.Event(), threading.Event()
    original = config.delete_workspace

    def paused(value):
        held.set()
        assert release.wait(5)
        return original(value)

    monkeypatch.setattr(config, "delete_workspace", paused)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = {"X-Doxograph-Workspace": target["id"]}
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(client.delete, f"/api/workspaces/{target['id']}")
        try:
            assert held.wait(5)
            assert client.post("/api/ingest", headers=headers, json={"text": "anything"}).status_code == 409
            assert client.delete(f"/api/workspaces/{target['id']}").status_code == 409
            assert client.get("/api/state").status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200
    assert client.get("/api/state", headers=headers).status_code == 404


def test_delete_refuses_symbolic_link_to_another_corpus(tmp_path):
    target = config.create_workspace("Research")
    folder = config.base_data_dir() / "workspaces" / target["id"]
    outside = tmp_path / "outside"
    folder.rename(outside)
    marker = outside / "keep.txt"
    marker.write_text("Keep this data")
    folder.symlink_to(outside, target_is_directory=True)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.delete(f"/api/workspaces/{target['id']}").status_code == 422
    assert marker.read_text() == "Keep this data"
    assert config.get_workspace(target["id"]) == target


@pytest.mark.parametrize("value", ["", ".", "..", "../outside", "Research"])
def test_delete_rejects_invalid_ids_without_removing_workspaces(value):
    target = config.create_workspace("Research")
    before = config.workspaces_path().read_bytes()
    with pytest.raises(KeyError):
        config.delete_workspace(value)
    assert config.workspaces_path().read_bytes() == before
    assert config.get_workspace(target["id"]) == target
    assert (config.base_data_dir() / "workspaces" / target["id"]).is_dir()


def test_delete_waits_until_worker_returns_even_after_terminal_state():
    target = config.create_workspace("Research")
    held, release = threading.Event(), threading.Event()

    @server._workspace_job
    def worker(job):
        server._set(job, state="done")
        held.set()
        assert release.wait(5)
        store.save_paper(store.new_paper("last-write", title="Final cleanup"))

    with config.use_workspace(target["id"]):
        job = server._new_job("Finishing import")
    try:
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(worker, job)
            try:
                assert held.wait(5)
                assert client.delete(f"/api/workspaces/{target['id']}").status_code == 409
            finally:
                release.set()
            pending.result(timeout=5)
        assert client.delete(f"/api/workspaces/{target['id']}").status_code == 200
    finally:
        server._jobs.pop(job["id"], None)


def test_refused_pruning_lease_preserves_notifications_journals_and_retry_inputs(monkeypatch):
    monkeypatch.setattr(server, "_jobs", {})
    target = config.create_workspace("Research")
    jobs, inputs, journals = [], [], []
    with config.use_workspace(target["id"]):
        for index in range(42):
            staged = config.pdfs_dir() / f".incoming-retry-{index}.pdf"
            staged.write_bytes(b"%PDF-1.4\nRetry input")
            job = server._new_import_job(f"Import {index}", False, staged=staged, filename="input.pdf")
            server._set(job, state="error")
            jobs.append(job)
            inputs.append(staged)
            journals.append(server.job_journal._paths[job["id"]])
    activity = config.workspace_activity
    busy = True

    @contextlib.contextmanager
    def lease(value, *, exclusive=False):
        if busy and value == target["id"]:
            raise BlockingIOError("Another server is active")
        with activity(value, exclusive=exclusive):
            yield

    monkeypatch.setattr(config, "workspace_activity", lease)
    server._prune_jobs()
    assert set(server._jobs) == {job["id"] for job in jobs}
    assert all(path.exists() for path in inputs + journals)
    assert all(job["_import"]["staged"] == path for job, path in zip(jobs, inputs))

    busy = False
    server._prune_jobs()
    assert set(server._jobs) == {job["id"] for job in jobs[2:]}
    assert all(not path.exists() for path in inputs[:2] + journals[:2])
    assert all(path.exists() for path in inputs[2:] + journals[2:])


@pytest.mark.parametrize("named", [False, True], ids=["default", "named"])
def test_rename_preserves_corpus_and_updates_cached_state(named):
    other = config.create_workspace("Other")
    workspace = config.create_workspace("Research") if named else config.get_workspace("default")
    key = workspace["id"]
    with config.use_workspace(key):
        paper = store.new_paper("mind", title="Consciousness")
        paper["notes"] = "Keep my reading notes."
        store.save_paper(paper)
        store.add_claim("mind", {"text": "A finding.", "note": "My claim note."})
        store.pdf_path("mind").write_bytes(b"%PDF-1.4\n")
        store.save_context("My research question.")
        original_dir = config.data_dir()
        paths = [store.paper_path("mind"), store.pdf_path("mind"), store.context_path()]
        contents = {path: path.read_bytes() for path in paths}

    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    headers = {"X-Doxograph-Workspace": key}
    before = client.get("/api/state", headers=headers)
    assert client.get("/api/state", headers={**headers, "If-None-Match": before.headers["etag"]}).status_code == 304

    response = client.patch(f"/api/workspaces/{key}", json={"name": "  Consciousness   research  "})
    assert response.status_code == 200
    renamed = response.json()["workspace"]
    assert renamed == {**workspace, "name": "Consciousness research"}
    assert config.get_workspace(key) == renamed
    assert config.get_workspace(other["id"]) == other
    assert next(item for item in config.list_workspaces() if item["id"] == key) == renamed
    assert {path: path.read_bytes() for path in paths} == contents
    with config.use_workspace(key):
        assert config.data_dir() == original_dir

    after = client.get("/api/state", headers={**headers, "If-None-Match": before.headers["etag"]})
    assert after.status_code == 200
    assert after.json() == {**before.json(), "workspace": renamed}
    assert after.headers["etag"] != before.headers["etag"]
    assert client.get(f"/pdf/mind?workspace={key}").status_code == 200

    # A second rename updates the saved entry, and later creation preserves it.
    assert client.patch(f"/api/workspaces/{key}", json={"name": "My library"}).status_code == 200
    config.create_workspace("New research")
    assert config.get_workspace(key)["name"] == "My library"
    assert sum(item["id"] == key for item in config.list_workspaces()) == 1


@pytest.mark.parametrize("name", ["", " \n\t ", "x" * 81])
def test_rename_rejects_invalid_names_without_changing_registry(name):
    config.create_workspace("Other")
    before = config.workspaces_path().read_bytes()
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.patch("/api/workspaces/default", json={"name": name})
    assert response.status_code == 422
    assert config.workspaces_path().read_bytes() == before


def test_rename_missing_workspace_does_not_create_it():
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    assert client.patch("/api/workspaces/missing", json={"name": "Research"}).status_code == 404
    assert [item["id"] for item in config.list_workspaces()] == ["default"]


def test_rename_does_not_overwrite_a_broken_registry():
    config.rename_workspace("default", "My library")
    config.workspaces_path().write_text("{not valid json", encoding="utf-8")
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    response = client.patch("/api/workspaces/default", json={"name": "Research"})
    assert response.status_code == 422
    assert config.workspaces_path().read_text(encoding="utf-8") == "{not valid json"
    assert client.get("/api/state").json()["workspace"]["name"] == "Default workspace"


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
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    assert client.get("/api/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/app.js").status_code == 200
    response = client.post("/api/workspaces", json={"name": "Consciousness"})
    assert response.status_code == 422
    assert "workspace registry is not valid JSON" in response.json()["detail"]
    # The default corpus is built in, not registered, so it is still readable.
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/state", headers={"X-Doxograph-Workspace": "default"}).status_code == 200
    assert client.get("/research-tools.js").status_code == 200
    assert client.get("/research-tools.css").status_code == 200
    assert client.get("/vendor/pdf.min.mjs").status_code == 200
    # Anything that has to read the registry says what is wrong with it.
    for response in (client.get("/api/workspaces"),
                     client.get("/api/state", headers={"X-Doxograph-Workspace": "animal"})):
        assert response.status_code == 500
        detail = response.json()["detail"]
        assert "workspace registry is not valid JSON" in detail
        assert str(config.workspaces_path()) in detail
