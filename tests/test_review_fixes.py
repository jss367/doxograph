"""Regression tests for the review findings on the initial implementation."""

from __future__ import annotations

import io
import json
import os
import threading
import time

import anthropic
import pytest
from fastapi.testclient import TestClient

import re
from pathlib import Path

import httpx

from doxograph import __main__, bib, config, export, extract, ingest, server, store


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
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"discard": ["unwanted"]}
        )
    assert response.status_code == 200
    assert response.json()["discarded"] == ["unwanted"]
    assert store.tag_names() == []
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["wanted"]


def test_accepting_a_proposal_adds_it_with_its_description():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"accept": ["wanted"]}
        )
    assert response.json()["accepted"] == ["wanted"]
    assert store.load_tags() == [{"name": "wanted", "description": "keep this one"}]
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["unwanted"]


def test_accept_and_discard_in_one_request():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/papers/doe2026study/proposed-tags",
            json={"accept": ["wanted"], "discard": ["unwanted"]},
        )
    assert store.tag_names() == ["wanted"]
    assert store.load_paper("doe2026study")["proposed_tags"] == []


def test_unknown_proposal_names_are_ignored():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
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
    """A claim rewritten during the call keeps its text and its old tags.

    The answer for it describes wording that no longer exists, so it is not
    applied. Every other claim still takes the answer.
    """
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
    assert claims[second_id]["tags"] == ["alpha"]              # tags for the old text
    assert claims[first_id]["tags"] == ["beta"]                # untouched claim retagged


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


def test_a_cited_arxiv_link_does_not_hijack_the_page():
    """An arXiv link in the bibliography is a different paper from this one.

    This replaces an earlier test that asserted the opposite. That test encoded
    the bug: any arXiv link anywhere in the document used to win, so pasting a
    journal URL could ingest and extract whichever preprint it happened to cite.
    """
    html = (
        '<head><meta name="citation_pdf_url" content="/local.pdf"></head>'
        '<body><ol class="references">'
        '<li><a href="https://arxiv.org/abs/2602.06941">some cited preprint</a></li>'
        '</ol></body>'
    )
    client = FakePageClient("https://journal.example.org/article/123", html)
    ref = ingest.resolve_page("https://journal.example.org/article/123", client)
    assert (ref.kind, ref.value) == ("pdf", "https://journal.example.org/local.pdf")


# --- round 2 findings -----------------------------------------------------

@pytest.mark.parametrize("token,expected", [
    ("10.1038/s41586-021-03819-2.", "10.1038/s41586-021-03819-2"),
    ("10.1038/s41586-021-03819-2,", "10.1038/s41586-021-03819-2"),
    ("(10.1145/3442188.3445922)", "10.1145/3442188.3445922"),
    ("[10.1038/x.y].", "10.1038/x.y"),
    ("doi:10.1038/example.", "10.1038/example"),
    ("https://doi.org/10.1038/example;", "10.1038/example"),
    # parentheses that belong to the identifier survive
    ("10.1002/(SICI)1097-0258(19980815)17:15", "10.1002/(SICI)1097-0258(19980815)17:15"),
])
def test_citation_punctuation_is_stripped_from_dois(token, expected):
    refs, unknown = ingest.parse_refs(token)
    assert unknown == []
    assert [(r.kind, r.value) for r in refs] == [("doi", expected)]


def test_normalize_doi_leaves_a_clean_doi_alone():
    assert ingest.normalize_doi("10.1038/s41586-021-03819-2") == "10.1038/s41586-021-03819-2"


def test_a_doi_inside_prose_is_still_recognized_per_token():
    refs, unknown = ingest.parse_refs("see 10.1145/3442188.3445922, and also 10.1038/x.y.")
    assert [r.value for r in refs] == ["10.1145/3442188.3445922", "10.1038/x.y"]
    assert unknown == ["see", "and", "also"]


def test_add_claim_honors_an_explicit_reviewed_flag():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "A holds for B.", "reviewed": False})
    assert claim["reviewed"] is False
    assert store.load_paper("doe2026study")["status"] == "extracted"


def test_creating_a_claim_through_the_api_stores_the_whole_patch():
    store.save_paper(store.new_paper("doe2026study"))
    store.add_tag("alpha")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/papers/doe2026study/claims", json={
            "text": "A holds for B.", "kind": "method", "strength": "headline",
            "tags": ["alpha"], "evidence": "n = 10", "quote": "verbatim", "locator": "p. 1",
            "reviewed": True,
        })
    claim = response.json()
    assert (claim["text"], claim["kind"], claim["strength"]) == ("A holds for B.", "method", "headline")
    assert claim["tags"] == ["alpha"] and claim["reviewed"] is True
    assert len(store.load_paper("doe2026study")["claims"]) == 1


def test_summary_exposes_updated_so_the_client_can_version_its_cache():
    store.save_paper(store.new_paper("doe2026study"))
    summary = store.summarize(store.load_paper("doe2026study"))
    assert summary["updated"]

    store.add_claim("doe2026study", {"text": "A holds for B."})
    later = store.summarize(store.load_paper("doe2026study"))
    assert later["updated"] >= summary["updated"]


# --- round 3 findings -----------------------------------------------------

def test_citation_arxiv_id_identifies_the_page():
    html = ('<head><meta name="citation_arxiv_id" content="2602.06941">'
            '<meta name="citation_pdf_url" content="/local.pdf"></head>'
            '<body><a href="https://arxiv.org/abs/1111.22222">cited</a></body>')
    client = FakePageClient("https://journal.example.org/a", html)
    ref = ingest.resolve_page("https://journal.example.org/a", client)
    assert (ref.kind, ref.value) == ("arxiv", "2602.06941")


def test_canonical_url_identifies_an_arxiv_page():
    html = ('<head><link rel="canonical" href="https://arxiv.org/abs/2602.06941"></head>'
            '<body><a href="https://arxiv.org/abs/1111.22222">cited</a></body>')
    client = FakePageClient("https://arxiv.org/abs/2602.06941v2", html)
    ref = ingest.resolve_page("https://arxiv.org/abs/2602.06941v2", client)
    assert (ref.kind, ref.value) == ("arxiv", "2602.06941")


def test_an_arxiv_link_in_the_head_is_a_last_resort():
    html = ('<head><meta name="og:title" content="A paper">'
            '<link rel="alternate" href="https://arxiv.org/abs/2602.06941"></head>'
            '<body><a href="https://arxiv.org/abs/1111.22222">cited</a></body>')
    client = FakePageClient("https://blog.example.org/post", html)
    ref = ingest.resolve_page("https://blog.example.org/post", client)
    assert (ref.kind, ref.value) == ("arxiv", "2602.06941")


def test_citation_doi_is_preferred_over_the_pdf_link():
    html = ('<head><meta name="citation_doi" content="10.1145/3442188.3445922">'
            '<meta name="citation_pdf_url" content="/local.pdf"></head>')
    client = FakePageClient("https://journal.example.org/a", html)
    ref = ingest.resolve_page("https://journal.example.org/a", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")


def test_an_unidentifiable_page_says_what_to_do_instead():
    html = '<head><title>Some page</title></head><body>no identifiers here</body>'
    client = FakePageClient("https://example.org/page", html)
    with pytest.raises(ValueError, match="paste the arXiv ID"):
        ingest.resolve_page("https://example.org/page", client)


@pytest.mark.parametrize("stored,pasted", [
    ("2602.06941", "2602.06941v2"),
    ("2602.06941v1", "2602.06941v2"),
    ("2602.06941v1", "2602.06941"),
    ("2602.06941V2", "2602.06941v2"),
])
def test_arxiv_versions_are_one_paper_for_deduplication(stored, pasted):
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study",
        source={"kind": "arxiv", "id": stored, "url": ""},
    ))
    meta = {"source": {"kind": "arxiv", "id": pasted}, "doi": ""}
    assert ingest.find_existing(meta) == "doe2026study"


def test_different_arxiv_papers_are_not_merged():
    store.save_paper(store.new_paper(
        "doe2026study", source={"kind": "arxiv", "id": "2602.06941v1", "url": ""}))
    assert ingest.find_existing({"source": {"kind": "arxiv", "id": "2602.06942v1"}, "doi": ""}) is None


def test_dois_match_regardless_of_trailing_punctuation():
    store.save_paper(store.new_paper("doe2026study", doi="10.1038/example",
                                     source={"kind": "doi", "id": "10.1038/example"}))
    assert ingest.find_existing({"source": {}, "doi": "10.1038/example."}) == "doe2026study"


def test_concurrent_uploads_of_the_same_filename_keep_their_own_pdfs(monkeypatch):
    """Two uploads sharing a basename must not stage to the same path."""
    import threading
    import time

    def fake_guess(path, client, display_name=None):
        # Read, pause, read again: a shared staging path shows up as a mismatch.
        first = path.read_bytes()
        time.sleep(0.15)
        second = path.read_bytes()
        assert first == second, "staging file changed underneath this upload"
        marker = first.split(b"marker:")[1].split(b"\n")[0].decode()
        return {
            "title": f"Paper {marker}", "authors": [f"Author {marker}"], "year": 2026,
            "abstract": "", "venue": "", "doi": "",
            "source": {"kind": "file", "id": f"{marker}.pdf", "url": "", "pdf_url": ""},
        }

    monkeypatch.setattr(ingest, "guess_from_pdf", fake_guess)

    results, errors = {}, []

    def upload(marker):
        try:
            data = b"%PDF-1.4\nmarker:" + marker.encode() + b"\n" + bytes([0] * 64)
            key, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
            results[marker] = key
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(m,)) for m in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(set(results.values())) == 2, f"uploads collided: {results}"
    for marker, key in results.items():
        assert marker.encode() in store.pdf_path(key).read_bytes()
    leftovers = list(config.pdfs_dir().glob(".incoming-*"))
    assert leftovers == [], f"staging files left behind: {leftovers}"


# --- the CLI must report failure -----------------------------------------

def test_add_returns_nonzero_when_every_reference_fails(monkeypatch, capsys):
    def boom(ref, client=None):
        raise RuntimeError("arXiv is down")

    monkeypatch.setattr(ingest, "ingest_ref", boom)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    assert "arXiv is down" in capsys.readouterr().err


def test_add_returns_nonzero_for_an_unreadable_reference(monkeypatch, capsys):
    args = __main__.build_parser().parse_args(["add", "--no-extract", "definitely-not-a-reference"])
    assert args.func(args) == 1
    assert "could not read reference" in capsys.readouterr().err


def test_add_returns_zero_when_the_reference_lands(monkeypatch):
    def land(ref, client=None):
        store.save_paper(store.new_paper("doe2026study"))
        store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")
        return "doe2026study", True

    monkeypatch.setattr(ingest, "ingest_ref", land)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 0


def test_add_reports_a_paper_that_arrived_without_its_pdf(monkeypatch, capsys):
    def land_without_pdf(ref, client=None):
        paper = store.new_paper("doe2026study")
        paper["notes"] = "PDF download failed: 503"
        store.save_paper(paper)
        return "doe2026study", True

    monkeypatch.setattr(ingest, "ingest_ref", land_without_pdf)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    err = capsys.readouterr().err
    assert "no PDF stored" in err and "503" in err


