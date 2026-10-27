"""Agent queries preserve provenance, workspace boundaries, and pagination."""

import pytest
from fastapi.testclient import TestClient

from doxograph import config, notebook, research_notes, server, store


@pytest.fixture
def client():
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        yield client


@pytest.fixture
def corpus():
    store.add_tag("recovery", "Return to the original task")
    store.add_tag("scaling")
    ids = []
    for key, title, author in [("alpha", "Café recovery", "Ada Roe"),
                                ("beta", "Steering models", "Bo Li")]:
        store.save_paper(store.new_paper(key, title=title, authors=[author], year=2026,
                                         labels=["read-next"], notes="A personal paper note."))
        c = store.add_claim(key, {"text": "Models recover after steering.", "tags": ["recovery"],
                                  "evidence": "n=10", "quote": "Recovery was observed.",
                                  "locator": "p. 3", "note": "Check the baseline.", "reviewed": key == "alpha"})
        ids.append(c["id"])
    return ids


def test_claim_search_filters_and_keeps_original_evidence(client, corpus):
    data = client.get("/api/query/claims", params={"q": "BASELINE café", "topic": "recovery",
                                                  "reviewed": True}).json()
    assert data["workspace"] == "default" and data["total"] == 1
    [claim] = data["items"]
    assert claim["id"] == corpus[0] and claim["paper"] == "alpha"
    assert claim["quote"] == "Recovery was observed." and claim["locator"] == "p. 3"
    assert claim["paper_authors"] == ["Ada Roe"] and claim["paper_year"] == 2026
    assert "quote_verified" in claim
    assert client.get("/api/query/claims", params={"reviewed": False}).json()["total"] == 1
    assert client.get("/api/query/claims", params={"paper": "beta"}).json()["items"][0]["id"] == corpus[1]
    assert client.get("/api/query/claims", params={"topic": "scaling"}).json()["items"] == []


def test_paper_search_includes_unextracted_papers_and_pages_stably(client, corpus):
    store.save_paper(store.new_paper("empty", title="Recovery without claims", abstract="Rare concept"))
    assert client.get("/api/query/papers", params={"q": "rare concept"}).json()["items"][0]["key"] == "empty"
    assert client.get("/api/query/papers", params={"topic": "recovery", "label": "read-next"}).json()["total"] == 2
    assert client.get("/api/query/papers", params={"q": "ada 2026"}).json()["total"] == 1
    for route in ["papers", "claims", "topics"]:
        first = client.get(f"/api/query/{route}", params={"limit": 1}).json()
        assert len(first["items"]) == 1 and first["next_offset"] == 1
        second = client.get(f"/api/query/{route}", params={"limit": 1, "offset": 1}).json()
        assert first["items"] != second["items"]
    paper = client.get("/api/query/papers/alpha").json()["paper"]
    assert paper["n_claims"] == 1 and paper["notes"] == "A personal paper note."
    assert "claims" not in paper and "history" not in paper
    assert client.get("/api/query/papers/missing").status_code == 404
    assert client.get("/api/query/claims", params={"offset": 100}).json()["next_offset"] is None
    topics = client.get("/api/query/topics", params={"q": "original task"}).json()["items"]
    assert topics == [{"name": "recovery", "description": "Return to the original task", "n_claims": 2}]


def test_queries_never_cross_workspaces(client, corpus):
    other = config.create_workspace("Other research")["id"]
    with config.use_workspace(other):
        store.save_paper(store.new_paper("alpha", title="Private other paper"))
        notebook.save_notes(notebook.NotesUpdate(notes="Other workspace notes", previous=""))
    headers = {"X-Doxograph-Workspace": other}
    assert client.get("/api/query/papers/alpha", headers=headers).json()["paper"]["title"] == "Private other paper"
    for route in ["claims", "topics", "evidence?kind=tensions"]:
        data = client.get(f"/api/query/{route}", headers=headers).json()
        assert data["workspace"] == other and data["items"] == []
    assert client.get("/api/query/notes/workspace", headers=headers).json()["body"] == "Other workspace notes"
    assert client.get("/api/query/notes/workspace").json()["body"] == ""
    assert client.get("/api/query/papers", headers={"X-Doxograph-Workspace": "missing"}).status_code == 404


def test_saved_analysis_preserves_decisions_staleness_and_references(client, corpus):
    live = {c["id"]: c for c in store.claim_rows()}
    store.record_tensions("recovery", [{"claims": corpus, "note": "Different conditions"}], live)
    store.record_agreements("recovery", [{"claims": corpus, "note": "Both report recovery"}], live)
    store.record_synthesis("recovery", "A synthesis with sources.", live)
    store.set_tension_status(store.tension_rows()[0]["id"], "dismissed")
    store.update_claim("alpha", corpus[0], {"text": "A revised claim."})
    before = {p: p.read_bytes() for p in config.data_dir().rglob("*.json")}
    for kind in ["agreements", "tensions", "syntheses"]:
        response = client.get("/api/query/evidence", params={"kind": kind, "topic": "recovery"})
        assert response.status_code == 200
        [row] = response.json()["items"]
        assert row["stale"] is True
        if kind == "tensions":
            assert row["status"] == "dismissed"
        if kind != "syntheses":
            assert row["claims"] == [{"paper": "alpha", "id": corpus[0]}, {"paper": "beta", "id": corpus[1]}]
        assert client.get("/api/query/evidence", params={"kind": kind, "topic": "missing"}).json()["items"] == []
    assert before == {p: p.read_bytes() for p in config.data_dir().rglob("*.json")}


def test_personal_notes_are_previewed_and_read_in_bounded_chunks(client):
    text = "A long personal note. " * 1000
    saved = research_notes.create_note(research_notes.NoteBody(title="Reading plan", body=text))
    data = client.get("/api/query/notes", params={"q": "plan personal"}).json()
    assert data["total"] == 1 and data["items"][0]["id"] == saved["id"]
    assert len(data["items"][0]["preview"]) == 300
    parts = []
    offset = 0
    while offset is not None:
        chunk = client.get(f"/api/query/notes/{saved['id']}", params={"limit": 5000, "offset": offset}).json()
        assert len(chunk["body"]) <= 5000 and chunk["length"] == len(text)
        parts.append(chunk["body"])
        offset = chunk["next_offset"]
    assert "".join(parts) == text
    assert client.get("/api/query/notes/missing").status_code == 404


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"reviewed": "maybe"}, {"q": "x" * 1001}])
def test_invalid_claim_queries_are_rejected(client, params):
    assert client.get("/api/query/claims", params=params).status_code == 422


def test_unreadable_papers_are_reported_as_partial_results(client, corpus):
    store.paper_path("broken").write_text("{invalid", encoding="utf-8")
    response = client.get("/api/query/claims").json()
    assert response["total"] == 2
    assert response["storage_issues"][0]["paper"] == "broken"
