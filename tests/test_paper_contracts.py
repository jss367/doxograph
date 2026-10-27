"""Validate saved paper shapes while preserving older and future fields."""

import json

import pytest
from fastapi.testclient import TestClient

from doxograph import server, store


@pytest.mark.parametrize("fields", [
    {"title": 42}, {"authors": [None]}, {"year": "2026"}, {"labels": "favorite"},
    {"claim_seq": "one"}, {"source": ["arxiv"]}, {"history": [None]},
    {"proposed_tags": [{"name": False}]},
    {"pdf_file": {"sha256": 123}},
    {"extraction": {"usage": {"input_tokens": "many"}}},
    {"claims": [{"id": "damaged-c1", "text": 42}]},
    {"claims": [{"id": "damaged-c1", "tags": [None]}]},
    {"claims": [{"id": "damaged-c1", "reviewed": "false"}]},
    {"claims": [{"id": "damaged-c1", "ledger_links": [{"claim": []}]}]},
    {"claims": [{"id": "damaged-c1", "quote_verified": "yes"}]},
])
def test_malformed_paper_fields_are_reported_without_hiding_healthy_research(fields):
    store.save_paper(store.new_paper("healthy"))
    damaged = store.new_paper("damaged", **fields)
    path = store.paper_path("damaged")
    store.write_json(path, damaged)
    before = path.read_bytes()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/state")
        assert response.status_code == 200
        assert [p["key"] for p in response.json()["papers"]] == ["healthy"]
        [issue] = response.json()["storage_issues"]
        assert issue["path"] == str(path)
        assert client.patch("/api/papers/damaged", json={"notes": "Overwrite?"}).status_code == 500
    assert path.read_bytes() == before


@pytest.mark.parametrize("fields", [{"title": None}, {"authors": "Ada"},
    {"claims": [{"id": "study-c1", "text": []}]}])
def test_invalid_writes_leave_the_saved_paper_and_history_untouched(fields):
    store.save_paper(store.new_paper("study", notes="Saved writing"))
    before = store.paper_path("study").read_bytes()
    changed = store.load_paper("study")
    changed.update(fields)
    with pytest.raises(ValueError, match="invalid paper field"):
        store.save_paper(changed)
    assert store.paper_path("study").read_bytes() == before


@pytest.mark.parametrize("note", [None, "My old note", "PDF download failed: Offline"])
def test_older_records_migrate_without_filling_or_rewriting_unrelated_fields(note):
    path = store.paper_path("legacy")
    original = {"key": "legacy", "authors": None, "notes": note,
                "claims": [{"id": "legacy-c4", "text": "Old wording", "tags": [], "future": {"keep": True}}],
                "history": [{"id": "edit", "at": "2026-01-01", "claim": "legacy-c7", "fields": None}],
                "future": {"setting": "keep"}}
    store.write_json(path, original)
    before = path.read_bytes()
    loaded = store.load_paper("legacy")
    assert loaded["claim_seq"] == 7
    assert loaded["authors"] is None
    assert "labels" not in loaded
    assert "summary" not in loaded
    assert loaded["claims"][0]["future"] == original["claims"][0]["future"]
    assert loaded["future"] == original["future"]
    if note and note.startswith(store.LEGACY_ERROR):
        assert loaded["error"] == note and loaded["notes"] == ""
    else:
        assert loaded["notes"] == (note or "") and loaded["error"] == ""
    assert path.read_bytes() == before
    store.save_paper(loaded)
    saved = json.loads(path.read_text())
    assert saved["future"] == original["future"]
    assert saved["claims"][0]["future"] == original["claims"][0]["future"]
    created = store.add_claim("legacy", {"text": "A new claim"})
    assert created["id"] == "legacy-c8"
