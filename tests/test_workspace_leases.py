"""Exercise Windows byte-range ownership without requiring a Windows host."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from doxograph import config, server


class ByteRangeLocks:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self):
        self.owners = {}
        self.lock_calls = 0

    def locking(self, descriptor, mode, length):
        stat = os.fstat(descriptor)
        key = (stat.st_dev, stat.st_ino)
        assert length == 1
        if mode == self.LK_NBLCK:
            if key in self.owners:
                raise OSError("Byte range already locked, including through another local handle")
            self.owners[key] = descriptor
            self.lock_calls += 1
        else:
            assert self.owners.pop(key) == descriptor


@pytest.fixture
def windows_locks(monkeypatch):
    workspace = config.create_workspace("Research")
    locks = ByteRangeLocks()
    monkeypatch.setattr(config, "fcntl", None)
    monkeypatch.setattr(config, "msvcrt", locks)
    yield workspace["id"], locks
    assert not config._windows_activity_leases
    assert not locks.owners


def test_windows_nested_activity_uses_one_handle_and_excludes_deletion(windows_locks):
    workspace, locks = windows_locks
    with config.workspace_activity(workspace):
        with config.workspace_activity(workspace):
            assert locks.lock_calls == 1
            with pytest.raises(BlockingIOError):
                with config.workspace_activity(workspace, exclusive=True):
                    pytest.fail("Deletion entered an active workspace")
        assert locks.owners
    assert not locks.owners
    with config.workspace_activity(workspace, exclusive=True):
        with pytest.raises(BlockingIOError):
            with config.workspace_activity(workspace):
                pytest.fail("Request entered a workspace during deletion")


def test_windows_lease_survives_request_until_worker_finishes(windows_locks):
    workspace, locks = windows_locks
    ready, release = threading.Event(), threading.Event()

    def worker():
        with config.workspace_activity(workspace):
            ready.set()
            assert release.wait(5)

    with ThreadPoolExecutor(1) as pool:
        try:
            with config.workspace_activity(workspace):
                future = pool.submit(worker)
                assert ready.wait(5)
            assert locks.lock_calls == 1
            with pytest.raises(BlockingIOError):
                with config.workspace_activity(workspace, exclusive=True):
                    pytest.fail("Deletion entered a worker's workspace")
        finally:
            release.set()
        future.result(timeout=5)
    assert not locks.owners


def test_windows_foreign_lock_failure_does_not_cache_a_lease(windows_locks):
    workspace, locks = windows_locks
    path = config.base_data_dir() / "locks" / f"workspace-{workspace}.activity.lock"
    with path.open("a+") as foreign:
        locks.locking(foreign.fileno(), locks.LK_NBLCK, 1)
        try:
            with pytest.raises(BlockingIOError):
                with config.workspace_activity(workspace):
                    pytest.fail("Request entered another server's locked workspace")
            assert not config._windows_activity_leases
        finally:
            locks.locking(foreign.fileno(), locks.LK_UNLCK, 1)
    with config.workspace_activity(workspace):
        assert locks.lock_calls == 2


def test_windows_named_import_can_submit_and_run_while_request_holds_lease(windows_locks, monkeypatch):
    workspace, locks = windows_locks
    jobs = []

    @server._workspace_job
    def work(job, ref, analyze):
        jobs.append(job)
        server._set(job, state="done")

    monkeypatch.setattr(server, "_run_ingest", work)
    with ThreadPoolExecutor(1) as pool:
        monkeypatch.setattr(server, "_pool", pool)
        client = TestClient(server.app, base_url="http://127.0.0.1:8765")
        response = client.post("/api/ingest", headers={"X-Doxograph-Workspace": workspace},
                               json={"text": "2602.06941", "extract": False})
        assert response.status_code == 200
        assert response.json()["queued"] == 1
    assert len(jobs) == 1
    assert jobs[0]["state"] == "done"
    assert not locks.owners
