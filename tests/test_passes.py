"""Per-topic and per-paper passes run a few items at a time."""

import threading
import time

from doxograph import config, extract, server, store


def test_items_run_concurrently_and_every_result_is_reported():
    started = threading.Barrier(3, timeout=5)

    def work(item):
        started.wait()          # all three have to be running at once to get past this
        if item == "b":
            raise ValueError("no")
        return item.upper()

    results = {item: (result, exc) for item, result, exc in
               extract.run_concurrently(["a", "b", "c"], work, workers=3)}
    assert results["a"] == ("A", None) and results["c"] == ("C", None)
    assert results["b"][0] is None and isinstance(results["b"][1], ValueError)


def test_workers_are_capped_by_the_setting(monkeypatch):
    monkeypatch.setattr(config, "PASS_WORKERS", 1)
    running = []
    peak = []

    def work(item):
        running.append(item)
        peak.append(len(running))
        time.sleep(0.02)
        running.remove(item)
        return item

    list(extract.run_concurrently([1, 2, 3, 4], work))
    assert max(peak) == 1


def test_the_cap_holds_across_passes_running_at_once(monkeypatch):
    monkeypatch.setattr(extract, "_pass_slots", threading.BoundedSemaphore(2))
    running = []
    peak = []
    guard = threading.Lock()

    def work(item):
        with guard:
            running.append(item)
            peak.append(len(running))
        time.sleep(0.02)
        with guard:
            running.remove(item)
        return item

    passes = [threading.Thread(target=lambda: list(extract.run_concurrently([1, 2, 3, 4], work, workers=4)))
              for _ in range(3)]
    for t in passes:
        t.start()
    for t in passes:
        t.join(timeout=10)
    assert max(peak) == 2


def test_a_workspace_bound_job_keeps_its_workspace_on_every_worker():
    workspace = config.create_workspace("Locomotion")
    seen = []

    def work(item):
        seen.append(config.workspace_id())
        return item

    with config.use_workspace(workspace["id"]):
        list(extract.run_concurrently([1, 2, 3], work, workers=3))
    assert seen == [workspace["id"]] * 3


def test_a_retag_job_reads_the_corpus_it_was_queued_in(monkeypatch):
    workspace = config.create_workspace("Locomotion")
    with config.use_workspace(workspace["id"]):
        store.save_paper(store.new_paper("roe2024gait", title="Gait"))
        store.add_claim("roe2024gait", {"text": "Legs.", "tags": []})
    seen = []

    def retag(key):
        seen.append((config.workspace_id(), store.load_paper(key)["title"]))
        return store.load_paper(key)

    monkeypatch.setattr(extract, "retag_paper", retag)
    with config.use_workspace(workspace["id"]):
        job = server._new_job("retag")
    server._run_retag(job, ["roe2024gait"])
    assert seen == [(workspace["id"], "Gait")]
    assert (job["state"], job["detail"]) == ("done", "1 papers")
