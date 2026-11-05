"""Findings: tensions, agreements and syntheses as one pass and one page."""

import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from doxograph import extract, job_queue, server, store


@pytest.fixture(autouse=True)
def quiet_queue(monkeypatch):
    """Jobs and timers left by another test must not hold the pass back."""
    now, timers = [0.0], []

    class Timer:
        def __init__(self, delay, function, args=()):
            self.at, self.function, self.args = now[0] + delay, function, args
            self.cancelled = self.fired = False
            self.daemon = False

        def start(self):
            timers.append(self)

        def cancel(self):
            self.cancelled = True

    def wait(predicate, timeout=5.0):
        until = now[0] + timeout
        while not predicate():
            timer = min((t for t in timers if not t.cancelled and not t.fired),
                        key=lambda t: t.at, default=None)
            if timer is None or timer.at > until:
                now[0] = until
                return predicate()
            now[0] = timer.at
            timer.fired = True
            timer.function(*timer.args)
        return True

    monkeypatch.setattr(job_queue, 'threading', SimpleNamespace(Timer=Timer))
    monkeypatch.setattr(job_queue, '_auto_findings_stopping', False)
    monkeypatch.setattr(sys.modules[__name__], '_wait', wait)
    def clear():
        with job_queue._auto_findings_lock:
            for timer in job_queue._auto_findings_timers.values():
                timer.cancel()
            job_queue._auto_findings_timers.clear()
        with job_queue._jobs_lock:
            job_queue._jobs.clear()
    clear()
    yield
    clear()


def _paper(key, title, author, year, *claims):
    store.save_paper(store.new_paper(key, title=title, authors=[author], year=year))
    return [store.add_claim(key, {"text": text, "tags": tags, "reviewed": True})["id"]
            for text, tags in claims]


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    store.add_tag("scaling", "")
    a, = _paper("doe2026recovery", "Recovery under steering", "Jane Doe", 2026,
                ("Steered Llama-3 70B returns to the task in about half of rollouts.", ["recovery-rate"]))
    b, c = _paper("li2025steer", "Steering washes out", "Bo Li", 2025,
                  ("Steered models never return to the task.", ["recovery-rate"]),
                  ("Recovery is unaffected by scale.", ["recovery-rate", "scaling"]))
    return a, b, c


def test_api_update_findings_queues_one_job_over_the_topics_two_papers_share(monkeypatch):
    build_corpus()   # recovery-rate has two papers; scaling has one
    submitted = []
    monkeypatch.setattr(job_queue._pool, "submit",
                        lambda fn, job, topics, force: submitted.append((fn.args[0].__name__, topics, force)))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/findings", json={"topics": ["scaling", "nonesuch"]}).json() == {"queued": 0}
        assert client.post("/api/findings", json={}).json() == {"queued": 1}
        assert max(job_queue._jobs.values(), key=lambda j: j["id"])["label"] == "findings in 1 topics"
        assert client.post("/api/findings", json={"force": True}).json() == {"queued": 1}
    assert submitted == [("_run_findings", ["recovery-rate"], False), ("_run_findings", ["recovery-rate"], True)]


def test_api_update_findings_queues_nothing_without_two_papers_on_a_topic():
    _paper("solo2026", "Alone", "Ann Solo", 2026, ("Only claim.", ["recovery-rate"]))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/findings", json={}).json() == {"queued": 0}


