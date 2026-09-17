"""The research context and the ledger are edited through the app."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import extract, server, store


def client() -> TestClient:
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_context_round_trips_and_reaches_the_prompt():
    with client() as c:
        response = c.put("/api/context", json={"text": "  Steering and recovery.\n"})
        assert response.json() == {"context": "Steering and recovery."}
        assert c.get("/api/state").json()["context"] == "Steering and recovery."
    assert store.context_path().read_text() == "Steering and recovery.\n"
    assert extract.context_block() == "Steering and recovery."


def test_an_empty_context_falls_back_to_the_default_wording():
    with client() as c:
        c.put("/api/context", json={"text": ""})
    assert store.load_context() == ""
    assert "No research context" in extract.context_block()


def test_a_line_moved_into_the_context_is_a_different_prompt():
    """The description and the context both hold newlines of their own, so
    running them together with one more would read "A\nB" beside "C" as the
    same prompt as "A" beside "B\nC". They are different prompts, and a topic
    whose claims had not moved would never be asked again."""
    one = extract._pass_extra("steering", "A\nB", "C")
    two = extract._pass_extra("steering", "A", "B\nC")
    assert one != two


def test_a_pass_is_signed_with_the_context_the_model_was_given():
    """Read once for the signature and again for the prompt, an edit landing
    between the two would sign the answer with a context the prompt never
    carried — and undoing the edit would leave it looking current."""
    with client() as c:
        c.put("/api/context", json={"text": "The context as it was."})
        context = extract.context_block()
        c.put("/api/context", json={"text": "Edited while the call was out."})
    assert extract._pass_extra("steering", "", context) != \
        extract._pass_extra("steering", "", extract.context_block())


def test_the_ledger_is_replaced_whole():
    with client() as c:
        response = c.put("/api/ledger", json={"claims": [
            {"id": " L1 ", "text": " Recovery is path-dependent. "},
            {"id": "L2", "text": ""},
        ]})
        assert response.json()["ledger"] == [
            {"id": "L1", "text": "Recovery is path-dependent."},
            {"id": "L2", "text": ""},
        ]
        assert c.put("/api/ledger", json={"claims": []}).json()["ledger"] == []
    assert store.load_ledger() == []


def test_ledger_ids_must_be_present_and_unique():
    with client() as c:
        assert c.put("/api/ledger", json={"claims": [{"id": "", "text": "x"}]}).status_code == 422
        response = c.put("/api/ledger", json={"claims": [{"id": "L1", "text": "a"}, {"id": "L1", "text": "b"}]})
        assert response.status_code == 422
        assert "used twice" in response.json()["detail"]
    assert store.load_ledger() == []


def test_the_ledger_written_through_the_api_is_what_extraction_links_against():
    with client() as c:
        c.put("/api/ledger", json={"claims": [{"id": "L1", "text": "Mine."}]})
    assert store.clean_ledger_links([{"claim": "L1", "relation": "supports", "note": ""},
                                     {"claim": "L9", "relation": "supports", "note": ""}]) == [
        {"claim": "L1", "relation": "supports", "note": ""}]
