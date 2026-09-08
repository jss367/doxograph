"""Agreements: groups of claims from different papers that assert the same finding."""

from fastapi.testclient import TestClient

from doxograph import __main__, export, extract, server, store


def _paper(key, title, author, year, *claims):
    store.save_paper(store.new_paper(key, title=title, authors=[author], year=year))
    return [store.add_claim(key, {"text": text, "tags": tags, "reviewed": True})["id"]
            for text, tags in claims]


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    a, = _paper("doe2026recovery", "Recovery under steering", "Jane Doe", 2026,
                ("Steered Llama-3 70B returns to the task in about half of rollouts.", ["recovery-rate"]))
    b, c = _paper("li2025steer", "Steering washes out", "Bo Li", 2025,
                  ("Steered models return to the task in roughly half of rollouts.", ["recovery-rate"]),
                  ("Recovery is unaffected by scale.", ["recovery-rate", "scaling"]))
    d, = _paper("roe2024vectors", "Steering vectors", "Al Roe", 2024,
                ("About half of steered rollouts recover.", ["recovery-rate"]))
    return a, b, c, d


def shown():
    return {r["id"]: r for r in store.claim_rows()}


def test_record_adds_an_open_group_and_drops_one_paper_and_unknown_members():
    a, b, c, d = build_corpus()
    result = store.record_agreements("recovery-rate", [
        {"claims": [a, b, "li2025steer-c99"], "note": "About half recover."},
        {"claims": [b, c], "note": "one paper only"},
        {"claims": [a], "note": "one claim"},
    ], shown())
    assert result == {"added": 1, "grown": 0, "reopened": 0, "kept": 0}
    [row] = store.agreement_rows()
    assert row["status"] == "open" and row["n_papers"] == 2 and row["stale"] is False
    assert {c["id"] for c in row["claims"]} == {a, b}
    assert row["topics"] == ["recovery-rate"]


def test_a_group_that_grows_is_the_same_agreement_reopened_for_the_new_member():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b], "note": "half"}], shown())
    aid = store.agreement_rows()[0]["id"]
    store.set_agreement_status(aid, "confirmed")
    result = store.record_agreements("recovery-rate", [{"claims": [d, b, a], "note": "half, three papers"}], shown())
    assert result == {"added": 0, "grown": 1, "reopened": 0, "kept": 0}
    [row] = store.agreement_rows()
    assert row["id"] == aid and row["status"] == "open" and row["n_papers"] == 3
    assert row["note"] == "half, three papers"


def test_a_returned_subset_and_a_repeat_keep_the_decision():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b, d], "note": "first"}], shown())
    aid = store.agreement_rows()[0]["id"]
    store.set_agreement_status(aid, "dismissed")
    result = store.record_agreements("recovery-rate", [
        {"claims": [a, b], "note": "smaller"},
        {"claims": [d, b, a], "note": "same again"},
    ], shown())
    assert result == {"added": 0, "grown": 0, "reopened": 0, "kept": 2}
    [row] = store.agreement_rows()
    assert row["status"] == "dismissed" and row["note"] == "first"