def test_add_returns_nonzero_when_extraction_fails(monkeypatch, capsys):
    monkeypatch.setattr(ingest, "ingest_ref", lambda ref, client=None: ("doe2026study", True))
    store.save_paper(store.new_paper("doe2026study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")   # extraction needs one

    def boom(key, keep_reviewed=True):
        raise RuntimeError("model refused")

    monkeypatch.setattr(extract, "extract_paper", boom)
    args = __main__.build_parser().parse_args(["add", "2602.06941"])
    assert args.func(args) == 1
    assert "extraction failed" in capsys.readouterr().err


def test_extract_returns_nonzero_when_a_paper_fails(monkeypatch, capsys):
    store.save_paper(store.new_paper("doe2026study"))

    def boom(key, keep_reviewed=True):
        raise RuntimeError("no PDF")

    monkeypatch.setattr(extract, "extract_paper", boom)
    args = __main__.build_parser().parse_args(["extract", "doe2026study"])
    assert args.func(args) == 1
    assert "no PDF" in capsys.readouterr().err


# --- round 4 findings -----------------------------------------------------

def test_concurrent_uploads_that_share_a_citekey_get_separate_papers(monkeypatch):
    """Two different papers can produce the same coarse key at the same moment."""
    import threading
    import time

    barrier = threading.Barrier(2)

    def fake_guess(path, client, display_name=None):
        marker = path.read_bytes().split(b"marker:")[1].split(b"\n")[0].decode()
        # Both workers reach the key decision together, which is what made
        # exists()-then-write unsafe.
        barrier.wait(timeout=5)
        time.sleep(0.05)
        return {
            # Same surname, year and first title word => same coarse citekey.
            "title": f"A Study of {marker}", "authors": ["Jane Doe"], "year": 2026,
            "abstract": "", "venue": "", "doi": "",
            "source": {"kind": "file", "id": f"{marker}.pdf", "url": "", "pdf_url": ""},
        }

    monkeypatch.setattr(ingest, "guess_from_pdf", fake_guess)
    results, errors = {}, []

    def upload(marker):
        try:
            data = b"%PDF-1.4\nmarker:" + marker.encode() + b"\n" + bytes(64)
            key, _ = ingest.ingest_pdf_bytes(data, f"{marker}.pdf")
            results[marker] = key
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(m,)) for m in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(set(results.values())) == 2, f"both uploads took the same key: {results}"
    for marker, key in results.items():
        assert marker.encode() in store.pdf_path(key).read_bytes()
        assert store.load_paper(key)["title"] == f"A Study of {marker}"


def test_overlapping_claim_updates_do_not_revert_each_other():
    """Two claims on one paper, reviewed at once: neither flag may be lost."""
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    first = store.add_claim("doe2026study", {"text": "One.", "reviewed": False})
    second = store.add_claim("doe2026study", {"text": "Two.", "reviewed": False})

    start = threading.Barrier(2)
    errors = []

    def mark(claim_id):
        try:
            start.wait(timeout=5)
            store.update_claim("doe2026study", claim_id, {"reviewed": True})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=mark, args=(c,)) for c in (first["id"], second["id"])]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[first["id"]]["reviewed"] is True
    assert claims[second["id"]]["reviewed"] is True


def test_many_overlapping_writes_all_survive():
    """Every claim added concurrently must be present, and the file stay valid."""
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    start = threading.Barrier(8)
    errors = []

    def add(n):
        try:
            start.wait(timeout=5)
            store.add_claim("doe2026study", {"text": f"Claim {n}."})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    claims = store.load_paper("doe2026study")["claims"]
    assert len(claims) == 8
    assert {c["text"] for c in claims} == {f"Claim {n}." for n in range(8)}
    assert len({c["id"] for c in claims}) == 8   # ids stayed unique under contention


def test_concurrent_writes_leave_no_temporary_files():
    import threading

    store.save_paper(store.new_paper("doe2026study"))
    threads = [threading.Thread(target=store.add_claim,
                                args=("doe2026study", {"text": f"C{n}."})) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert list(config.papers_dir().glob("*.tmp")) == []
    assert list(config.papers_dir().glob(".*")) == []


@pytest.mark.parametrize("advertised,expected", [
    ("/a.pdf?token=x&amp;download=1", "https://journal.example.org/a.pdf?token=x&download=1"),
    ("https://cdn.example.net/a.pdf?x=1&amp;y=2", "https://cdn.example.net/a.pdf?x=1&y=2"),
    # Entities are decoded here; percent-encoding is httpx's job at request time.
    ("/caf&eacute;.pdf", "https://journal.example.org/café.pdf"),
])
def test_html_entities_in_metadata_are_decoded(advertised, expected):
    html = f'<head><meta name="citation_pdf_url" content="{advertised}"></head>'
    client = FakePageClient("https://journal.example.org/issue/landing", html)
    ref = ingest.resolve_page("https://journal.example.org/issue/landing", client)
    assert ref.value == expected


def test_entities_are_decoded_in_a_doi_too():
    html = '<head><meta name="citation_doi" content="10.1145/3442188.3445922&nbsp;"></head>'
    client = FakePageClient("https://journal.example.org/a", html)
    ref = ingest.resolve_page("https://journal.example.org/a", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")


# --- round 5 findings -----------------------------------------------------

def test_reservation_is_visible_to_deduplication_immediately():
    """A reserved key must already carry its identity, not an empty placeholder."""
    key = store.reserve_key("doe2026study", source={"kind": "arxiv", "id": "2602.06941"},
                            doi="", title="A Study")
    assert ingest.find_existing({"source": {"kind": "arxiv", "id": "2602.06941v2"}, "doi": ""}) == key


def test_concurrent_ingests_of_one_paper_make_one_paper(monkeypatch):
    """Two requests for the same arXiv ID must not race past each other."""
    import threading
    import time

    barrier = threading.Barrier(2)
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""},
    }

    def fake_fetch(arxiv_id, client):
        barrier.wait(timeout=5)   # both arrive at the claim step together
        return {**meta, "source": {**meta["source"], "id": arxiv_id}}

    monkeypatch.setattr(ingest, "fetch_arxiv", fake_fetch)
    monkeypatch.setattr(ingest, "download_pdf",
                        lambda url, dest, client: (_ for _ in ()).throw(AssertionError("no pdf_url")))

    results, errors = {}, []

    def add(version):
        try:
            time.sleep(0.01)
            key, created = ingest.ingest_ref(ingest.Ref("arxiv", f"2602.06941{version}", ""))
            results[version] = (key, created)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(v,)) for v in ("v1", "v2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    keys = {key for key, _ in results.values()}
    assert len(keys) == 1, f"the same paper was ingested twice: {results}"
    assert sum(1 for _, created in results.values() if created) == 1
    assert len(store.paper_keys()) == 1


def test_concurrent_tag_accepts_both_land():
    """Two papers' proposals accepted at once must both reach the vocabulary."""
    import threading

    for key, tag in (("doe2026a", "alpha"), ("doe2026b", "beta")):
        paper = store.new_paper(key)
        paper["proposed_tags"] = [{"name": tag, "description": f"{tag} desc"}]
        store.save_paper(paper)

    start = threading.Barrier(2)
    errors = []

    client = TestClient(server.app, base_url="http://127.0.0.1:8765")

    def accept(key, tag):
        try:
            start.wait(timeout=5)   # build the client first, so the barrier is the only gate
            client.post(f"/api/papers/{key}/proposed-tags", json={"accept": [tag]})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=accept, args=a) for a in (("doe2026a", "alpha"), ("doe2026b", "beta"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, errors
    assert set(store.tag_names()) == {"alpha", "beta"}, "one accepted tag was dropped"


def test_many_concurrent_tag_adds_all_land():
    import threading

    start = threading.Barrier(6)

    def add(n):
        start.wait(timeout=5)
        store.add_tag(f"topic-{n}", f"number {n}")

    threads = [threading.Thread(target=add, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert set(store.tag_names()) == {f"topic-{n}" for n in range(6)}


def test_deleting_a_paper_while_a_merge_is_saving_leaves_no_ghost():
    """Remove must not race a completing re-read into a paper with no PDF.

    The merge parks between its load and its save. The delete is attempted at
    exactly that moment: without the lock it unlinks both files and the merge's
    save then recreates the JSON, leaving a paper whose PDF is gone.
    """
    import threading

    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")

    parked = threading.Event()
    delete_attempted = threading.Event()
    errors = []

    def merge():
        try:
            with store.paper_lock("doe2026study"):
                paper = store.load_paper("doe2026study")
                parked.set()
                delete_attempted.wait(timeout=2)   # give the delete its chance
                paper["summary"] = "merged"
                store.save_paper(paper)
        except Exception as exc:
            errors.append(exc)

    def remove():
        try:
            parked.wait(timeout=5)
            threading.Timer(0.2, delete_attempted.set).start()
            store.delete_paper("doe2026study")     # blocks on the lock when locked
            delete_attempted.set()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=merge), threading.Thread(target=remove)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert not store.paper_path("doe2026study").exists(), "the merge recreated a deleted paper"
    assert not store.pdf_path("doe2026study").exists(), "a ghost paper was left with no PDF"


def test_rename_and_accept_do_not_deadlock():
    """Lock order is vocabulary then paper on both paths."""
    import threading

    store.add_tag("old", "to be renamed")
    paper = store.new_paper("doe2026study")
    paper["proposed_tags"] = [{"name": "fresh", "description": "new one"}]
    store.save_paper(paper)
    store.add_claim("doe2026study", {"text": "X.", "tags": ["old"]})

    errors = []

    def rename():
        try:
            store.rename_tag("old", "renamed")
        except Exception as exc:
            errors.append(exc)

    def accept():
        try:
            with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
                client.post("/api/papers/doe2026study/proposed-tags", json={"accept": ["fresh"]})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=rename), threading.Thread(target=accept)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "deadlocked"
    assert not errors, errors
    assert "renamed" in store.tag_names() and "fresh" in store.tag_names()


# --- round 6 findings -----------------------------------------------------

def test_upload_metadata_comes_from_the_arrival_filename(monkeypatch):
    """The staging path is randomized; it must not reach the title or the key."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    data = b"%PDF-1.4\n" + bytes(64)
    key, created = ingest.ingest_pdf_bytes(data, "Attention_Is_All_You_Need.pdf")
    paper = store.load_paper(key)
    assert created
    assert paper["title"] == "Attention Is All You Need"
    assert paper["source"]["id"] == "Attention_Is_All_You_Need.pdf"
    assert "incoming" not in key and "incoming" not in paper["title"]


def test_the_same_upload_twice_gets_the_same_metadata(monkeypatch):
    """Randomized staging names used to make each upload look like a new paper."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    data = b"%PDF-1.4\n" + bytes(64)
    first, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
    second, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
    titles = {store.load_paper(k)["title"] for k in (first, second)}
    assert titles == {"paper"}, titles


def test_re_adding_a_paper_recovers_a_missing_pdf(monkeypatch):
    """A transient download failure must not make the paper unrecoverable."""
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "",
                   "pdf_url": "https://arxiv.org/pdf/2602.06941"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)

    attempts = {"n": 0}

    def flaky(url, client):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("network blip")
        path = config.pdfs_dir() / ".download-test.pdf"
        path.write_bytes(b"%PDF-1.4\nrecovered")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", flaky)

    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert created
    assert not store.pdf_path(key).exists()
    assert "PDF download failed" in store.load_paper(key)["notes"]

    again, created_again = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (again, created_again) == (key, False)
    assert store.pdf_path(key).read_bytes() == b"%PDF-1.4\nrecovered"
    assert store.load_paper(key)["notes"] == ""
    assert len(store.paper_keys()) == 1


def test_a_present_pdf_is_not_downloaded_again(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)
    calls = {"n": 0}

    def counted(url, client):
        calls["n"] += 1
        path = config.pdfs_dir() / f".download-{calls['n']}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", counted)
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert calls["n"] == 1


def test_publishing_a_pdf_for_a_removed_paper_leaves_no_orphan():
    """A download finishing after Remove must not recreate the PDF."""
    store.save_paper(store.new_paper("doe2026study"))
    staged = config.pdfs_dir() / ".download-staged.pdf"
    staged.write_bytes(b"%PDF-1.4\n")

    store.delete_paper("doe2026study")
    assert ingest.publish_pdf("doe2026study", staged) is False
    assert not store.pdf_path("doe2026study").exists()
    assert not staged.exists(), "the staged file was left behind"


def test_publishing_a_pdf_for_a_live_paper_succeeds():
    store.save_paper(store.new_paper("doe2026study"))
    staged = config.pdfs_dir() / ".download-staged.pdf"
    staged.write_bytes(b"%PDF-1.4\nbody")
    assert ingest.publish_pdf("doe2026study", staged) is True
    assert store.pdf_path("doe2026study").read_bytes() == b"%PDF-1.4\nbody"


