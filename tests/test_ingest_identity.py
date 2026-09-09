"""Working out which paper a reference, page, or PDF is."""

from __future__ import annotations

import pytest

from doxograph import config, ingest, store


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