def test_the_pass_runs_tensions_then_agreements_then_syntheses_and_sums_them_up(monkeypatch):
    order = []

    def tensions(topic, rows=None, tags=None, force=False):
        order.append(("tensions", topic, force))
        return {"added": 2, "reopened": 0, "kept": 0, "returned": 2, "skipped": False}

    def agreements(topic, rows=None, tags=None, force=False):
        order.append(("agreements", topic, force))
        return {"added": 1, "grown": 0, "reopened": 0, "kept": 0, "returned": 1, "skipped": False}

    def synthesize(topic, rows=None, tags=None, force=False):
        order.append(("syntheses", topic, force))
        return {"written": topic == "recovery-rate", "skipped": topic != "recovery-rate", "claims": 2, "papers": 2}

    monkeypatch.setattr(extract, "find_tensions", tensions)
    monkeypatch.setattr(extract, "find_agreements", agreements)
    monkeypatch.setattr(extract, "synthesize_topic", synthesize)
    job = job_queue._new_job("findings in 2 topics")
    job_queue._run_findings(job, ["recovery-rate", "scaling"], True)
    stages = [stage for stage, _, _ in order]
    # Every tension is found before any agreement, and every agreement before
    # any synthesis: the synthesis prompt reads the tensions on file.
    assert stages == ["tensions"] * 2 + ["agreements"] * 2 + ["syntheses"] * 2
    assert all(force for _, _, force in order)
    assert job["state"] == "done"
    assert job["detail"] == "2 topics, 4 tensions new, 2 agreements new, 1 syntheses written"


def test_a_stage_that_fails_in_one_topic_does_not_stop_the_later_stages(monkeypatch):
    asked = []

    def tensions(topic, rows=None, tags=None, force=False):
        asked.append(("tensions", topic))
        if topic == "scaling":
            raise RuntimeError("refused")
        return {"added": 1, "reopened": 0, "kept": 0, "returned": 1, "skipped": False}

    def agreements(topic, rows=None, tags=None, force=False):
        asked.append(("agreements", topic))
        if topic == "scaling":
            raise RuntimeError("timed out")
        return {"added": 0, "grown": 0, "reopened": 0, "kept": 0, "returned": 0, "skipped": True}

    def synthesize(topic, rows=None, tags=None, force=False):
        asked.append(("syntheses", topic))
        return {"written": True, "skipped": False, "claims": 2, "papers": 2}

    monkeypatch.setattr(extract, "find_tensions", tensions)
    monkeypatch.setattr(extract, "find_agreements", agreements)
    monkeypatch.setattr(extract, "synthesize_topic", synthesize)
    job = job_queue._new_job("findings in 2 topics")
    job_queue._run_findings(job, ["recovery-rate", "scaling"])
    assert sorted(asked) == sorted([(s, t) for s in ("tensions", "agreements", "syntheses")
                                    for t in ("recovery-rate", "scaling")])
    assert job["state"] == "error"
    # One topic failed twice: counted once, with both failures on record.
    assert job["detail"] == ("1 of 2 topics failed, 1 tensions new, 0 agreements new, 2 syntheses written; "
                             "tensions in scaling: RuntimeError: refused; "
                             "agreements in scaling: RuntimeError: timed out")


def test_a_reworded_synthesis_prompt_moves_the_synthesis_basis_and_not_the_tension_basis(monkeypatch):
    """The synthesis version is part of what a synthesis was asked and not
    part of what a tension was, so a reworded synthesis prompt writes the
    syntheses again without sending the tension pass back over every topic."""
    from doxograph import config
    build_corpus()
    synthesis_before = store.synthesis_prompt_basis("recovery-rate")
    tension_before = extract._pass_extra("recovery-rate", "How often a model returns to task.", "")
    monkeypatch.setattr(config, "SYNTHESIS_VERSION", config.SYNTHESIS_VERSION + 1)
    assert store.synthesis_prompt_basis("recovery-rate") != synthesis_before
    assert extract._pass_extra("recovery-rate", "How often a model returns to task.", "") == tension_before


def _claims_for(key):
    def extract_paper(k, keep_reviewed=False):
        store.add_claim(k, {"text": f"A claim from {k}.", "tags": ["recovery-rate"], "reviewed": True})
    return extract_paper


def _findings_jobs():
    return [j for j in job_queue._jobs.values() if j.get("kind") == "findings"]


def _wait(predicate, timeout=5.0):
    raise AssertionError('The inert clock fixture must replace this helper')


