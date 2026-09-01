"""Tensions: pairs of claims from different papers that disagree."""

import pytest
from fastapi.testclient import TestClient

from doxograph import export, extract, server, store, __main__


def _paper(key, title, author, year, *claims):
    paper = store.new_paper(key, title=title, authors=[author], year=year)
    store.save_paper(paper)
    ids = []
    for text, tags in claims:
        ids.append(store.add_claim(key, {"text": text, "tags": tags, "reviewed": True})["id"])
    return ids


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    a, = _paper("doe2026recovery", "Recovery under steering", "Jane Doe", 2026,
                ("Llama-3 70B recovers in 46% of rollouts.", ["recovery-rate"]))
    b, c = _paper("li2025steer", "Steering does not wash out", "Bo Li", 2025,
                  ("Steered models almost never return to the task.", ["recovery-rate"]),
                  ("Recovery is unaffected by scale.", ["recovery-rate", "scaling"]))
    return a, b, c


def shown():
    return {r["id"]: r for r in store.claim_rows()}


def test_tension_topics_need_two_papers():
    build_corpus()
    assert store.tension_topics() == ["recovery-rate"]   # scaling has one paper


def test_record_adds_open_tensions_and_drops_same_paper_and_unknown_pairs():
    a, b, c = build_corpus()
    result = store.record_tensions("recovery-rate", [
        {"claims": [a, b], "kind": "contradiction", "note": "Doe sees recovery; Li does not."},
        {"claims": [b, c], "kind": "tension", "note": "same paper"},
        {"claims": [a, "li2025steer-c99"], "kind": "tension", "note": "invented id"},
        {"claims": [a, a], "kind": "tension", "note": "one claim twice"},
    ], shown())
    assert result == {"added": 1, "reopened": 0, "kept": 0}
    rows = store.tension_rows()
    assert len(rows) == 1
    tension = rows[0]
    assert tension["status"] == "open"
    assert tension["kind"] == "contradiction"
    assert tension["topics"] == ["recovery-rate"]
    assert tension["stale"] is False
    assert {r["id"] for r in tension["claims"]} == {a, b}


def test_rerun_keeps_a_decision_and_the_pair_order_does_not_matter():
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "first"}], shown())
    tid = store.tension_rows()[0]["id"]
    store.set_tension_status(tid, "dismissed")

    result = store.record_tensions("recovery-rate", [
        {"claims": [b, a], "kind": "contradiction", "note": "second"},
    ], shown())
    assert result == {"added": 0, "reopened": 0, "kept": 1}
    [tension] = store.tension_rows()
    assert tension["id"] == tid
    assert tension["status"] == "dismissed"
    assert tension["note"] == "first"          # the model does not get to remake a decision


def test_editing_a_claim_marks_the_tension_stale_and_a_rerun_reopens_it():
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "first"}], shown())
    tid = store.tension_rows()[0]["id"]
    store.set_tension_status(tid, "confirmed")

    store.update_claim("doe2026recovery", a, {"text": "Llama-3 70B recovers in 4.6% of rollouts."})
    [tension] = store.tension_rows()
    assert tension["stale"] is True
    assert tension["status"] == "confirmed"     # still the reviewer's call until re-judged

    result = store.record_tensions("recovery-rate", [
        {"claims": [a, b], "kind": "tension", "note": "second"},
    ], shown())
    assert result == {"added": 0, "reopened": 1, "kept": 0}
    [tension] = store.tension_rows()
    assert tension["status"] == "open"
    assert tension["note"] == "second"
    assert tension["stale"] is False


def test_a_claim_edited_during_the_model_call_leaves_the_tension_stale():
    a, b, _ = build_corpus()
    snapshot = shown()                          # what the prompt was built from
    store.update_claim("doe2026recovery", a, {"text": "Llama-3 70B recovers in 4.6% of rollouts."})
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "old text"}], snapshot)
    [tension] = store.tension_rows()
    assert tension["stale"] is True             # the judgment is about text nobody can see any more
    assert tension["fingerprints"][a] == store.claim_fingerprint(snapshot[a])

    result = store.record_tensions("recovery-rate", [
        {"claims": [a, b], "kind": "tension", "note": "new text"},
    ], shown())
    assert result == {"added": 0, "reopened": 1, "kept": 0}
    assert store.tension_rows()[0]["stale"] is False