def test_retag_reads_the_vocabulary_while_holding_its_lock(monkeypatch):
    """The invariant: `known` is read under the vocabulary lock, not before it.

    Asserting the interleaving directly needs the deletion to land in the gap
    between the snapshot and the save, which is not reproducible from outside.
    So this asserts the property that closes the gap instead: at the moment
    retag reads the vocabulary, this thread owns the vocabulary lock.
    """
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})

    held = []
    real_tag_names = store.tag_names

    def watched_tag_names():
        held.append(store._vocab._is_owned())
        return real_tag_names()

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"assignments": [{"id": claim["id"], "tags": ["alpha"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(store, "tag_names", watched_tag_names)
    extract.retag_paper("doe2026study")

    # The prompt-building read happens before the lock; the read that decides
    # what gets written must happen under it.
    assert held, "the vocabulary was never read"
    assert held[-1] is True, "retag applied tags using a vocabulary read outside the lock"


def test_retag_drops_a_tag_that_is_no_longer_in_the_vocabulary(monkeypatch):
    """Whatever the interleaving, an undeclared tag must never be written."""
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"assignments": [{"id": claim["id"], "tags": ["alpha", "never-declared"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    extract.retag_paper("doe2026study")
    assert store.load_paper("doe2026study")["claims"][0]["tags"] == ["alpha"]


# --- BibTeX escaping ------------------------------------------------------

def test_percent_in_a_url_is_escaped():
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", authors=["Jane Doe"], year=2026,
        source={"kind": "url", "id": "u", "url": "https://ex.org/a%20b?q=1&r=2"},
    ))
    text = bib.render()
    assert r"url = {https://ex.org/a\%20b?q=1\&r=2}" in text
    # every field line must still close its brace
    for line in text.splitlines():
        if " = {" in line:
            assert line.rstrip(",").endswith("}"), line


def test_percent_in_a_doi_is_escaped():
    store.save_paper(store.new_paper("doe2026study", title="A Study", authors=["Jane Doe"],
                                     year=2026, venue="Nature", doi="10.1000/a%2Fb"))
    assert r"doi = {10.1000/a\%2Fb}" in bib.render()


def test_tilde_in_a_url_is_escaped():
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", authors=["Jane Doe"], year=2026,
        source={"kind": "url", "id": "u", "url": "https://ex.org/~jane/p.pdf"},
    ))
    assert r"\textasciitilde{}jane" in bib.render()


# --- round 7 findings -----------------------------------------------------

def test_uploading_to_a_removed_paper_leaves_no_orphan_pdf(monkeypatch):
    """An upload publishes through the locked helper, like a download does."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")

    real_publish = ingest.publish_pdf

    removed = []

    def publish_after_removal(key, staging):
        # Stand in for Remove landing between the reservation and the copy.
        removed.append(key)
        store.delete_paper(key)
        return real_publish(key, staging)

    monkeypatch.setattr(ingest, "publish_pdf", publish_after_removal)

    # Reporting success here would mark the job done for a paper that no longer
    # exists, so the ingest now fails loudly instead.
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_pdf_bytes(b"%PDF-1.4\n" + bytes(64), "paper.pdf")

    assert removed, "the test did not exercise the removal path"
    assert not store.pdf_path(removed[0]).exists(), "an orphan PDF was left for a removed paper"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []
    assert list(config.pdfs_dir().glob(".download-*")) == []


def test_a_normal_upload_still_attaches_its_pdf(monkeypatch):
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody", "paper.pdf")
    assert created
    assert store.pdf_path(key).read_bytes() == b"%PDF-1.4\nbody"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_uploading_a_pdf_for_a_record_that_lacks_one_attaches_it(monkeypatch):
    """The existing-paper branch publishes through the helper too."""
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study",
        source={"kind": "arxiv", "id": "2602.06941", "url": ""},
    ))
    monkeypatch.setattr(ingest, "guess_from_pdf", lambda path, client, display_name=None: {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""},
    })
    key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody", "paper.pdf")
    assert (key, created) == ("doe2026study", False)
    assert store.pdf_path("doe2026study").read_bytes() == b"%PDF-1.4\nbody"
    assert len(store.paper_keys()) == 1


# --- round 8 findings -----------------------------------------------------

def test_paper_lock_is_held_across_processes(tmp_path):
    """A second OS process must block on the same paper's lock."""
    import subprocess
    import sys
    import textwrap
    import time

    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    # The child appends to a log under the lock; the parent holds it first.
    log = tmp_path / "order.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.paper_lock("doe2026study"):
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.paper_lock("doe2026study"):
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)                      # the child is blocked, not finished
        blocked_while_held = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked_while_held, "the child did not wait for the lock"
    assert child.returncode == 0
    assert log.read_text().split() == ["parent", "child"]


def test_paper_lock_is_reentrant_within_a_thread():
    """Nesting must not deadlock on the file lock's own descriptor."""
    store.save_paper(store.new_paper("doe2026study"))
    with store.paper_lock("doe2026study"):
        with store.paper_lock("doe2026study"):
            store.add_claim("doe2026study", {"text": "X."})   # takes it a third time
    assert len(store.load_paper("doe2026study")["claims"]) == 1


def test_vocabulary_lock_is_held_across_processes(tmp_path):
    import subprocess
    import sys
    import textwrap
    import time

    log = tmp_path / "vocab.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.vocab_lock():
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.vocab_lock():
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the vocabulary lock"
    assert log.read_text().split() == ["parent", "child"]


def test_needs_extraction_tracks_the_pdf_not_the_creation():
    store.save_paper(store.new_paper("doe2026study"))
    assert store.needs_extraction("doe2026study") is False      # no PDF yet

    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")
    assert store.needs_extraction("doe2026study") is True       # recovered

    store.add_claim("doe2026study", {"text": "X."})
    assert store.needs_extraction("doe2026study") is False      # already read
    assert store.needs_extraction("no-such-paper") is False


def test_recovering_a_pdf_makes_the_cli_read_the_paper(monkeypatch, capsys):
    """The recovery path must reach extraction, not stop at created=False."""
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "",
                   "pdf_url": "https://arxiv.org/pdf/2602.06941"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)

    attempts = {"n": 0}

    def flaky(url, client):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("blip")
        path = config.pdfs_dir() / ".download-t.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", flaky)

    read = []
    monkeypatch.setattr(extract, "extract_paper",
                        lambda key, keep_reviewed=True: read.append(key))

    args = __main__.build_parser().parse_args(["add", "2602.06941"])
    assert args.func(args) == 1              # first run: no PDF, reported
    assert read == []
    assert "no PDF stored" in capsys.readouterr().err

    assert args.func(args) == 0              # second run: recovered and read
    assert read == ["doe2026recovery"]


def open_descriptor_count() -> int:
    """How many file descriptors this process currently holds."""
    for probe in ("/dev/fd", "/proc/self/fd"):
        path = Path(probe)
        if path.is_dir():
            return len(list(path.iterdir()))
    pytest.skip("no way to count open descriptors on this platform")


def test_a_failed_download_closes_its_staging_descriptor():
    """An early failure must not leak the descriptor `mkstemp` handed back."""

    class Failing:
        def stream(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    # One failure first, so any one-off descriptors are already accounted for.
    with pytest.raises(httpx.ConnectError):
        ingest.fetch_pdf("https://example.org/x.pdf", Failing())

    before = open_descriptor_count()
    for _ in range(25):
        with pytest.raises(httpx.ConnectError):
            ingest.fetch_pdf("https://example.org/x.pdf", Failing())
    after = open_descriptor_count()

    assert after - before < 5, f"leaked about {after - before} descriptors over 25 failures"
    assert list(config.pdfs_dir().glob(".download-*")) == []


def test_a_failed_download_leaves_no_staging_file(monkeypatch):
    class NotAPdf:
        def stream(self, *args, **kwargs):
            class R:
                headers = {"content-type": "text/html"}

                def raise_for_status(self):
                    return None

                def iter_bytes(self, n):
                    yield b"<html>not a pdf</html>"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    with pytest.raises(ValueError, match="rather than a PDF"):
        ingest.fetch_pdf("https://example.org/x.pdf", NotAPdf())
    assert list(config.pdfs_dir().glob(".download-*")) == []


# --- round 9 findings -----------------------------------------------------

def test_claim_lock_is_held_across_processes(tmp_path):
    """Two processes ingesting the same paper must not both reserve a key."""
    import subprocess
    import sys
    import textwrap
    import time

    log = tmp_path / "claim.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.claim_lock():
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.claim_lock():
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(1.0)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the claim lock"
    assert log.read_text().split() == ["parent", "child"]


def test_two_processes_ingesting_one_paper_make_one_paper(tmp_path):
    """The whole check-and-reserve transaction, across real processes.

    An outcome guard, not a proof. Both children busy-wait to a shared
    wall-clock instant, but the window between `find_existing` and
    `reserve_key` is microseconds and process startup jitter is larger, so this
    still passes with the cross-process lock removed. It is kept because it
    exercises the real two-process path end to end and would catch a coarser
    regression. `test_claim_lock_is_held_across_processes` is the test that
    actually fails without the lock.
    """
    import subprocess
    import sys
    import textwrap
    import time

    start_at = time.time() + 3.0
    script = textwrap.dedent(f"""
        import os, time
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import ingest, store
        meta = {{
            "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
            "abstract": "", "venue": "arXiv", "doi": "",
            "source": {{"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""}},
        }}
        ingest.fetch_arxiv = lambda i, c: meta
        while time.time() < {start_at!r}:      # busy-wait to the shared instant
            pass
        key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
        print(f"{{key}} {{created}}")
    """)

    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    results = [p.communicate(timeout=60) for p in procs]
    outputs = [out.strip() for out, _ in results]

    assert all(p.returncode == 0 for p in procs), results
    keys = {line.split()[0] for line in outputs}
    created = [line.split()[1] for line in outputs]
    assert len(keys) == 1, f"two processes made two papers: {outputs}"
    assert created.count("True") == 1, f"both processes claimed to create it: {outputs}"
    assert len(store.paper_keys()) == 1


def test_the_vocabulary_is_never_observed_half_written():
    """Readers do not take the lock, so the write has to be atomic."""
    import threading

    for n in range(12):
        store.add_tag(f"topic-{n}", f"description number {n}")

    stop = threading.Event()
    bad = []

    def reader():
        while not stop.is_set():
            names = store.tag_names()
            if names and len(names) < 12:
                bad.append(sorted(names))

    def writer():
        for n in range(40):
            store.add_tag(f"extra-{n}", "x")

    watcher = threading.Thread(target=reader)
    watcher.start()
    writer()
    stop.set()
    watcher.join(timeout=5)

    assert bad == [], f"a partial vocabulary was visible: {bad[:3]}"
    assert len(store.tag_names()) == 52


def test_the_vocabulary_is_replaced_rather_than_truncated(monkeypatch):
    """The atomicity mechanism itself.

    The concurrent-reader test above asserts the invariant but cannot reliably
    hit the truncation window. This one is deterministic: a write that goes
    through `os.replace` cannot be observed half-done, and an in-place
    `write_text` never calls it.
    """
    import os as os_module

    store.add_tag("alpha", "first")
    replaced = []
    real_replace = os_module.replace

    def watched_replace(src_path, dest_path, *args, **kwargs):
        replaced.append(str(dest_path))
        return real_replace(src_path, dest_path, *args, **kwargs)

    monkeypatch.setattr(store.os, "replace", watched_replace)
    store.add_tag("beta", "second")

    assert any(name.endswith("tags.yaml") for name in replaced), (
        f"tags.yaml was not published through os.replace: {replaced}")
    assert sorted(store.tag_names()) == ["alpha", "beta"]


def test_ledger_writes_are_atomic_too():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    assert store.load_ledger() == [{"id": "L1", "text": "A claim."}]
    store.save_ledger([{"id": "L1", "text": "A claim."}, {"id": "L2", "text": "Another."}])
    assert [c["id"] for c in store.load_ledger()] == ["L1", "L2"]


# --- round 11 findings ----------------------------------------------------

def test_a_page_pdf_link_survives_doi_resolution():
    """A publisher page that gives both a DOI and a PDF must not lose the PDF."""
    html = ('<head><meta name="citation_doi" content="10.1145/3442188.3445922">'
            '<meta name="citation_pdf_url" content="/pdf/article.pdf"></head>')
    client = FakePageClient("https://journal.example.org/issue/a", html)
    ref = ingest.resolve_page("https://journal.example.org/issue/a", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")
    assert ref.pdf_url == "https://journal.example.org/pdf/article.pdf"


def test_the_carried_pdf_is_used_when_crossref_has_none(monkeypatch):
    crossref = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "Journal", "doi": "10.1145/3442188.3445922",
        "source": {"kind": "doi", "id": "10.1145/3442188.3445922",
                   "url": "https://doi.org/10.1145/3442188.3445922", "pdf_url": ""},
    }
    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi, client: dict(crossref))
    fetched = []

    def fake_fetch(url, client):
        fetched.append(url)
        path = config.pdfs_dir() / ".download-t.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", fake_fetch)
    ref = ingest.Ref("doi", "10.1145/3442188.3445922", "",
                     pdf_url="https://journal.example.org/pdf/article.pdf")
    key, created = ingest.ingest_ref(ref)

    assert created
    assert fetched == ["https://journal.example.org/pdf/article.pdf"]
    assert store.pdf_path(key).exists()
    assert store.needs_extraction(key) is True


