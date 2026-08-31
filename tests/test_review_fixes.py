"""Regression tests for the review findings on the initial implementation."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from doxograph import extract, ingest, server, store


# --- proposed topics: accepting and discarding are different -------------

def paper_with_proposals() -> None:
    paper = store.new_paper("doe2026study", title="A Study")
    paper["proposed_tags"] = [
        {"name": "wanted", "description": "keep this one"},
        {"name": "unwanted", "description": "discard this one"},
    ]
    store.save_paper(paper)


def test_discarding_a_proposal_keeps_it_out_of_the_vocabulary():
    paper_with_proposals()
    with TestClient(server.app) as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"discard": ["unwanted"]}
        )
    assert response.status_code == 200
    assert response.json()["discarded"] == ["unwanted"]
    assert store.tag_names() == []
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["wanted"]


def test_accepting_a_proposal_adds_it_with_its_description():
    paper_with_proposals()
    with TestClient(server.app) as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"accept": ["wanted"]}
        )
    assert response.json()["accepted"] == ["wanted"]
    assert store.load_tags() == [{"name": "wanted", "description": "keep this one"}]
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["unwanted"]


def test_accept_and_discard_in_one_request():
    paper_with_proposals()
    with TestClient(server.app) as client:
        client.post(
            "/api/papers/doe2026study/proposed-tags",
            json={"accept": ["wanted"], "discard": ["unwanted"]},
        )
    assert store.tag_names() == ["wanted"]
    assert store.load_paper("doe2026study")["proposed_tags"] == []


def test_unknown_proposal_names_are_ignored():
    paper_with_proposals()
    with TestClient(server.app) as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"accept": ["never-proposed"]}
        )
    assert response.json()["accepted"] == []
    assert store.tag_names() == []


# --- a blank hand-added claim is a draft, not a reviewed claim ------------

def test_blank_claim_is_not_marked_reviewed():
    store.save_paper(store.new_paper("doe2026study"))
    draft = store.add_claim("doe2026study", {})
    assert draft["reviewed"] is False
    assert store.load_paper("doe2026study")["status"] == "extracted"


def test_claim_added_with_text_is_reviewed():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "A holds for B."})
    assert claim["reviewed"] is True


# --- retag must not overwrite edits made while the model was working -----

class FakeResponse:
    def __init__(self, payload: dict):
        block = type("Block", (), {"type": "text", "text": json.dumps(payload)})()
        self.content = [block]
        self.stop_reason = "end_turn"
        self.usage = None


class FakeMessages:
    def __init__(self, payload: dict, during_call=None):
        self.payload = payload
        self.during_call = during_call

    def create(self, **kwargs):
        if self.during_call:
            self.during_call()  # stands in for a concurrent edit while we wait
        return FakeResponse(self.payload)


class FakeClient:
    def __init__(self, payload: dict, during_call=None):
        self.messages = FakeMessages(payload, during_call)


@pytest.fixture
def retag_corpus():
    store.add_tag("alpha")
    store.add_tag("beta")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    first = store.add_claim("doe2026study", {"text": "First claim.", "tags": ["alpha"]})
    second = store.add_claim("doe2026study", {"text": "Second claim.", "tags": ["alpha"]})
    return first["id"], second["id"]


def test_retag_preserves_an_edit_made_during_the_model_call(monkeypatch, retag_corpus):
    first_id, second_id = retag_corpus

    def concurrent_edit():
        store.update_claim("doe2026study", second_id, {"text": "Corrected by hand.", "reviewed": True})

    payload = {"assignments": [
        {"id": first_id, "tags": ["beta"]},
        {"id": second_id, "tags": ["beta"]},
    ]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload, concurrent_edit))

    extract.retag_paper("doe2026study")

    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[second_id]["text"] == "Corrected by hand."   # the edit survived
    assert claims[second_id]["reviewed"] is True
    assert claims[second_id]["tags"] == ["beta"]               # and the retag applied


def test_retag_drops_tags_absent_from_the_vocabulary(monkeypatch, retag_corpus):
    first_id, _ = retag_corpus
    payload = {"assignments": [{"id": first_id, "tags": ["beta", "invented"]}]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload))

    extract.retag_paper("doe2026study")
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[first_id]["tags"] == ["beta"]


def test_retag_leaves_claims_the_model_did_not_mention(monkeypatch, retag_corpus):
    first_id, second_id = retag_corpus
    payload = {"assignments": [{"id": first_id, "tags": ["beta"]}]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload))

    extract.retag_paper("doe2026study")
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[second_id]["tags"] == ["alpha"]


# --- an advertised PDF link may be relative -------------------------------

class FakePageResponse:
    def __init__(self, url: str, html: str):
        self.url = url
        self.text = html
        self.headers = {"content-type": "text/html"}

    def raise_for_status(self):
        return None


class FakePageClient:
    def __init__(self, url: str, html: str):
        self._response = FakePageResponse(url, html)

    def get(self, url, **kwargs):
        return self._response


@pytest.mark.parametrize("advertised,expected", [
    ("/papers/article.pdf", "https://journal.example.org/papers/article.pdf"),
    ("article.pdf", "https://journal.example.org/issue/article.pdf"),
    ("https://cdn.example.net/a.pdf", "https://cdn.example.net/a.pdf"),
])
def test_relative_citation_pdf_url_is_resolved(advertised, expected):
    html = f'<meta name="citation_pdf_url" content="{advertised}">'
    client = FakePageClient("https://journal.example.org/issue/landing", html)
    ref = ingest.resolve_page("https://journal.example.org/issue/landing", client)
    assert ref.kind == "pdf"
    assert ref.value == expected


def test_arxiv_id_on_a_landing_page_still_wins_over_a_pdf_link():
    html = '<a href="https://arxiv.org/abs/2602.06941">preprint</a>' \
           '<meta name="citation_pdf_url" content="/local.pdf">'
    client = FakePageClient("https://example.org/landing", html)
    ref = ingest.resolve_page("https://example.org/landing", client)
    assert (ref.kind, ref.value) == ("arxiv", "2602.06941")