def test_editing_or_deleting_a_member_marks_it_stale_and_a_rerun_reopens_it():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b, d], "note": "half"}], shown())
    aid = store.agreement_rows()[0]["id"]
    store.set_agreement_status(aid, "confirmed")
    store.update_claim("doe2026recovery", a, {"text": "Steered Llama-3 70B never returns to the task."})
    assert store.agreement_rows()[0]["stale"] is True
    result = store.record_agreements("recovery-rate", [{"claims": [a, b, d], "note": "re-judged"}], shown())
    assert result["reopened"] == 1
    [row] = store.agreement_rows()
    assert row["status"] == "open" and row["stale"] is False
    # Deleting a member leaves the rest as the agreement, stale until re-judged.
    store.set_agreement_status(aid, "confirmed")
    store.delete_claim("roe2024vectors", d)
    [row] = store.agreement_rows()
    assert row["stale"] is True and row["n_papers"] == 2
    # A later pass on any topic prunes the dead member from the record; the
    # group is still stale, since the confirmation was of three papers.
    store.record_agreements("scaling", [], shown())
    [row] = store.agreement_rows()
    assert row["stale"] is True and row["status"] == "confirmed"
    assert {c["id"] for c in row["claims"]} == {a, b}
    # Re-judging the reduced group reopens it against the current members.
    result = store.record_agreements("recovery-rate", [{"claims": [a, b], "note": "two"}], shown())
    assert result["reopened"] == 1
    [row] = store.agreement_rows()
    assert row["stale"] is False and row["status"] == "open" and row["note"] == "two"
    store.set_agreement_status(aid, "confirmed")
    # Down to one paper, it disappears.
    store.delete_claim("li2025steer", b)
    assert store.agreement_rows() == []


def test_deciding_refreshes_the_basis_and_reopening_does_not():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b], "note": "half"}], shown())
    aid = store.agreement_rows()[0]["id"]
    store.update_claim("doe2026recovery", a, {"text": "Reworded."})
    assert store.agreement_rows()[0]["stale"] is True
    store.set_agreement_status(aid, "confirmed")
    assert store.agreement_rows()[0]["stale"] is False
    store.update_claim("doe2026recovery", a, {"text": "Reworded again."})
    store.set_agreement_status(aid, "open")
    assert store.agreement_rows()[0]["stale"] is True


def test_renaming_or_deleting_a_tag_rewrites_agreement_topics():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b], "note": "half"}], shown())
    store.rename_tag("recovery-rate", "recovery")
    assert store.agreement_rows()[0]["topics"] == ["recovery"]
    store.delete_tag("recovery")
    assert store.agreement_rows()[0]["topics"] == []


def test_find_agreements_records_what_the_model_returns(monkeypatch):
    a, b, c, d = build_corpus()
    import json
    calls = []

    class Response:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": json.dumps(
            {"agreements": [{"claims": [a, b, d], "note": "About half of rollouts recover."}]})})()]

    class Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return Response()

    monkeypatch.setattr(extract, "client", lambda: type("C", (), {"messages": Messages()})())
    result = extract.find_agreements("recovery-rate")
    assert result == {"added": 1, "grown": 0, "reopened": 0, "kept": 0, "returned": 1}
    assert "Doe (2026)" in calls[0]["messages"][0]["content"]
    assert extract.find_agreements("scaling")["returned"] == 0     # one paper: no call
    assert len(calls) == 1


def test_api_state_routes_and_export():
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b, d], "note": "About half recover."}], shown())
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        [row] = client.get("/api/state").json()["agreements"]
        assert row["n_papers"] == 3
        assert client.patch(f"/api/agreements/{row['id']}", json={"status": "confirmed"}).json()["status"] == "confirmed"
        assert client.patch(f"/api/agreements/{row['id']}", json={"status": "maybe"}).status_code == 422
        assert client.patch("/api/agreements/a99", json={"status": "open"}).status_code == 404
        html = export.render()
        assert "Where the papers agree" in html and "3 papers" in html and "About half recover." in html
        assert client.post("/api/agreements", json={"topics": ["scaling"]}).json() == {"queued": 0}
        assert client.delete(f"/api/agreements/{row['id']}").json() == {"deleted": row["id"]}
        assert client.get("/api/state").json()["agreements"] == []
        assert "Where the papers agree" not in export.render()


def test_cli_lists_agreements_without_calling_the_model(capsys):
    a, b, c, d = build_corpus()
    store.record_agreements("recovery-rate", [{"claims": [a, b, d], "note": "About half recover."}], shown())
    assert __main__.main(["agreements", "--list"]) == 0
    out = capsys.readouterr().out
    assert "3 papers" in out and "[Roe 2024]" in out and "1 agreements, 1 open" in out