def test_crossrefs_own_pdf_wins_over_the_carried_one(monkeypatch):
    crossref = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "Journal", "doi": "10.1145/3442188.3445922",
        "source": {"kind": "doi", "id": "10.1145/3442188.3445922", "url": "",
                   "pdf_url": "https://crossref.example.org/a.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi, client: dict(crossref))
    fetched = []
    monkeypatch.setattr(ingest, "fetch_pdf", lambda url, client: (
        fetched.append(url), config.pdfs_dir() / ".d.pdf")[1])
    (config.pdfs_dir() / ".d.pdf").write_bytes(b"%PDF-1.4\n")

    ingest.ingest_ref(ingest.Ref("doi", "10.1145/3442188.3445922", "",
                                 pdf_url="https://journal.example.org/pdf/article.pdf"))
    assert fetched == ["https://crossref.example.org/a.pdf"]


# --- ledger links must name a real claim ---------------------------------

def test_extraction_drops_a_link_to_a_claim_that_does_not_exist():
    store.save_ledger([{"id": "L1", "text": "A real claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "s", "relevance": "r", "proposed_tags": [],
        "claims": [{
            "text": "A finding.", "kind": "finding", "strength": "headline", "tags": [],
            "evidence": "", "quote": "", "locator": "",
            "ledger_links": [
                {"claim": "L1", "relation": "supports", "note": "real"},
                {"claim": "L9", "relation": "supports", "note": "invented"},
                {"claim": "", "relation": "supports", "note": "empty"},
            ],
        }],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    links = paper["claims"][0]["ledger_links"]
    assert [l["claim"] for l in links] == ["L1"]
    assert links[0]["note"] == "real"


def test_a_link_is_dropped_if_the_ledger_changed_during_the_call():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "A finding.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "",
                    "ledger_links": [{"claim": "L1", "relation": "supports", "note": "n"}]}],
    }
    store.save_ledger([{"id": "L2", "text": "Renumbered."}])   # while the model worked
    paper = extract.merge_extraction("doe2026study", payload)
    assert paper["claims"][0]["ledger_links"] == []


def test_the_api_cannot_attach_a_bogus_ledger_link():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {
        "text": "X.",
        "ledger_links": [{"claim": "L1", "relation": "supports", "note": "ok"},
                         {"claim": "nope", "relation": "supports", "note": "bad"}],
    })
    assert [l["claim"] for l in claim["ledger_links"]] == ["L1"]

    updated = store.update_claim("doe2026study", claim["id"], {
        "ledger_links": [{"claim": "also-nope", "relation": "contradicts", "note": ""}]})
    assert updated["ledger_links"] == []


def test_a_link_with_no_ledger_at_all_is_dropped():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {
        "text": "X.", "ledger_links": [{"claim": "L1", "relation": "supports", "note": ""}]})
    assert claim["ledger_links"] == []


# --- round 12 findings ----------------------------------------------------

@pytest.mark.parametrize("title,expected", [
    ("study}", r"study\}"),
    ("a{b}c", r"a\{b\}c"),
    ("100% {done}", r"100\% \{done\}"),
])
def test_braces_in_a_title_are_escaped(title, expected):
    store.save_paper(store.new_paper("doe2026study", title=title,
                                     authors=["Jane Doe"], year=2026))
    text = bib.render()
    assert f"title = {{{expected}}}" in text


def test_a_backslash_in_metadata_is_escaped():
    store.save_paper(store.new_paper("doe2026study", title=r"C:\path", authors=["Jane Doe"],
                                     year=2026))
    assert r"title = {C:\textbackslash{}path}" in bib.render()


def delimiter_braces(text: str) -> str:
    """Drop escaped literals so only structural braces remain countable."""
    return re.sub(r"\\[{}]", "", text)


def test_every_bibtex_entry_has_balanced_braces():
    """A literal brace used to unbalance the field and swallow the rest."""
    for n, title in enumerate(["study}", "{a}", "plain", "a{b", "100% }x{"]):
        store.save_paper(store.new_paper(f"doe2026s{n}", title=title,
                                         authors=[f"A B{n}"], year=2026, venue="Journal"))
    text = bib.render()
    for entry_text in text.split("\n\n"):
        if not entry_text.strip():
            continue
        structural = delimiter_braces(entry_text)
        assert structural.count("{") == structural.count("}"), entry_text
        # every field line closes what it opens
        for line in entry_text.splitlines():
            if " = {" in line:
                assert line.rstrip(",").endswith("}"), line
                inner = delimiter_braces(line)
                assert inner.count("{") == inner.count("}"), line


def test_a_metadata_free_upload_named_with_a_brace_round_trips(monkeypatch):
    """The exact case from the report: an upload called `study}.pdf`."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    ingest.ingest_pdf_bytes(b"%PDF-1.4\n" + bytes(32), "study}.pdf")
    text = bib.render()
    assert r"\}" in text
    structural = delimiter_braces(text)
    assert structural.count("{") == structural.count("}"), text


# --- round 13 findings ----------------------------------------------------

def test_a_deleted_claims_id_is_never_reused():
    """A recycled id lets an in-flight retag or PATCH land on the replacement."""
    store.save_paper(store.new_paper("doe2026study"))
    first = store.add_claim("doe2026study", {"text": "One."})
    second = store.add_claim("doe2026study", {"text": "Two."})
    assert (first["id"], second["id"]) == ("doe2026study-c1", "doe2026study-c2")

    store.delete_claim("doe2026study", second["id"])       # the highest one
    third = store.add_claim("doe2026study", {"text": "Three."})
    assert third["id"] == "doe2026study-c3", "the deleted id was recycled"


def test_ids_stay_unique_after_deleting_everything():
    store.save_paper(store.new_paper("doe2026study"))
    seen = set()
    for _ in range(5):
        claim = store.add_claim("doe2026study", {"text": "X."})
        seen.add(claim["id"])
        store.delete_claim("doe2026study", claim["id"])
    assert len(seen) == 5, seen
    assert store.load_paper("doe2026study")["claim_seq"] == 5


def test_the_counter_is_derived_for_a_corpus_written_before_it_existed():
    """An older paper file has claims but no `claim_seq`."""
    paper = store.new_paper("doe2026study")
    paper["claims"] = [
        {"id": "doe2026study-c1", "text": "One.", "tags": [], "ledger_links": []},
        {"id": "doe2026study-c7", "text": "Seven.", "tags": [], "ledger_links": []},
    ]
    del paper["claim_seq"]
    store.save_paper(paper)

    claim = store.add_claim("doe2026study", {"text": "Next."})
    assert claim["id"] == "doe2026study-c8", "the counter did not continue past the highest id"


def test_extraction_does_not_reuse_ids_across_runs():
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": f"Claim {n}.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}
                   for n in range(3)],
    }
    first = extract.merge_extraction("doe2026study", payload)
    first_ids = [c["id"] for c in first["claims"]]

    # Keep one, then re-extract: the fresh claims must not take retired ids.
    store.update_claim("doe2026study", first_ids[0], {"reviewed": True})
    second = extract.merge_extraction("doe2026study", payload)
    second_ids = [c["id"] for c in second["claims"]]

    assert len(set(second_ids)) == len(second_ids)
    reissued = (set(second_ids) - {first_ids[0]}) & set(first_ids)
    assert reissued == set(), f"ids were reissued: {reissued}"


def test_a_hand_written_claim_after_extraction_gets_a_fresh_id():
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "Extracted.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}],
    }
    extracted = extract.merge_extraction("doe2026study", payload)
    used = {c["id"] for c in extracted["claims"]}
    manual = store.add_claim("doe2026study", {"text": "By hand."})
    assert manual["id"] not in used


# --- round 14 findings ----------------------------------------------------

def test_a_deleted_paper_key_is_never_issued_again():
    """A citekey identifies one paper for all time, so nothing has to prove which."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_claim("doe2026study", {"text": "One."})
    store.delete_paper("doe2026study")

    # A different paper that produces the same coarse citekey.
    key = store.reserve_key("doe2026study", title="A Study of Something Else")
    assert key == "doe2026studya", "the retired key was issued again"
    assert store.add_claim(key, {"text": "Unrelated."})["id"] == "doe2026studya-c1"


def test_keys_keep_retiring_across_incarnations():
    issued = []
    for _ in range(3):
        key = store.reserve_key("doe2026study")
        issued.append(key)
        store.add_claim(key, {"text": "X."})
        store.delete_paper(key)
    assert issued == ["doe2026study", "doe2026studya", "doe2026studyb"]
    assert store.retired_keys() == set(issued)


def test_a_key_with_no_history_is_issued_as_is():
    assert store.reserve_key("doe2026study") == "doe2026study"


