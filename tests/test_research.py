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