def test_reading_papers_queues_one_findings_pass_once_the_last_one_is_read(monkeypatch):
    """Two papers read close together are answered by one pass, queued once no
    paper is still being read and the delay has run out."""
    store.add_tag("recovery-rate", "")
    store.save_paper(store.new_paper("a2026", title="A"))
    store.save_paper(store.new_paper("b2026", title="B"))
    monkeypatch.setattr(extract, "extract_paper", _claims_for("x"))
    monkeypatch.setattr(job_queue, "AUTO_FINDINGS_DELAY", 0.1)
    submitted = []
    monkeypatch.setattr(job_queue._pool, "submit", lambda fn, job, *args: submitted.append((fn.args[0].__name__, job, args)))
    for key in ("a2026", "b2026"):
        job_queue._run_extract(job_queue._new_job(key), key, False)
    assert _wait(lambda: submitted)
    assert [name for name, _, _ in submitted] == ["_run_findings"]
    job = submitted[0][1]
    assert job["kind"] == "findings" and job["automatic"] is True and job["label"] == "findings in 1 topics"
    assert submitted[0][2] == (["recovery-rate"], False)
    assert not _wait(lambda: len(submitted) > 1, timeout=0.4)


def test_the_pass_waits_while_a_paper_is_still_being_read(monkeypatch):
    store.add_tag("recovery-rate", "")
    store.save_paper(store.new_paper("a2026", title="A"))
    store.save_paper(store.new_paper("b2026", title="B"))
    monkeypatch.setattr(extract, "extract_paper", _claims_for("x"))
    monkeypatch.setattr(job_queue, "AUTO_FINDINGS_DELAY", 0.1)
    submitted = []
    monkeypatch.setattr(job_queue._pool, "submit", lambda fn, job, *args: submitted.append(fn.args[0].__name__))
    store.add_claim("b2026", {"text": "A claim from b.", "tags": ["recovery-rate"], "reviewed": True})
    reading = job_queue._new_job("b2026", key="b2026", state="reading")
    job_queue._run_extract(job_queue._new_job("a2026"), "a2026", False)
    assert not _wait(lambda: submitted, timeout=0.4)   # b is still being read
    job_queue._set(reading, state="done")
    assert _wait(lambda: submitted)
    assert submitted == ["_run_findings"]


def test_nothing_is_queued_with_the_switch_off_or_analysis_off(monkeypatch):
    from doxograph import config
    store.add_tag("recovery-rate", "")
    store.save_paper(store.new_paper("a2026", title="A"))
    monkeypatch.setattr(extract, "extract_paper", _claims_for("x"))
    monkeypatch.setattr(job_queue, "AUTO_FINDINGS_DELAY", 0.05)
    submitted = []
    monkeypatch.setattr(job_queue._pool, "submit", lambda fn, job, *args: submitted.append(fn.args[0].__name__))
    config.set_auto_findings(False)
    job_queue._run_extract(job_queue._new_job("a2026"), "a2026", False)
    assert not _wait(lambda: submitted, timeout=0.3)
    config.set_auto_findings(True)
    config.set_ai_enabled(False)
    job_queue.schedule_findings()
    assert not _wait(lambda: submitted, timeout=0.3)


def test_a_retag_queues_the_pass_too(monkeypatch):
    store.add_tag("recovery-rate", "")
    a, b, _ = build_corpus()
    monkeypatch.setattr(extract, "retag_paper", lambda key: {"changed": 1})
    monkeypatch.setattr(job_queue, "AUTO_FINDINGS_DELAY", 0.05)
    submitted = []
    monkeypatch.setattr(job_queue._pool, "submit", lambda fn, job, *args: submitted.append(fn.args[0].__name__))
    job_queue._run_retag(job_queue._new_job("retag"), ["doe2026recovery"])
    assert _wait(lambda: submitted)
    assert submitted == ["_run_findings"]


def test_the_setting_is_saved_and_shown_in_the_state():
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        first = client.get("/api/state")
        assert first.json()["auto_findings"] is True
        assert client.put("/api/settings", json={"auto_findings": False}).json() == {"ai_enabled": True, "auto_findings": False}
        changed = client.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
        assert changed.status_code == 200 and changed.json()["auto_findings"] is False