def test_retiring_uses_a_lock_that_no_other_lock_nests_inside():
    """`delete_paper` holds the paper lock while retiring.

    Sharing the vocabulary lock here would invert the documented order — a tag
    rename holds vocabulary and takes paper locks — and deadlock. This asserts
    the two do not contend.
    """
    import threading

    store.add_tag("alpha")
    for n in range(3):
        store.save_paper(store.new_paper(f"doe2026s{n}"))
        store.add_claim(f"doe2026s{n}", {"text": "X.", "tags": ["alpha"]})

    errors = []
    start = threading.Barrier(2)

    def rename():
        try:
            start.wait(timeout=5)
            store.rename_tag("alpha", "renamed")
        except Exception as exc:
            errors.append(exc)

    def remove():
        try:
            start.wait(timeout=5)
            for n in range(3):
                store.delete_paper(f"doe2026s{n}")
        except Exception as exc:
            errors.append(exc)

    # Daemons: if the lock order inverts these never finish, and a non-daemon
    # thread would hang the interpreter at exit instead of failing the test.
    threads = [threading.Thread(target=rename, daemon=True),
               threading.Thread(target=remove, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "deleting a paper deadlocked against a tag rename"
    assert not errors, errors


def test_deleting_a_paper_records_its_key_even_with_no_claims():
    store.save_paper(store.new_paper("doe2026study"))
    store.delete_paper("doe2026study")
    assert store.retired_keys() == {"doe2026study"}


# --- round 16 findings ----------------------------------------------------

def test_reserving_a_key_holds_the_retirement_lock(monkeypatch):
    """Check and create must be one transaction.

    Reading the retired set and creating separately leaves a gap in which a
    concurrent `delete_paper` retires and unlinks a key; the caller then still
    believes it is free and recreates it. Asserted as the invariant, because the
    gap is microseconds and not reachable from outside.
    """
    held = []
    real = store.retired_keys

    def watched():
        held.append(bool(getattr(store._depth, "held", {}).get("retired")))
        return real()

    monkeypatch.setattr(store, "retired_keys", watched)
    store.reserve_key("doe2026study")

    assert held, "the retired set was never consulted"
    assert all(held), "the retired set was read without holding the retirement lock"


def test_key_allocation_continues_past_z():
    """The a-z ceiling became permanent once keys were retired."""
    for candidate in store.key_candidates("doe2026study"):
        store.save_paper(store.new_paper(candidate))
        if candidate.endswith("z") and len(candidate) == len("doe2026study") + 1:
            break

    assert store.reserve_key("doe2026study") == "doe2026studyaa"


def test_repeated_delete_and_readd_never_runs_out():
    """Thirty cycles on one coarse key used to fail permanently at the 27th."""
    issued = []
    for _ in range(30):
        key = store.reserve_key("doe2026study")
        issued.append(key)
        store.delete_paper(key)
    assert len(set(issued)) == 30
    assert issued[26] == "doe2026studyz"           # the old ceiling
    assert issued[27] == "doe2026studyaa"          # continued past it
    assert store.reserve_key("doe2026study") not in set(issued)


def test_many_metadata_free_uploads_of_the_same_filename(monkeypatch):
    """The reported path: several files all called paper.pdf."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    keys = []
    for n in range(30):
        key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody" + bytes([n]), "paper.pdf")
        assert created, f"upload {n} did not create a paper"
        keys.append(key)
    assert len(set(keys)) == 30


def test_key_exhaustion_still_reports_clearly(monkeypatch):
    monkeypatch.setattr(store, "MAX_KEY_CANDIDATES", 3)
    monkeypatch.setattr(store, "key_candidates", lambda base: iter([base, base + "a", base + "b"]))
    for suffix in ("", "a", "b"):
        store.save_paper(store.new_paper("doe2026study" + suffix))
    with pytest.raises(RuntimeError, match="cannot find an unused key"):
        store.reserve_key("doe2026study")


# --- round 17 findings ----------------------------------------------------

def test_a_download_for_a_removed_paper_fails_the_ingest(monkeypatch):
    """`download_pdf` returning False must not be reported as success."""
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)
    monkeypatch.setattr(ingest, "download_pdf", lambda url, key, client: False)

    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_add_reports_a_paper_removed_mid_ingest_without_crashing(monkeypatch, capsys):
    """`_report_missing_pdf` used to raise KeyError on the deleted JSON."""
    def land_then_vanish(ref, client=None):
        return "doe2026study", True      # nothing was ever written

    monkeypatch.setattr(ingest, "ingest_ref", land_then_vanish)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    assert "removed while it was being added" in capsys.readouterr().err


def recovery_corpus(monkeypatch):
    """An existing paper with no PDF and a recorded download failure."""
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", notes="PDF download failed: 503",
        source={"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    ))
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)


def test_recovery_keeps_the_note_when_the_retry_fails(monkeypatch):
    """A failed download raises; the note must survive so the paper explains itself."""
    recovery_corpus(monkeypatch)

    def still_down(url, key, client):
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(ingest, "download_pdf", still_down)
    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (key, created) == ("doe2026study", False)
    assert store.load_paper("doe2026study")["notes"] == "PDF download failed: 503"


def test_recovery_reports_a_paper_removed_mid_retry(monkeypatch):
    """False means the paper went away, which is different from a failed download."""
    recovery_corpus(monkeypatch)
    monkeypatch.setattr(ingest, "download_pdf", lambda url, key, client: False)
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_recovery_clears_the_note_when_the_retry_lands(monkeypatch):
    recovery_corpus(monkeypatch)

    def lands(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands)
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert store.load_paper("doe2026study")["notes"] == ""


# --- round 18: the legacy claim sequence -----------------------------------

def legacy_paper(reviewed_ids=(), unreviewed_ids=()):
    """A paper written before `claim_seq` existed."""
    paper = store.new_paper("doe2026study", title="A Study")
    del paper["claim_seq"]
    paper["claims"] = [
        {"id": cid, "text": f"Claim {cid}.", "kind": "finding", "strength": "aside",
         "tags": [], "evidence": "", "quote": "", "locator": "", "ledger_links": [],
         "reviewed": cid in reviewed_ids}
        for cid in list(reviewed_ids) + list(unreviewed_ids)
    ]
    store.save_paper(paper)
    return paper


def test_ensure_claim_seq_reads_every_claim():
    paper = legacy_paper(reviewed_ids=("doe2026study-c2",),
                         unreviewed_ids=("doe2026study-c9",))
    assert store.ensure_claim_seq(paper) == 9
    assert paper["claim_seq"] == 9


def test_re_extraction_does_not_reissue_an_unreviewed_legacy_id():
    """The highest id belonged to a claim the reviewed-only filter discards."""
    legacy_paper(reviewed_ids=("doe2026study-c2",),
                 unreviewed_ids=("doe2026study-c9",))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "Fresh.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    fresh = [c for c in paper["claims"] if c["text"] == "Fresh."]
    assert len(fresh) == 1
    assert fresh[0]["id"] == "doe2026study-c10", (
        f"reissued a discarded claim's id: {fresh[0]['id']}")


def test_re_extraction_on_a_wholly_unreviewed_legacy_paper():
    """The real corpus shape: no claim_seq and nothing reviewed yet."""
    legacy_paper(unreviewed_ids=tuple(f"doe2026study-c{n}" for n in range(1, 17)))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": f"Fresh {n}.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}
                   for n in range(3)],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    ids = [c["id"] for c in paper["claims"]]
    assert ids == ["doe2026study-c17", "doe2026study-c18", "doe2026study-c19"], ids


# --- round 19: removal detected after the PDF landed -----------------------

def test_removal_after_a_successful_recovery_is_reported(monkeypatch):
    """A KeyError on the post-publication reload means the paper went away."""
    recovery_corpus(monkeypatch)

    def lands_then_vanishes(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        store.paper_path(key).unlink()      # removed between publish and reload
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands_then_vanishes)
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_a_failed_retry_is_still_tolerated(monkeypatch):
    """Only removal is fatal; a download that simply fails leaves the paper."""
    recovery_corpus(monkeypatch)

    def fails(url, key, client):
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(ingest, "download_pdf", fails)
    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (key, created) == ("doe2026study", False)
    assert store.load_paper("doe2026study")["notes"] == "PDF download failed: 503"


# --- round 20: the web job must say a paper cannot be read -----------------

def run_ingest_job(ref, monkeypatch, do_extract=False):
    """Drive the server's ingest worker synchronously and return its job."""
    job = {"id": 1, "label": "t", "state": "queued", "detail": "", "key": None}
    monkeypatch.setattr(server, "_prune_jobs", lambda: None)
    server._run_ingest(job, ref, do_extract)
    return job


def test_a_web_ingest_with_no_pdf_is_reported_as_a_failure(monkeypatch):
    """`done` with an empty detail drops the job from the strip entirely."""
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "Journal", "doi": "10.1145/3442188.3445922",
        "source": {"kind": "doi", "id": "10.1145/3442188.3445922", "url": "", "pdf_url": ""},
    }
    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi, client: dict(meta))
    job = run_ingest_job(ingest.Ref("doi", "10.1145/3442188.3445922", ""), monkeypatch)

    assert job["state"] == "error", job
    assert "no PDF stored" in job["detail"]
    assert "Add it again to retry" in job["detail"]


def test_a_web_ingest_that_lands_its_pdf_is_done(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: dict(meta))

    def lands(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands)
    job = run_ingest_job(ingest.Ref("arxiv", "2602.06941", ""), monkeypatch)
    assert job["state"] == "done", job
    assert job["detail"] == ""


def test_a_failed_download_is_reported_with_its_note(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: dict(meta))

    def fails(url, client):
        raise httpx.ConnectError("503 from the host")

    monkeypatch.setattr(ingest, "fetch_pdf", fails)
    job = run_ingest_job(ingest.Ref("arxiv", "2602.06941", ""), monkeypatch)
    assert job["state"] == "error", job
    assert "503 from the host" in job["detail"]


# --- round 21: a tag deleted during the call must not come back ------------

def extraction_payload(tags, proposed=()):
    return {
        "summary": "", "relevance": "",
        "proposed_tags": [{"name": n, "description": ""} for n in proposed],
        "claims": [{"text": "A finding.", "kind": "finding", "strength": "aside",
                    "tags": list(tags), "evidence": "", "quote": "", "locator": "",
                    "ledger_links": []}],
    }


def test_a_tag_deleted_during_the_call_is_not_written_back():
    store.add_tag("alpha")
    store.add_tag("doomed")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())          # what the model was shown

    store.delete_tag("doomed")                    # the user deletes it mid-call

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "doomed"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha"], "a deleted tag came back"
    assert [t["name"] for t in paper["proposed_tags"]] == []


def test_a_tag_renamed_during_the_call_does_not_reappear():
    store.add_tag("old")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())

    store.rename_tag("old", "new")

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["old"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == []
    assert "old" not in store.tag_names()


def test_a_genuinely_new_tag_is_still_proposed():
    """Only names the vocabulary just lost are dropped, not invented ones."""
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "invented"], proposed=["invented"]),
        prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha", "invented"]
    assert [t["name"] for t in paper["proposed_tags"]] == ["invented"]


def test_a_deleted_tag_re_proposed_by_the_model_is_allowed():
    """If the model puts the name forward as new, that is a proposal, not an echo."""
    store.add_tag("doomed")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())
    store.delete_tag("doomed")

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["doomed"], proposed=["doomed"]),
        prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["doomed"]
    assert [t["name"] for t in paper["proposed_tags"]] == ["doomed"]


def test_without_a_snapshot_nothing_is_dropped():
    """Callers that do not pass `prompt_tags` keep the previous behavior."""
    store.save_paper(store.new_paper("doe2026study"))
    paper = extract.merge_extraction("doe2026study", extraction_payload(["whatever"]))
    assert paper["claims"][0]["tags"] == ["whatever"]


# --- round 22: the prompt and its snapshot must come from one read ---------

def test_the_prompt_and_the_snapshot_come_from_one_vocabulary_read(monkeypatch):
    """Two reads let a tag added between them reach the prompt but not the snapshot.

    Asserted by counting: the vocabulary is read exactly once before the prompt
    is handed to the model, so the two cannot disagree.
    """
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    reads = []
    real_load_tags = store.load_tags
    monkeypatch.setattr(store, "load_tags",
                        lambda: (reads.append(1), real_load_tags())[1])

    seen = {}

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            seen["prompt"] = kwargs["messages"][0]["content"][1]["text"]
            seen["reads_before_prompt"] = len(reads)
            payload = {"summary": "", "relevance": "", "proposed_tags": [], "claims": []}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    extract.extract_paper("doe2026study")

    assert "- alpha:" in seen["prompt"], "the vocabulary did not reach the prompt"
    assert seen["reads_before_prompt"] == 1, (
        f"the vocabulary was read {seen['reads_before_prompt']} times before the prompt; "
        "a tag arriving between reads would be in the prompt but not the snapshot")


def test_a_tag_added_just_before_the_call_is_still_recognised():
    """The regression the single read prevents, asserted at the merge."""
    store.add_tag("alpha")
    store.add_tag("late-arrival")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())      # both names were shown

    store.delete_tag("late-arrival")          # deleted while the call ran

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "late-arrival"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha"]
    assert [t["name"] for t in paper["proposed_tags"]] == []


# --- round 24 findings ----------------------------------------------------

JOURNAL_TITLE = "Cortical dynamics under distribution shift"

JOURNAL_PAGE_ONE = f"""
{JOURNAL_TITLE}
Journal of Made-Up Results 12(3), 2026.  https://doi.org/10.1234/journal.2026.99

Abstract. We revisit the setting of Smith et al. (arXiv:2301.09876) and show
that their result does not hold under shift. See also doi:10.5555/cited.2019.7.
"""

