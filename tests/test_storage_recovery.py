"""Damaged files stay visible, and interrupted topic changes resume safely."""

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from doxograph import __main__, config, server, store


def corpus():
    store.add_tag("old", "Original description")
    for key in ("first", "second"):
        store.save_paper(store.new_paper(key, title=key))
        store.add_claim(key, {"text": key, "tags": ["old"], "note": "My note"})
    rows = {row["id"]: row for row in store.claim_rows()}
    ids = list(rows)
    store.record_tensions("old", [{"claims": ids, "kind": "tension", "note": "Why"}], rows)
    store.record_agreements("old", [{"claims": ids, "note": "Agreement"}], rows)
    store.record_synthesis("old", "A synthesis", rows)
    return ids


def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


@pytest.mark.parametrize("contents", [b"{broken", b"\xff", b"[]", b'{"key":"damaged","claims":[null]}'])
def test_unreadable_papers_are_reported_without_changing_the_files(contents, caplog):
    store.save_paper(store.new_paper("healthy"))
    path = store.paper_path("damaged")
    path.write_bytes(contents)
    with client() as c:
        response = c.get("/api/state")
        assert response.status_code == 200
        assert [p["key"] for p in response.json()["papers"]] == ["healthy"]
        [issue] = response.json()["storage_issues"]
        assert issue["paper"] == "damaged"
        assert issue["path"] == str(path)
        assert "Cannot read" in issue["detail"]
        assert c.patch("/api/papers/damaged", json={"notes": "Overwrite?"}).status_code == 500
    assert path.read_bytes() == contents
    assert str(path) in caplog.text


def test_repairing_a_file_clears_the_warning_and_changes_the_etag():
    path = store.paper_path("repaired")
    path.write_text("{broken")
    with client() as c:
        first = c.get("/api/state")
        store.write_json(path, store.new_paper("repaired"))
        repaired = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []
    assert [p["key"] for p in repaired.json()["papers"]] == ["repaired"]


def test_permission_failure_is_visible(monkeypatch):
    store.save_paper(store.new_paper("unreadable"))
    original = Path.read_text

    def denied(path, *args, **kwargs):
        if path == store.paper_path("unreadable"):
            raise PermissionError("Access denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", denied)
    with client() as c:
        [issue] = c.get("/api/state").json()["storage_issues"]
    assert issue["paper"] == "unreadable"
    assert "Access denied" in issue["detail"]


