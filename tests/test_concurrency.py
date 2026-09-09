"""Locks, atomic writes, and concurrent writers of one corpus."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from doxograph import config, server, store


def test_overlapping_claim_updates_do_not_revert_each_other():
    """Two claims on one paper, reviewed at once: neither flag may be lost."""
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    first = store.add_claim("doe2026study", {"text": "One.", "reviewed": False})
    second = store.add_claim("doe2026study", {"text": "Two.", "reviewed": False})

    start = threading.Barrier(2)
    errors = []

    def mark(claim_id):
        try:
            start.wait(timeout=5)
            store.update_claim("doe2026study", claim_id, {"reviewed": True})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=mark, args=(c,)) for c in (first["id"], second["id"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[first["id"]]["reviewed"] is True
    assert claims[second["id"]]["reviewed"] is True


def test_many_overlapping_writes_all_survive():
    """Every claim added concurrently must be present, and the file stay valid."""
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    start = threading.Barrier(8)
    errors = []

    def add(n):
        try:
            start.wait(timeout=5)
            store.add_claim("doe2026study", {"text": f"Claim {n}."})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    claims = store.load_paper("doe2026study")["claims"]
    assert len(claims) == 8
    assert {c["text"] for c in claims} == {f"Claim {n}." for n in range(8)}
    assert len({c["id"] for c in claims}) == 8   # ids stayed unique under contention


def test_concurrent_writes_leave_no_temporary_files():
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    threads = [threading.Thread(target=store.add_claim,
                                args=("doe2026study", {"text": f"C{n}."})) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert list(config.papers_dir().glob("*.tmp")) == []
    assert list(config.papers_dir().glob(".*")) == []


def test_concurrent_tag_accepts_both_land():
    """Two papers' proposals accepted at once must both reach the vocabulary."""
    import threading

    for key, tag in (("doe2026a", "alpha"), ("doe2026b", "beta")):
        paper = store.new_paper(key)
        paper["proposed_tags"] = [{"name": tag, "description": f"{tag} desc"}]
        store.save_paper(paper)

    start = threading.Barrier(2)
    errors = []

    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    def accept(key, tag):
        try:
            start.wait(timeout=5)   # build the client first, so the barrier is the only gate
            client.post(f"/api/papers/{key}/proposed-tags", json={"accept": [tag]})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=accept, args=a) for a in (("doe2026a", "alpha"), ("doe2026b", "beta"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert set(store.tag_names()) == {"alpha", "beta"}, "one accepted tag was dropped"


def test_many_concurrent_tag_adds_all_land():
    import threading

    start = threading.Barrier(6)

    def add(n):
        start.wait(timeout=5)
        store.add_tag(f"topic-{n}", f"number {n}")

    threads = [threading.Thread(target=add, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert set(store.tag_names()) == {f"topic-{n}" for n in range(6)}


def test_deleting_a_paper_while_a_merge_is_saving_leaves_no_ghost():
    """Remove must not race a completing re-read into a paper with no PDF.

    The merge parks between its load and its save. The delete is attempted at
    exactly that moment: without the lock it unlinks both files and the merge's
    save then recreates the JSON, leaving a paper whose PDF is gone.
    """
    import threading

    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")

    parked = threading.Event()
    delete_attempted = threading.Event()
    errors = []

    def merge():
        try:
            with store.paper_lock("doe2026study"):
                paper = store.load_paper("doe2026study")
                parked.set()
                delete_attempted.wait(timeout=2)   # give the delete its chance
                paper["summary"] = "merged"
                store.save_paper(paper)
        except Exception as exc:
            errors.append(exc)

    def remove():
        try:
            parked.wait(timeout=5)
            threading.Timer(0.2, delete_attempted.set).start()
            store.delete_paper("doe2026study")     # blocks on the lock when locked
            delete_attempted.set()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=merge), threading.Thread(target=remove)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert not store.paper_path("doe2026study").exists(), "the merge recreated a deleted paper"
    assert not store.pdf_path("doe2026study").exists(), "a ghost paper was left with no PDF"


def test_rename_and_accept_do_not_deadlock():
    """Lock order is vocabulary then paper on both paths."""
    import threading

    store.add_tag("old", "to be renamed")
    paper = store.new_paper("doe2026study")
    paper["proposed_tags"] = [{"name": "fresh", "description": "new one"}]
    store.save_paper(paper)
    store.add_claim("doe2026study", {"text": "X.", "tags": ["old"]})

    errors = []

    def rename():
        try:
            store.rename_tag("old", "renamed")
        except Exception as exc:
            errors.append(exc)

    def accept():
        try:
            with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
                client.post("/api/papers/doe2026study/proposed-tags", json={"accept": ["fresh"]})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=rename), threading.Thread(target=accept)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "deadlocked"
    assert not errors, errors
    assert "renamed" in store.tag_names() and "fresh" in store.tag_names()


def test_paper_lock_is_held_across_processes(tmp_path):
    """A second OS process must block on the same paper's lock."""
    import subprocess
    import sys
    import textwrap
    import time

    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    # The child appends to a log under the lock; the parent holds it first.
    log = tmp_path / "order.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.paper_lock("doe2026study"):
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.paper_lock("doe2026study"):
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)                      # the child is blocked, not finished
        blocked_while_held = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked_while_held, "the child did not wait for the lock"
    assert child.returncode == 0
    assert log.read_text().split() == ["parent", "child"]


def test_paper_lock_is_reentrant_within_a_thread():
    """Nesting must not deadlock on the file lock's own descriptor."""
    store.save_paper(store.new_paper("doe2026study"))
    with store.paper_lock("doe2026study"):
        with store.paper_lock("doe2026study"):
            store.add_claim("doe2026study", {"text": "X."})   # takes it a third time
    assert len(store.load_paper("doe2026study")["claims"]) == 1


def test_vocabulary_lock_is_held_across_processes(tmp_path):
    import subprocess
    import sys
    import textwrap
    import time

    log = tmp_path / "vocab.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.vocab_lock():
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.vocab_lock():
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the vocabulary lock"
    assert log.read_text().split() == ["parent", "child"]


def test_claim_lock_is_held_across_processes(tmp_path):
    """Two processes ingesting the same paper must not both reserve a key."""
    import subprocess
    import sys
    import textwrap
    import time

    log = tmp_path / "claim.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.claim_lock():
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.claim_lock():
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the claim lock"
    assert log.read_text().split() == ["parent", "child"]


def test_the_vocabulary_is_never_observed_half_written():
    """Readers do not take the lock, so the write has to be atomic."""
    import threading

    for n in range(12):
        store.add_tag(f"topic-{n}", f"description number {n}")

    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            names = store.tag_names()
            if names and len(names) < 12:
                bad.append(sorted(names))

    def writer():
        for n in range(40):
            store.add_tag(f"extra-{n}", "x")

    watcher = threading.Thread(target=reader)
    watcher.start()
    writer()
    stop.set()
    watcher.join(timeout=5)

    assert bad == [], f"a partial vocabulary was visible: {bad[:3]}"
    assert len(store.tag_names()) == 52


def test_the_vocabulary_is_replaced_rather_than_truncated(monkeypatch):
    """The atomicity mechanism itself.

    The concurrent-reader test above asserts the invariant but cannot reliably
    hit the truncation window. This one is deterministic: a write that goes
    through `os.replace` cannot be observed half-done, and an in-place
    `write_text` never calls it.
    """
    import os as os_module

    store.add_tag("alpha", "first")
    replaced = []
    real_replace = os_module.replace

    def watched_replace(src_path, dest_path, *args, **kwargs):
        replaced.append(str(dest_path))
        return real_replace(src_path, dest_path, *args, **kwargs)

    monkeypatch.setattr(store.os, "replace", watched_replace)
    store.add_tag("beta", "second")

    assert any(name.endswith("tags.yaml") for name in replaced), (
        f"tags.yaml was not published through os.replace: {replaced}")
    assert sorted(store.tag_names()) == ["alpha", "beta"]


def test_ledger_writes_are_atomic_too():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    assert store.load_ledger() == [{"id": "L1", "text": "A claim."}]
    store.save_ledger([{"id": "L1", "text": "A claim."}, {"id": "L2", "text": "Another."}])
    assert [c["id"] for c in store.load_ledger()] == ["L1", "L2"]


def test_a_paper_deleted_mid_listing_does_not_fail_the_listing(monkeypatch):
    """`paper_keys` is a snapshot; a key can be gone by the time it is read."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    real_keys = store.paper_keys

    monkeypatch.setattr(store, "paper_keys", lambda: real_keys() + ["gone2026missing"])
    assert [p["key"] for p in store.all_papers()] == ["doe2026study"]

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/state").status_code == 200