ARXIV_PAGE_ONE = """
arXiv:2504.01234v2 [cs.CL] 3 Apr 2026

Endogenous steering resistance
We build on Smith et al. (arXiv:2301.09876).
"""

CROSSREF_TITLES = {
    "10.1234/journal.2026.99": JOURNAL_TITLE,
    "10.5555/cited.2019.7": "Something else entirely, cited in passing",
    "10.9999/cited.2020.1": "A paper in the bibliography",
}


def identity_probe(monkeypatch, page_one: str, page_two: str = "", embedded: str = ""):
    """Record which lookups `guess_from_pdf` tries, and with what identifier."""
    calls = {"doi": [], "arxiv": []}

    def pages(path, pages=2):
        return page_one if pages == 1 else page_one + page_two

    def arxiv(ident, client):
        calls["arxiv"].append(ident)
        return {"title": "arxiv paper", "authors": [], "year": None, "abstract": "", "venue": "",
                "doi": "", "source": {"kind": "arxiv", "id": ident, "url": "", "pdf_url": ""}}

    def crossref(ident, client):
        calls["doi"].append(ident)
        return {"title": CROSSREF_TITLES.get(ident, "unknown work"), "authors": [], "year": None,
                "abstract": "", "venue": "", "doi": ident,
                "source": {"kind": "doi", "id": ident, "url": "", "pdf_url": ""}}

    monkeypatch.setattr(ingest, "pdf_first_page_text", pages)
    monkeypatch.setattr(ingest, "pdf_metadata_doi", lambda path: embedded)
    monkeypatch.setattr(ingest, "fetch_arxiv", arxiv)
    monkeypatch.setattr(ingest, "fetch_crossref", crossref)
    return calls


def test_a_cited_arxiv_id_does_not_become_the_uploads_identity(monkeypatch, tmp_path):
    """A journal PDF citing a preprint must be filed under its own DOI."""
    calls = identity_probe(monkeypatch, JOURNAL_PAGE_ONE)
    meta = ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")

    assert calls["arxiv"] == [], f"filed under the preprint it cites: {calls}"
    assert meta["doi"] == "10.1234/journal.2026.99"
    assert meta["title"] == JOURNAL_TITLE


def test_the_arxiv_stamp_is_still_read_as_identity(monkeypatch, tmp_path):
    """The margin stamp is the file's own id, even alongside a cited one."""
    calls = identity_probe(monkeypatch, ARXIV_PAGE_ONE)
    ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")
    assert calls["arxiv"] == ["2504.01234"]


def test_identity_does_not_come_from_the_reference_list(monkeypatch, tmp_path):
    """Page two is where other papers' DOIs live; only page one is identity."""
    references = "\n[1] Smith. https://doi.org/10.9999/cited.2020.1\n"
    calls = identity_probe(monkeypatch, "Untitled draft\n", references)
    meta = ingest.guess_from_pdf(tmp_path / "my-draft.pdf", None, "my-draft.pdf")

    assert calls == {"doi": [], "arxiv": []}, f"identified from the bibliography: {calls}"
    assert meta["title"] == "my draft"


def test_a_paper_deleted_mid_listing_does_not_fail_the_listing(monkeypatch):
    """`paper_keys` is a snapshot; a key can be gone by the time it is read."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    real_keys = store.paper_keys

    monkeypatch.setattr(store, "paper_keys", lambda: real_keys() + ["gone2026missing"])
    assert [p["key"] for p in store.all_papers()] == ["doe2026study"]

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/state").status_code == 200


# --- round 25 findings ----------------------------------------------------

CITED_ONLY_PAGE = """
A working paper with no DOI of its own

We extend the analysis of Jones (2019), doi:10.5555/cited.2019.7, to the
multilingual case.
"""


def test_a_cited_doi_does_not_become_the_uploads_identity(monkeypatch, tmp_path):
    """The only DOI on the page belongs to a paper this one cites."""
    calls = identity_probe(monkeypatch, CITED_ONLY_PAGE)
    meta = ingest.guess_from_pdf(tmp_path / "working-paper.pdf", None, "working-paper.pdf")

    assert calls["doi"] == ["10.5555/cited.2019.7"], "the DOI was never checked"
    assert meta["doi"] == "", f"filed under the paper it cites: {meta['title']}"
    assert meta["title"] == "working paper"


CITED_FIRST_PAGE = f"""
{JOURNAL_TITLE}

Abstract. Following Jones (2019), doi:10.5555/cited.2019.7, we revisit the
setting and show that the result does not hold under shift.

Published in the Journal of Made-Up Results.  https://doi.org/10.1234/journal.2026.99
"""


def test_the_papers_own_doi_wins_over_one_it_cites_first(monkeypatch, tmp_path):
    """A page can cite a DOI above the one in its own imprint line."""
    calls = identity_probe(monkeypatch, CITED_FIRST_PAGE)
    meta = ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")

    assert calls["doi"] == ["10.5555/cited.2019.7", "10.1234/journal.2026.99"]
    assert meta["doi"] == "10.1234/journal.2026.99"


def test_a_doi_in_the_files_metadata_is_taken_as_identity(monkeypatch, tmp_path):
    """XMP is the publisher's own statement, so no title check is needed."""
    calls = identity_probe(monkeypatch, "scanned page with no readable text\n",
                           embedded="10.1234/journal.2026.99")
    meta = ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")

    assert calls["doi"] == ["10.1234/journal.2026.99"]
    assert meta["title"] == JOURNAL_TITLE


def test_a_download_is_not_read_back_into_memory(monkeypatch):
    """The signature check must not undo the streaming it follows."""
    read_whole = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (read_whole.append(self.name), real_read_bytes(self))[1])

    class Streamed:
        def stream(self, *args, **kwargs):
            class R:
                headers = {"content-type": "application/pdf"}

                def raise_for_status(self):
                    return None

                def iter_bytes(self, n):
                    yield b"%PDF-1.4\n"
                    for _ in range(4):
                        yield b"x" * n

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    staged = ingest.fetch_pdf("https://example.org/big.pdf", Streamed())
    try:
        assert staged.stat().st_size > 65536
        assert read_whole == [], "the staged download was read back in full"
    finally:
        staged.unlink(missing_ok=True)


# --- round 26 findings ----------------------------------------------------

def test_a_rename_during_a_retag_is_not_undone(monkeypatch):
    """The rename rewrites the claim; the stale assignment must not revert it."""
    store.add_tag("alpha")
    store.add_tag("old-name")
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claim_seq"] = 1
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": ["alpha", "old-name"], "evidence": "",
                        "quote": "", "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            # The rename lands while the model is thinking.
            store.rename_tag("old-name", "new-name")
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha", "old-name"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha", "new-name"], "the rename was undone"


def test_a_tag_deleted_during_a_retag_stays_off(monkeypatch):
    """The other half of the same rule: a vanished tag is not written back."""
    store.add_tag("alpha")
    store.add_tag("doomed")
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": ["alpha", "doomed"], "evidence": "",
                        "quote": "", "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.delete_tag("doomed")
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha", "doomed"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    result = extract.retag_paper("doe2026study")
    assert result["claims"][0]["tags"] == ["alpha"]


def test_an_upload_is_staged_on_disk_not_held_in_memory(monkeypatch):
    """The worker gets a path; the batch no longer costs one buffer per file."""
    handed = []
    monkeypatch.setattr(server._pool, "submit",
                        lambda fn, job, staged, name, extract_now: handed.append((staged, name)))

    body = b"%PDF-1.4\n" + b"x" * 200_000
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/upload?extract_now=false",
                               files={"files": ("big.pdf", body, "application/pdf")})

    assert response.json() == {"queued": 1}
    staged, name = handed[0]
    assert isinstance(staged, Path), f"the worker was handed {type(staged).__name__}"
    try:
        assert staged.read_bytes() == body
        assert name == "big.pdf"
    finally:
        staged.unlink(missing_ok=True)


def test_a_rejected_upload_leaves_no_staging_file(monkeypatch):
    """`ingest_staged_pdf` owns the file it is given, on every path out."""
    staged = ingest.stage_upload(io.BytesIO(b"<html>not a pdf</html>"), "fake.pdf")
    assert staged.exists()

    with pytest.raises(ValueError, match="is not a PDF"):
        ingest.ingest_staged_pdf(staged, "fake.pdf")

    assert not staged.exists()
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


# --- round 27 findings ----------------------------------------------------

def retag_client(monkeypatch, during_the_call):
    """A retag call that runs `during_the_call` before answering."""
    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            during_the_call()
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())


def study_with_tags(tags):
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": list(tags), "evidence": "", "quote": "",
                        "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)


def set_claim_tags(tags):
    paper = store.load_paper("doe2026study")
    paper["claims"][0]["tags"] = sorted(tags)
    store.save_paper(paper)


def test_a_tag_a_person_adds_during_a_retag_is_kept(monkeypatch):
    """The added tag is in the vocabulary the model saw, and still survives."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["alpha"])

    retag_client(monkeypatch, lambda: set_claim_tags(["alpha", "beta"]))
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha", "beta"], "a person's edit was reversed"


def test_a_tag_a_person_removes_during_a_retag_stays_off(monkeypatch):
    """The other direction: the model's answer must not put it back."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["alpha", "beta"])

    retag_client(monkeypatch, lambda: set_claim_tags(["beta"]))
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["beta"], "a removed tag was restored"


def test_an_untouched_claim_still_takes_the_models_answer(monkeypatch):
    """The rule only stands down for claims somebody else changed."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["beta"])

    retag_client(monkeypatch, lambda: None)
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha"]


def test_upload_staging_does_not_run_on_the_event_loop(monkeypatch):
    """A large drop must not stop the page from polling or saving."""
    import asyncio

    where = {}
    real_stage = ingest.stage_upload

    def watched(source, filename):
        try:
            asyncio.get_running_loop()
            where["on_the_loop"] = True
        except RuntimeError:
            where["on_the_loop"] = False
        return real_stage(source, filename)

    monkeypatch.setattr(ingest, "stage_upload", watched)
    monkeypatch.setattr(server._pool, "submit",
                        lambda fn, job, staged, name, extract_now: staged.unlink(missing_ok=True))

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/upload?extract_now=false",
                               files={"files": ("big.pdf", b"%PDF-1.4\n" + b"x" * 100_000)})

    assert response.json() == {"queued": 1}
    assert where["on_the_loop"] is False, "the copy blocked the event loop"


# --- round 29 findings ----------------------------------------------------

BIBLIOGRAPHY_ON_PAGE_ONE = """
A two-page note with no DOI of its own

Abstract. A short remark on distribution shift.

References
[1] Cortical dynamics under distribution shift. Journal of Made-Up Results,
    2026. https://doi.org/10.1234/journal.2026.99
"""


def test_a_title_in_a_first_page_bibliography_is_not_identity(monkeypatch, tmp_path):
    """The cited paper is named next to its DOI; that pair is not this paper."""
    calls = identity_probe(monkeypatch, BIBLIOGRAPHY_ON_PAGE_ONE)
    meta = ingest.guess_from_pdf(tmp_path / "note.pdf", None, "note.pdf")

    assert calls["doi"] == ["10.1234/journal.2026.99"], "the DOI was never checked"
    assert meta["doi"] == "", f"filed under the work it cites: {meta['title']}"
    assert meta["title"] == "note"


def test_a_title_above_the_abstract_is_identity(monkeypatch, tmp_path):
    """The same check still accepts a paper's own front matter."""
    identity_probe(monkeypatch, JOURNAL_PAGE_ONE)
    meta = ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")
    assert meta["doi"] == "10.1234/journal.2026.99"


