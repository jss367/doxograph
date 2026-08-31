"""Regression tests for the review findings on the initial implementation."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from pathlib import Path

import httpx

from doxograph import __main__, bib, config, extract, ingest, server, store


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
    with TestClient(server.app) as client:
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

    client = TestClient(server.app)

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
            with TestClient(server.app) as client:
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

    def publish_after_removal(key, staging):
        # Stand in for Remove landing between the reservation and the copy.
        store.delete_paper(key)
        return real_publish(key, staging)

    monkeypatch.setattr(ingest, "publish_pdf", publish_after_removal)
    key, _ = ingest.ingest_pdf_bytes(b"%PDF-1.4\n" + bytes(64), "paper.pdf")

    assert not store.pdf_path(key).exists(), "an orphan PDF was left for a removed paper"
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