@pytest.mark.parametrize("operation", ["rename", "delete"])
@pytest.mark.parametrize("failed_file", ["first.json", "second.json", "tensions.json", "agreements.json", "syntheses.json", "tags.yaml"])
def test_each_failed_write_can_resume_without_losing_edits(monkeypatch, operation, failed_file):
    ids = corpus()
    original = store.write_atomic

    def fail(path, text):
        if path.name == failed_file:
            raise OSError("Disk full")
        return original(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(store, "write_atomic", fail)
        with pytest.raises(store.StorageRecoveryError, match="Disk full"):
            if operation == "rename":
                store.rename_tag("old", "new")
            else:
                store.delete_tag("old")
        assert store.tag_change_path().exists()
        with pytest.raises(store.StorageRecoveryError):
            store.add_tag("unrelated")

    # Recovery must transform current data, not overwrite this newer edit with
    # a paper snapshot taken at the start of the failed operation.
    store.update_claim("first", ids[0], {"note": "Written after the failure", "reviewed": False})
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert response.json()["storage_issues"] == []
    expected = ["new"] if operation == "rename" else []
    assert store.tag_names() == expected
    for paper in store.all_papers():
        assert paper["claims"][0]["tags"] == expected
    first = store.load_paper("first")["claims"][0]
    assert first["note"] == "Written after the failure"
    assert first["reviewed"] is False
    assert store.load_tensions()[0]["topics"] == expected
    assert store.load_agreements()[0]["topics"] == expected
    assert set(store._read_syntheses()["syntheses"]) == set(expected)
    assert not store.tag_change_path().exists()
    before = store.paper_path("first").read_bytes()
    store.recover_storage()
    assert store.paper_path("first").read_bytes() == before


@pytest.mark.parametrize("exit_after", ["first.json", "tags.yaml"])
def test_a_fresh_process_recovers_an_abrupt_exit(exit_after):
    corpus()
    script = """
import os
from doxograph import store
original = store.write_atomic
def crash(path, text):
    original(path, text)
    if path.name == os.environ['EXIT_AFTER']:
        os._exit(17)
store.write_atomic = crash
store.rename_tag('old', 'new')
"""
    env = {**os.environ, "EXIT_AFTER": exit_after}
    child = subprocess.run([sys.executable, "-c", script], env=env, timeout=15)
    assert child.returncode == 17
    assert store.tag_change_path().exists()
    recovered = subprocess.run(
        [sys.executable, "-m", "doxograph", "list"], env=env,
        capture_output=True, text=True, timeout=15,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert store.tag_names() == ["new"]
    assert all(p["claims"][0]["tags"] == ["new"] for p in store.all_papers())
    assert not store.tag_change_path().exists()


def test_persistent_failure_remains_visible_and_blocks_competing_topic_changes(monkeypatch):
    corpus()
    original = store.write_atomic

    def fail(path, text):
        if path == store.paper_path("second"):
            raise OSError("Disk full")
        return original(path, text)

    monkeypatch.setattr(store, "write_atomic", fail)
    with client() as c:
        response = c.patch("/api/tags/old", json={"name": "new"})
        assert response.status_code == 503
        assert "retry automatically" in response.json()["detail"]
        state = c.get("/api/state")
        assert state.status_code == 200
        assert "Disk full" in state.json()["storage_issues"][0]["detail"]
        repeated = c.get("/api/state", headers={"If-None-Match": state.headers["etag"]})
        assert repeated.status_code == 304
        assert c.post("/api/tags", json={"name": "another"}).status_code == 503
    assert __main__.main(["list"]) == 1


def test_known_damage_prevents_a_topic_change_before_any_files_move():
    corpus()
    store.paper_path("damaged").write_text("{broken")
    with pytest.raises(store.PaperReadError):
        store.rename_tag("old", "new")
    assert store.tag_names() == ["old"]
    assert store.load_paper("first")["claims"][0]["tags"] == ["old"]
    assert not store.tag_change_path().exists()


def test_unreadable_recovery_intent_is_preserved():
    path = store.tag_change_path()
    path.write_text("{broken")
    with pytest.raises(store.StorageRecoveryError):
        store.add_tag("new")
    assert path.read_text() == "{broken"
    with client() as c:
        assert c.get("/api/state").json()["storage_issues"]
        assert c.get("/api/health").status_code == 200


def test_recovery_and_warnings_are_scoped_to_the_selected_workspace():
    other = config.create_workspace("Other research")
    store.tag_change_path().write_text("{broken")
    store.paper_path("damaged").write_text("{broken")
    with client() as c:
        response = c.get("/api/state", headers={"X-Doxograph-Workspace": other["id"]})
        assert response.json()["storage_issues"] == []
        assert len(c.get("/api/state").json()["storage_issues"]) == 2


def test_failure_to_record_intent_changes_nothing(monkeypatch):
    corpus()
    original = store.write_atomic

    def fail(path, text):
        if path == store.tag_change_path():
            raise OSError("Disk full")
        return original(path, text)

    monkeypatch.setattr(store, "write_atomic", fail)
    with pytest.raises(OSError):
        store.rename_tag("old", "new")
    assert store.tag_names() == ["old"]
    assert all(p["claims"][0]["tags"] == ["old"] for p in store.all_papers())
    assert not store.tag_change_path().exists()


def test_concurrent_vocabulary_edits_recover_once_and_both_survive(monkeypatch):
    corpus()
    original = store.write_atomic

    def fail(path, text):
        if path == store.paper_path("second"):
            raise OSError("Disk full")
        return original(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(store, "write_atomic", fail)
        with pytest.raises(store.StorageRecoveryError):
            store.rename_tag("old", "new")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(store.add_tag, ["extra-a", "extra-b"]))
    assert store.tag_names() == ["extra-a", "extra-b", "new"]
    assert not store.tag_change_path().exists()


def test_incomplete_intent_cannot_be_mistaken_for_a_deletion():
    corpus()
    change = {"version": 1, "old": "old", "tags": []}
    store.tag_change_path().write_text(json.dumps(change))
    with pytest.raises(store.StorageRecoveryError):
        store.recover_storage()
    assert store.load_paper("first")["claims"][0]["tags"] == ["old"]


def test_reading_a_healthy_library_does_not_require_write_access(monkeypatch):
    store.save_paper(store.new_paper("healthy"))

    @contextmanager
    def denied(path):
        raise PermissionError("Lock directory is read-only")
        yield

    monkeypatch.setattr(store, "_file_lock", denied)
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert response.json()["storage_issues"] == []


def test_recovery_lock_failure_is_reported_without_hiding_the_library(monkeypatch):
    corpus()
    store.write_json(store.tag_change_path(), {
        "version": 1, "old": "old", "new": "new", "tags": [{"name": "new"}],
    })

    @contextmanager
    def denied(path):
        raise PermissionError("Lock directory is read-only")
        yield

    monkeypatch.setattr(store, "_file_lock", denied)
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert len(response.json()["papers"]) == 2
    assert "Lock directory is read-only" in response.json()["storage_issues"][0]["detail"]
    assert store.tag_change_path().exists()


def test_transient_read_failure_is_retried_without_a_file_change(monkeypatch):
    store.save_paper(store.new_paper("temporary"))
    original = store.load_paper
    failed = False

    def fail_once(key):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("Temporary read failure")
        return original(key)

    monkeypatch.setattr(store, "load_paper", fail_once)
    with client() as c:
        first = c.get("/api/state")
        assert first.json()["storage_issues"]
        repaired = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []
    assert len(repaired.json()["papers"]) == 1


def test_repeated_state_polls_log_only_new_or_changed_read_errors(caplog):
    path = store.paper_path("damaged")
    path.write_text("{broken")

    def warnings():
        return [r for r in caplog.records if r.name == store.__name__]

    with client() as c:
        for _ in range(4):
            assert c.get("/api/state").json()["storage_issues"]
        assert len(warnings()) == 1
        path.write_text("[")
        assert c.get("/api/state").json()["storage_issues"]
        assert len(warnings()) == 2
        store.write_json(path, store.new_paper("damaged"))
        assert c.get("/api/state").json()["storage_issues"] == []
        path.write_text("[")
        assert c.get("/api/state").json()["storage_issues"]
        assert len(warnings()) == 3


def test_concurrent_scans_log_the_same_error_once(caplog):
    store.paper_path("damaged").write_text("{broken")
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: store.all_papers(), range(8))) == [[]] * 8
    assert len([r for r in caplog.records if r.name == store.__name__]) == 1


def test_log_deduplication_is_scoped_to_the_workspace(caplog):
    other = config.create_workspace("Other")
    store.paper_path("damaged").write_text("{broken")
    store.all_papers()
    with config.use_workspace(other["id"]):
        store.paper_path("damaged").write_text("{broken")
        store.all_papers()
        store.all_papers()
    store.all_papers()
    assert len([r for r in caplog.records if r.name == store.__name__]) == 2