def test_a_claim_rewritten_during_a_retag_keeps_its_tags(monkeypatch):
    """The answer describes wording the claim no longer has."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["beta"])

    def rewrite():
        paper = store.load_paper("doe2026study")
        paper["claims"][0]["text"] = "Rewritten by hand."
        store.save_paper(paper)

    retag_client(monkeypatch, rewrite)
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["text"] == "Rewritten by hand."
    assert result["claims"][0]["tags"] == ["beta"], "tags for the old wording were applied"


# --- round 30 findings ----------------------------------------------------

def test_a_correction_made_during_a_re_read_survives(monkeypatch):
    """A claim edited while the model was reading is newer than its answer."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study",
                            {"text": "Original wording.", "tags": [], "reviewed": False})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.update_claim("doe2026study", claim["id"], {"text": "Corrected by hand."})
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    texts = [c["text"] for c in paper["claims"]]
    assert "Corrected by hand." in texts, "the correction was overwritten by the re-read"
    assert "A fresh claim." in texts


def test_a_claim_written_during_a_re_read_survives(monkeypatch):
    """A claim added by hand while the model read is not in the snapshot."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.add_claim("doe2026study",
                            {"text": "Written by hand.", "tags": [], "reviewed": False})
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    texts = [c["text"] for c in paper["claims"]]
    assert "Written by hand." in texts, "a hand-written claim was discarded"
    assert "A fresh claim." in texts


def test_an_untouched_unreviewed_claim_is_still_replaced(monkeypatch):
    """The rule only spares claims that changed; a re-read still replaces."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_claim("doe2026study",
                    {"text": "Original wording.", "tags": [], "reviewed": False})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    assert [c["text"] for c in paper["claims"]] == ["A fresh claim."]


def test_the_cli_does_not_read_a_local_pdf_whole(monkeypatch, tmp_path, capsys):
    """`doxograph add file.pdf` streams it into staging like an upload does."""
    read_whole = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (read_whole.append(self.name), real_read_bytes(self))[1])
    monkeypatch.setattr(ingest, "guess_from_pdf", lambda path, client, display_name=None: {
        "title": "A Local Paper", "authors": [], "year": None, "abstract": "", "venue": "",
        "doi": "", "source": {"kind": "file", "id": display_name, "url": "", "pdf_url": ""}})

    pdf = tmp_path / "local.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)

    code = __main__.main(["add", str(pdf), "--no-extract"])

    assert code == 0, capsys.readouterr()
    assert "local.pdf" not in read_whole, "the CLI read the whole PDF into memory"


# --- post-merge stabilization --------------------------------------------

def test_overlapping_extractions_are_serialized_without_blocking_edits(monkeypatch):
    """A second re-read starts only after the first merge has completed."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_started = threading.Event()
    call_guard = threading.Lock()
    call_count = 0

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            nonlocal call_count
            with call_guard:
                call_count += 1
                call_number = call_count
            if call_number == 1:
                first_entered.set()
                assert release_first.wait(5), "test did not release the first extraction"
            else:
                second_entered.set()
            payload = {
                "summary": "",
                "relevance": "",
                "proposed_tags": [],
                "claims": [{
                    "text": f"Extraction {call_number}.",
                    "kind": "finding",
                    "strength": "aside",
                    "tags": [],
                    "evidence": "",
                    "quote": "",
                    "locator": "",
                    "ledger_links": [],
                }],
            }
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})

    errors = []

    def run(second=False):
        if second:
            second_started.set()
        try:
            extract.extract_paper("doe2026study")
        except BaseException as exc:  # collect worker failures for the assertion thread
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run, kwargs={"second": True})
    first.start()
    assert first_entered.wait(5), "the first extraction never reached the model"
    second.start()
    assert second_started.wait(5)
    time.sleep(0.2)
    assert not second_entered.is_set(), "the second model call overlapped the first"

    # The extraction lock must not be the paper lock: an ordinary edit can land
    # while the model call is parked and is reconciled by the merge.
    manual = store.add_claim("doe2026study", {"text": "Written while reading."})
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert second_entered.is_set()
    claims = store.load_paper("doe2026study")["claims"]
    assert [claim["text"] for claim in claims] == ["Written while reading.", "Extraction 2."]
    assert manual["id"] == claims[0]["id"]


def test_extraction_lock_is_held_across_processes(tmp_path):
    """The server and CLI cannot re-read one paper at the same time."""
    import subprocess
    import sys
    import textwrap

    log = tmp_path / "extraction-order.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.extraction_lock("doe2026study"):
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.extraction_lock("doe2026study"):
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(0.5)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the extraction lock"
    assert child.returncode == 0
    assert log.read_text().split() == ["parent", "child"]


# --- The PDF reaches the model through the Files API, not inline -------------


class FakeFiles:
    def __init__(self, fail_delete=()):
        self.uploads = []
        self.deletions = []
        self.fail_delete = set(fail_delete)

    def upload(self, file):
        self.uploads.append(file)
        return type("F", (), {"id": f"file_{len(self.uploads)}"})()

    def delete(self, file_id):
        self.deletions.append(file_id)
        if file_id in self.fail_delete:
            self.fail_delete.discard(file_id)
            import httpx2
            raise anthropic.APIConnectionError(
                request=httpx2.Request("DELETE", f"https://api.anthropic.com/v1/files/{file_id}"))


class FilesClient:
    """A client whose messages.create records the PDF block it was sent."""

    def __init__(self, fail_on=(), fail_delete=()):
        self.files = FakeFiles(fail_delete)
        self.messages = self
        self.blocks = []
        self.fail_on = set(fail_on)

    def create(self, **kwargs):
        block = kwargs["messages"][0]["content"][0]
        self.blocks.append(block)
        file_id = block["source"]["file_id"]
        if file_id in self.fail_on:
            self.fail_on.discard(file_id)
            raise anthropic.BadRequestError(
                f"file {file_id} not found", response=_response(400), body=None)
        payload = {"summary": "", "relevance": "", "proposed_tags": [], "claims": []}
        text = type("B", (), {"type": "text", "text": json.dumps(payload)})()
        return type("R", (), {"content": [text], "stop_reason": "end_turn", "usage": None})()


def _response(status: int):
    import httpx2
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.fixture
def paper_with_pdf():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4 fake")
    return "doe2026study"


def test_the_pdf_is_sent_by_file_id_not_inlined(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    extract.extract_paper(paper_with_pdf)

    assert api.files.uploads == [store.pdf_path(paper_with_pdf)]
    assert api.blocks[0]["source"] == {"type": "file", "file_id": "file_1"}
    assert api.blocks[0]["cache_control"]["type"] == "ephemeral"
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_1"


def test_a_second_read_reuses_the_upload(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    extract.extract_paper(paper_with_pdf)
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 1
    assert [b["source"]["file_id"] for b in api.blocks] == ["file_1", "file_1"]


def test_a_replaced_pdf_is_uploaded_again(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    store.pdf_path(paper_with_pdf).write_bytes(b"%PDF-1.4 a different, longer file")
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1"]
    assert api.blocks[-1]["source"]["file_id"] == "file_2"
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_2"


def test_same_size_and_mtime_with_different_bytes_is_uploaded_again(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    pdf = store.pdf_path(paper_with_pdf)
    original = pdf.stat()
    pdf.write_bytes(b"%PDF-1.4 new!")
    os.utime(pdf, ns=(original.st_atime_ns, original.st_mtime_ns))
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1"]
    assert api.blocks[-1]["source"]["file_id"] == "file_2"


def test_a_failed_remote_delete_is_retried_on_the_next_read(monkeypatch, paper_with_pdf):
    api = FilesClient(fail_delete={"file_1"})
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    store.pdf_path(paper_with_pdf).write_bytes(b"%PDF-1.4 replacement")
    extract.extract_paper(paper_with_pdf)
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["superseded_file_ids"] == ["file_1"]

    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1", "file_1"]
    assert "superseded_file_ids" not in store.load_paper(paper_with_pdf)["pdf_upload"]


def test_removing_a_paper_deletes_all_of_its_remote_uploads(monkeypatch, paper_with_pdf):
    paper = store.load_paper(paper_with_pdf)
    paper["pdf_upload"] = {
        "file_id": "file_current",
        "superseded_file_ids": ["file_old_1", "file_old_2"],
    }
    store.save_paper(paper)
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.delete(f"/api/papers/{paper_with_pdf}")

    assert response.status_code == 200
    assert api.files.deletions == ["file_old_1", "file_old_2", "file_current"]
    assert not store.paper_path(paper_with_pdf).exists()
    assert not store.pdf_path(paper_with_pdf).exists()


def test_a_remote_failure_keeps_the_paper_metadata_for_delete_retry(monkeypatch, paper_with_pdf):
    paper = store.load_paper(paper_with_pdf)
    paper["pdf_upload"] = {"file_id": "file_current"}
    store.save_paper(paper)
    api = FilesClient(fail_delete={"file_current"})

    with pytest.raises(anthropic.APIConnectionError):
        extract.delete_paper(paper_with_pdf, api=api)

    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_current"
    assert store.pdf_path(paper_with_pdf).exists()


def test_an_upload_the_server_forgot_is_redone_once(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    api.fail_on.add("file_1")  # the server no longer has it
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert [b["source"]["file_id"] for b in api.blocks] == ["file_1", "file_1", "file_2"]


def test_an_unrelated_bad_request_is_not_retried(monkeypatch, paper_with_pdf):
    api = FilesClient()

    def create(**kwargs):
        raise anthropic.BadRequestError("something else", response=_response(400), body=None)

    api.create = create
    monkeypatch.setattr(extract, "client", lambda: api)

    with pytest.raises(anthropic.BadRequestError):
        extract.extract_paper(paper_with_pdf)
    assert len(api.files.uploads) == 1


def test_extracting_without_a_pdf_says_so(monkeypatch):
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    monkeypatch.setattr(extract, "client", lambda: FilesClient())
    with pytest.raises(FileNotFoundError):
        extract.extract_paper("doe2026study")


# --- a blank author must not take out the citation markers ----------------
#
# Crossref records an institutional author under `name`, with no given or
# family part, and reading only those two left the paper carrying a blank
# author. Every citation marker then indexed the first author's words, and
# `"".split()` is empty: the tension pass, the synthesis pass, the HTML export
# and `tensions --list` all raised for the whole corpus over one paper.

def blank_author_corpus() -> tuple[str, str]:
    """Two papers on one topic, the first of them with a blank first author."""
    first = store.new_paper("anon2020study", title="A Study", authors=["", "Jane Roe"],
                            year=2020)
    first["claims"] = [{"id": "anon2020study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "headline", "tags": ["topic"], "evidence": "why",
                        "quote": "", "locator": "", "ledger_links": [], "reviewed": True}]
    store.save_paper(first)
    second = store.new_paper("doe2021other", title="Another", authors=["John Doe"], year=2021)
    second["claims"] = [{"id": "doe2021other-c1", "text": "The other finding.",
                         "kind": "finding", "strength": "headline", "tags": ["topic"],
                         "evidence": "", "quote": "", "locator": "", "ledger_links": [],
                         "reviewed": True}]
    store.save_paper(second)
    return "anon2020study-c1", "doe2021other-c1"


def test_a_blank_author_still_names_a_paper_in_the_prompt():
    blank_author_corpus()
    listing = extract._tension_listing(store.claim_rows())
    assert "Roe et al." in listing, "the blank author hid the one that has a name"


def test_a_blank_author_does_not_break_the_export():
    first, _ = blank_author_corpus()
    store.record_synthesis("topic", f"They agree [{first}].",
                           {r["id"]: r for r in store.claim_rows()}, [])
    assert "Roe 2020" in export.render()


def test_a_blank_author_does_not_break_the_tension_listing(capsys):
    first, second = blank_author_corpus()
    rows = store.claim_rows()
    store.record_tensions("topic", [{"claims": [first, second], "kind": "tension", "note": "n"}],
                          {r["id"]: r for r in rows})

    assert __main__.main(["tensions", "--list"]) == 0
    assert "[Roe 2020]" in capsys.readouterr().out


def test_a_paper_with_no_author_at_all_falls_back_to_its_key():
    assert store.cite_surname([], "doe2026study") == "doe2026study"
    assert store.cite_surname(["", ""], "doe2026study") == "doe2026study"


def test_an_institutional_author_keeps_its_name():
    """The root of it: a `name`-only author must not arrive as an empty string."""
    work = {"message": {"title": ["A Study"], "DOI": "10.1/x",
                        "author": [{"name": "The Sudbury Collaboration"},
                                   {"given": "Jane", "family": "Roe"}]}}

    class Client:
        def get(self, url, **kwargs):
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self: work})()

    meta = ingest.fetch_crossref("10.1/x", Client())
    assert meta["authors"] == ["The Sudbury Collaboration", "Jane Roe"]


# --- a paper patch is typed, like every other write ----------------------

def test_a_paper_patch_refuses_a_year_that_is_not_a_number():
    """A string year sorted against every other paper's number and broke export."""
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"year": "not a year"}).status_code == 422
        assert client.patch("/api/papers/doe2026study", json={"authors": "Jane Roe"}).status_code == 422

    assert store.load_paper("doe2026study")["year"] == 2026
    export.render()   # still sortable


