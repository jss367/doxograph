"""Findings: tensions, agreements and syntheses as one pass and one page."""

from fastapi.testclient import TestClient

from doxograph import extract, job_queue, server, store


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