def test_a_pair_found_under_two_topics_is_one_tension():
    a, b, _ = build_corpus()
    store.update_claim("doe2026recovery", a, {"tags": ["recovery-rate", "scaling"]})
    store.update_claim("li2025steer", b, {"tags": ["recovery-rate", "scaling"]})
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    store.record_tensions("scaling", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    [tension] = store.tension_rows()
    assert tension["topics"] == ["recovery-rate", "scaling"]


def test_deleting_a_claim_removes_its_tensions_from_view_and_from_the_next_write():
    a, b, c = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    store.delete_claim("li2025steer", b)
    assert store.tension_rows() == []
    store.record_tensions("recovery-rate", [], shown())
    assert store.load_tensions() == []


def test_an_unreadable_ledger_is_reported_and_never_written_over():
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    path = store.tensions_path()
    path.write_text('{"seq": 1, "tensions": [', encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        store.record_tensions("recovery-rate", [], shown())
    with pytest.raises(ValueError):
        store.tension_rows()
    assert path.read_text(encoding="utf-8") == '{"seq": 1, "tensions": ['
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="should hold an object"):
        store.set_tension_status("t1", "confirmed")


def test_status_must_be_known_and_tension_must_exist():
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    tid = store.tension_rows()[0]["id"]
    try:
        store.set_tension_status(tid, "maybe")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown status was accepted")
    try:
        store.set_tension_status("t999", "open")
    except KeyError:
        pass
    else:
        raise AssertionError("a missing tension was updated")


def test_find_tensions_skips_a_topic_with_one_paper_without_calling_the_model(monkeypatch):
    build_corpus()
    monkeypatch.setattr(extract, "client", lambda: (_ for _ in ()).throw(AssertionError("called")))
    assert extract.find_tensions("scaling") == {"added": 0, "reopened": 0, "kept": 0, "returned": 0}


def test_find_tensions_records_what_the_model_returns(monkeypatch):
    a, b, _ = build_corpus()
    captured = {}

    class Text:
        type = "text"
        def __init__(self, text): self.text = text

    class Response:
        stop_reason = "end_turn"
        content = [Text('{"tensions": [{"claims": ["%s", "%s"], "kind": "contradiction", '
                        '"note": "Doe (2026) sees recovery; Li (2025) does not."}]}' % (a, b))]

    class Messages:
        def create(self, **kwargs):
            captured.update(kwargs)
            return Response()

    class Client:
        messages = Messages()

    monkeypatch.setattr(extract, "client", lambda: Client())
    result = extract.find_tensions("recovery-rate")
    assert result == {"added": 1, "reopened": 0, "kept": 0, "returned": 1}
    prompt = captured["messages"][0]["content"]
    assert "Doe (2026)" in prompt and "Li et al." not in prompt
    assert a in prompt and b in prompt
    assert "How often a model returns to task." in prompt
    [tension] = store.tension_rows()
    assert tension["kind"] == "contradiction"


def test_api_state_carries_tensions_and_status_can_be_changed():
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "n"}], shown())
    with TestClient(server.app) as client:
        state = client.get("/api/state").json()
        [tension] = state["tensions"]
        assert tension["status"] == "open"
        assert state["tension_statuses"] == ["open", "confirmed", "dismissed"]

        r = client.patch(f"/api/tensions/{tension['id']}", json={"status": "confirmed"})
        assert r.status_code == 200 and r.json()["status"] == "confirmed"
        assert client.patch(f"/api/tensions/{tension['id']}", json={"status": "maybe"}).status_code == 422
        assert client.patch("/api/tensions/t999", json={"status": "open"}).status_code == 404

        assert client.delete(f"/api/tensions/{tension['id']}").status_code == 200
        assert client.get("/api/state").json()["tensions"] == []


def test_api_find_tensions_queues_nothing_without_two_papers_on_a_topic():
    _paper("solo2026", "Alone", "Ann Solo", 2026, ("Only claim.", ["recovery-rate"]))
    with TestClient(server.app) as client:
        assert client.post("/api/tensions", json={}).json() == {"queued": 0}


def test_export_lists_open_and_confirmed_tensions_but_not_dismissed():
    a, b, c = build_corpus()
    store.update_claim("li2025steer", c, {"tags": ["recovery-rate"]})
    store.record_tensions("recovery-rate", [
        {"claims": [a, b], "kind": "contradiction", "note": "Keep this one."},
        {"claims": [a, c], "kind": "tension", "note": "Drop this one."},
    ], shown())
    drop = next(t for t in store.tension_rows() if t["note"] == "Drop this one.")
    store.set_tension_status(drop["id"], "dismissed")
    html = export.render()
    assert "Where the papers disagree" in html
    assert "Keep this one." in html
    assert "Drop this one." not in html


def test_cli_lists_tensions_without_calling_the_model(capsys):
    a, b, _ = build_corpus()
    store.record_tensions("recovery-rate", [{"claims": [a, b], "kind": "tension", "note": "The note."}], shown())
    assert __main__.main(["tensions", "--list"]) == 0
    out = capsys.readouterr().out
    assert "t1" in out and "open" in out and "The note." in out
    assert "1 tensions, 1 open" in out