def test_a_paper_patch_leaves_the_fields_it_does_not_name_alone():
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026,
                                     venue="A Journal", notes="a note"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"title": "A Better Study"})

    assert response.status_code == 200
    paper = store.load_paper("doe2026study")
    assert paper["title"] == "A Better Study"
    assert (paper["venue"], paper["notes"], paper["year"]) == ("A Journal", "a note", 2026)


def test_a_paper_patch_can_clear_a_year():
    """`None` is a value to write; only an absent field is left alone."""
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"year": None}).status_code == 200
    assert store.load_paper("doe2026study")["year"] is None


def test_a_paper_patch_ignores_a_field_that_is_not_the_users_to_set():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.patch("/api/papers/doe2026study", json={"title": "A Study", "claims": ["nonsense"]})
    assert store.load_paper("doe2026study")["claims"] == []


# --- one paper's retag failure must not cost the papers after it ----------

def test_a_retag_failure_does_not_cost_the_papers_after_it(monkeypatch):
    """Retag all runs the corpus in a fixed order; an early failure used to
    leave every later paper unretagged, unlike the tension and synthesis
    passes beside it and unlike the command line."""
    retagged = []

    def retag(key):
        if key == "a2020first":
            raise RuntimeError("the model refused")
        retagged.append(key)

    monkeypatch.setattr(extract, "retag_paper", retag)
    job = server._new_job("retag 3 papers")
    try:
        server._run_retag(job, ["a2020first", "b2021second", "c2022third"])
    finally:
        server._jobs.pop(job["id"], None)

    assert retagged == ["b2021second", "c2022third"], "a failure stopped the whole batch"
    assert job["state"] == "error"
    assert "1 of 3 papers failed" in job["detail"]


def test_a_retag_batch_that_all_works_is_done(monkeypatch):
    monkeypatch.setattr(extract, "retag_paper", lambda key: None)
    job = server._new_job("retag 2 papers")
    try:
        server._run_retag(job, ["a2020first", "b2021second"])
    finally:
        server._jobs.pop(job["id"], None)

    assert (job["state"], job["detail"]) == ("done", "2 papers")


# --- a page on another site cannot post into the corpus -------------------
#
# A multipart POST is a "simple" request, so a browser sends it across origins
# with no preflight to stop it: any page in any tab could drop a PDF into the
# corpus of a running server. Everything else here takes a JSON body and is
# held back by a preflight it cannot answer.

@pytest.fixture
def queued_jobs(monkeypatch):
    """The work a request queued, without letting the pool run any of it.

    These two routes reach the network — arXiv, then the model — as soon as
    their job starts, so a check that they are refused must not depend on being
    refused to stay offline.
    """
    submitted: list[tuple] = []
    monkeypatch.setattr(server._pool, "submit", lambda *args, **kwargs: submitted.append(args))
    return submitted


def test_a_cross_site_upload_is_refused(queued_jobs):
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "https://evil.example.com"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a page on another site queued an upload"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_a_cross_site_json_post_is_refused(queued_jobs):
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/ingest", json={"text": "2501.00001"},
                               headers={"Origin": "https://evil.example.com"})
    assert response.status_code == 403
    assert queued_jobs == []


def test_the_pages_own_requests_are_allowed():
    """The app posts from the page the server served, so its origin is the host."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"title": "A Better Study"},
                                headers={"Origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200


def test_a_request_with_no_origin_is_allowed():
    """curl, the CLI and the macOS app's uploader are not a browser acting for
    somebody else's page, which is the only thing the check is for."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"}).status_code == 200


def test_a_cross_site_read_with_an_honest_host_is_left_alone():
    """A page that asked for this server by its real name cannot read the reply.

    `Origin` says another site sent this, but `Host` says the browser resolved
    `127.0.0.1` itself, which is the same-origin policy's own case: the response
    goes nowhere the sender can see it. Only a rebound `Host` escapes that, and
    the check below is what catches it.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health",
                          headers={"Origin": "https://evil.example.com"}).status_code == 200


# --- the same-origin test cannot be made of the request's own headers -----
#
# Comparing `Origin` against `Host` only asks a caller to agree with itself. A
# page on a hostname whose DNS is rebound to 127.0.0.1 reaches this server with
# both headers set to that hostname, so the comparison passes and the check
# that exists to stop it waves it through. Trust comes from where the server
# listens instead, which the caller cannot influence.
#
# And rebinding is not stopped by the same-origin policy either -- defeating it
# is the whole trick. The browser believes `evil.example.com` and this server
# are one origin, so the page reads every reply it gets. That is why `Host` is
# checked on reads too, and not only on the writes.

@pytest.fixture
def bound_nowhere_in_particular(monkeypatch):
    """The default: nothing published beyond the loopback interface."""
    monkeypatch.setattr(server, "_published_authorities", frozenset())
    monkeypatch.setattr(server, "_bound_to_every_address", False)


def test_a_rebound_hostname_cannot_pose_as_the_page(queued_jobs, bound_nowhere_in_particular):
    """`Host` says what the attacker wants it to say, so it cannot be evidence."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://evil.example.com", "Host": "evil.example.com"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a rebound hostname queued an upload"


def test_a_rebound_hostname_cannot_delete_a_paper(bound_nowhere_in_particular):
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.request(
            "DELETE", "/api/papers/doe2026study",
            headers={"Origin": "http://evil.example.com", "Host": "evil.example.com"},
        )
    assert response.status_code == 403
    assert store.load_paper("doe2026study")["title"] == "A Study"


def test_a_rebound_hostname_cannot_read_the_corpus(bound_nowhere_in_particular):
    """The rebound page is same-origin to the browser, so it reads the answer.

    Which is the whole reason a read has to be checked: refusing only the writes
    leaves every title, tag and note in the corpus readable by any page whose
    DNS points here. The request carries no `Origin` -- a plain `GET` never does
    -- so `Host` is the only thing there is to go on.
    """
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/api/state", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert "A Study" not in response.text


def test_a_rebound_hostname_cannot_read_a_pdf(bound_nowhere_in_particular):
    """The papers themselves, not just what the corpus says about them."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4 the paper itself")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/pdf/doe2026study", headers={"Host": "evil.example.com"})
    assert response.status_code == 403
    assert b"the paper itself" not in response.content


def test_every_spelling_of_the_loopback_page_is_allowed(bound_nowhere_in_particular):
    """The browser may have been pointed at any of these; all are this machine."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    for origin in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"):
        with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
            response = client.patch("/api/papers/doe2026study", json={"notes": origin},
                                    headers={"Origin": origin})
        assert response.status_code == 200, origin


def test_a_hostname_that_merely_contains_localhost_is_refused(bound_nowhere_in_particular):
    """`localhost.evil.example.com` is a name the attacker owns, not this machine."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": "http://localhost.evil.example.com"})
    assert response.status_code == 403


def test_another_page_on_this_machine_is_still_another_site(queued_jobs,
                                                            bound_nowhere_in_particular):
    """An origin is a scheme, a host *and a port*.

    Whatever else is running on this machine -- a dev server on 3000, something
    a package script started -- is not this app, and a page it serves can post a
    multipart form here without a preflight to stop it. It cannot read the
    reply, but enqueueing an upload needs no reply.
    """
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/upload",
            files={"files": ("paper.pdf", b"%PDF-1.4 ...", "application/pdf")},
            headers={"Origin": "http://localhost:3000"},
        )
    assert response.status_code == 403
    assert queued_jobs == [], "a page on another loopback port queued an upload"


def test_a_request_addressed_to_another_port_is_refused(bound_nowhere_in_particular):
    """The port comes off the listening socket, so `Host` cannot talk it round."""
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/health", headers={"Host": "127.0.0.1:9999"}).status_code == 403


def test_the_address_serve_published_is_trusted(bound_nowhere_in_particular):
    """`serve --host` is documented, so the page it serves has to keep working."""
    server.trust_bind("192.168.1.5", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.5:8765",
                                     "Host": "192.168.1.5:8765"}).status_code == 200
        # Neither another address on the same network nor the same address on
        # another port is where this page came from.
        for origin in ("http://192.168.1.6:8765", "http://192.168.1.5:9999"):
            assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": origin}).status_code == 403, origin
        # And a name that is not published is refused before the origin is even
        # looked at, whatever the page claims about itself.
        assert client.get("/api/state", headers={"Host": "192.168.1.6:8765"}).status_code == 403


def test_a_wildcard_bind_trusts_nothing_the_request_says(bound_nowhere_in_particular):
    """`--host 0.0.0.0` answers on every address, and names none of them.

    The address the browser typed cannot be read off the socket, and the one
    place it is written down -- the request -- is the one place a rebound page
    controls. So matching `Origin` against `Host` is not a fallback here; it is
    the very comparison this whole check exists to refuse.
    """
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"notes": "n"},
                                headers={"Origin": "http://evil.example.com",
                                         "Host": "evil.example.com"})
    assert response.status_code == 403
    assert store.load_paper("doe2026study").get("notes") != "n"


def test_a_wildcard_bind_still_serves_the_operators_own_machine(bound_nowhere_in_particular):
    """Loopback keeps working, so the app on the host itself is unaffected."""
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200


def test_a_wildcard_bind_trusts_the_name_the_operator_published(monkeypatch,
                                                                bound_nowhere_in_particular):
    """Someone has to say what the page is served as; only the operator can."""
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "http://192.168.1.5:8765")
    server.trust_bind("0.0.0.0", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.5:8765",
                                     "Host": "192.168.1.5:8765"}).status_code == 200
        # Everything else on that network is still somebody else.
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://192.168.1.6:8765",
                                     "Host": "192.168.1.6:8765"}).status_code == 403


def test_a_published_loopback_name_beats_the_bound_port_rule(monkeypatch,
                                                             bound_nowhere_in_particular):
    """A TLS terminator on `localhost:443` in front of a backend on 8765.

    The loopback rule compares against the port the socket reports, which is
    the backend's, so the published name fails it -- and the operator's own
    `PUBLISHED_ORIGINS_ENV` has to be consulted first, or it is accepted at
    startup and then quietly ignored on every request.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "https://localhost, http://localhost")
    server.trust_bind("127.0.0.1", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "https://localhost"}).status_code == 200
        # And the same name as the `Host` the proxy passed along, which carries
        # no scheme and so is read against the request's own.
        assert client.get("/api/state", headers={"Host": "localhost"}).status_code == 200


def test_an_unpublished_loopback_port_is_still_refused(monkeypatch,
                                                       bound_nowhere_in_particular):
    """Consulting the published names first must not widen loopback generally.

    `localhost:3000` is another program on this machine and another origin; it
    is trusted only if the operator named it, which is what pins the published
    check to going *before* the bound-port rule rather than instead of it.
    """
    monkeypatch.setenv(server.PUBLISHED_ORIGINS_ENV, "https://localhost")
    server.trust_bind("127.0.0.1", 8765)
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"notes": "n"},
                            headers={"Origin": "http://localhost:3000"}).status_code == 403
        assert client.get("/api/state", headers={"Host": "localhost:3000"}).status_code == 403
    assert store.load_paper("doe2026study").get("notes") != "n"
