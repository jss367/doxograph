"""Working out which paper a reference, page, or PDF is."""

from __future__ import annotations

import re
import time

import pytest
from fastapi.testclient import TestClient

from doxograph import config, ingest, server, store


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


SICI = "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2"


@pytest.mark.parametrize("token,expected", [
    ("10.1038/s41586-021-03819-2.", "10.1038/s41586-021-03819-2"),
    ("10.1038/s41586-021-03819-2,", "10.1038/s41586-021-03819-2"),
    ("(10.1145/3442188.3445922)", "10.1145/3442188.3445922"),
    ("[10.1038/x.y].", "10.1038/x.y"),
    ("doi:10.1038/example.", "10.1038/example"),
    ("https://doi.org/10.1038/example;", "10.1038/example"),
    # parentheses that belong to the identifier survive
    ("10.1002/(SICI)1097-0258(19980815)17:15", "10.1002/(SICI)1097-0258(19980815)17:15"),
    # and so do the angle brackets of a SICI code, while wrapping ones go
    (f"{SICI}.", SICI),
    (f"doi:{SICI}", SICI),
    (f"https://doi.org/{SICI}", SICI),
    (f"<{SICI}>", SICI),
    ("<10.1038/example>", "10.1038/example"),
    ("<https://doi.org/10.1038/example>.", "10.1038/example"),
])
def test_citation_punctuation_is_stripped_from_dois(token, expected):
    refs, unknown = ingest.parse_refs(token)
    assert unknown == []
    assert [(r.kind, r.value) for r in refs] == [("doi", expected)]


def test_markup_after_a_doi_is_not_read_as_part_of_it():
    """Only a SICI segment opens an angle bracket inside a DOI. A closing tag
    or an inline one after the DOI stays out of it."""
    html = ('<head><title>A paper</title>'
            '<span class="doi">10.1038/example</span><i>10.1145/3442188.3445922</i><br></head>')
    client = FakePageClient("https://journal.example.org/a", html)
    ref = ingest.resolve_page("https://journal.example.org/a", client)
    assert (ref.kind, ref.value) == ("doi", "10.1038/example")


def test_a_sici_doi_in_a_files_metadata_is_read_whole():
    xmp = f"<rdf:li>doi:{SICI}</rdf:li>"
    assert re.search(ingest.DOI_RE, xmp).group(0) == SICI


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


PROJECT_PAGE = """<head><title>Prompt Injection as Role Confusion</title></head><body>
<h1>Prompt Injection as Role Confusion</h1>
<p>Human red-teamers <a href="https://arxiv.org/abs/2510.09023">win</a>.</p>
<pre><code>@inproceedings{ye2026,
  title = {Prompt Injection as {Role} Confusion},
  author = {Ye, Charles and Cui, Jasmine},
  year = {2026},
  %s
}</code></pre></body>"""


@pytest.mark.parametrize("field,expected", [
    ("url = {https://arxiv.org/abs/2603.12277}", ("arxiv", "2603.12277")),
    ("eprint = {2603.12277}, archivePrefix = {arXiv}", ("arxiv", "2603.12277")),
    ('journal = "arXiv preprint arXiv:2603.12277"', ("arxiv", "2603.12277")),
    ("doi = {10.1145/3442188.3445922}", ("doi", "10.1145/3442188.3445922")),
    ("url = {https://doi.org/10.1145/3442188.3445922}", ("doi", "10.1145/3442188.3445922")),
    ("howpublished = {doi:10.1145/3442188.3445922}", ("doi", "10.1145/3442188.3445922")),
    ("note = {doi:10.1145/3442188.3445922}", ("doi", "10.1145/3442188.3445922")),
    ("note = {https://doi.org/10.1145/3442188.3445922}", ("doi", "10.1145/3442188.3445922")),
    ("doi = {10.1234/foo\\_bar}", ("doi", "10.1234/foo_bar")),
    ("url = {https://doi.org/10.1234/foo\\_bar}", ("doi", "10.1234/foo_bar")),
])
def test_a_project_page_is_identified_by_its_own_bibtex(field, expected):
    """A project page carries no citation metadata, only a BibTeX block."""
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == expected


def test_bibtex_for_another_paper_does_not_hijack_the_page():
    html = ('<head><title>Our lab</title></head><body><h1>Publications</h1>'
            '<pre>@article{doe2025, title={A Study of Something Else},'
            ' url={https://arxiv.org/abs/2510.09023}}</pre></body>')
    client = FakePageClient("https://lab.example.org/", html)
    with pytest.raises(ValueError, match="paste the arXiv ID"):
        ingest.resolve_page("https://lab.example.org/", client)


def test_a_publications_list_is_not_passed_off_as_one_of_its_papers():
    """Each paper under its own heading, each with its BibTeX, is still a list."""
    papers = [("Prompt Injection as Role Confusion", "2603.12277"),
              ("A Study of Something Else Entirely", "2510.09023")]
    html = "<head><title>Our lab: publications</title></head><body>" + "".join(
        f"<h1>{title}</h1><pre>@article{{p{n}, title={{{title}}},"
        f" url={{https://arxiv.org/abs/{arxiv}}}}}</pre>"
        for n, (title, arxiv) in enumerate(papers)) + "</body>"
    client = FakePageClient("https://lab.example.org/publications", html)
    with pytest.raises(ValueError, match="paste the arXiv ID"):
        ingest.resolve_page("https://lab.example.org/publications", client)


def test_a_generic_page_title_does_not_claim_a_longer_citation():
    """A citation that only begins with the page's title is another paper."""
    html = ("<head><title>Natural Language Processing</title></head><body>"
            "<pre>@book{t2022, title={Natural Language Processing with Transformers},"
            " url={https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://nlp.example.org/", html)
    with pytest.raises(ValueError, match="paste the arXiv ID"):
        ingest.resolve_page("https://nlp.example.org/", client)


def test_a_page_title_may_add_the_sites_name_to_the_papers():
    html = ("<head><title>Prompt Injection as Role Confusion | Project Page</title></head>"
            "<body><pre>@article{ye2026, title={Prompt Injection as Role Confusion},"
            " url={https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://role-confusion.github.io/", html)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


def test_a_cited_arxiv_paper_does_not_outrank_the_entrys_own_doi():
    """An abstract may mention another paper; only where the entry is published counts."""
    field = ("abstract = {We build on arXiv:2510.09023 and \\url{https://arxiv.org/abs/2510.09023}},"
             " keywords = {arXiv:2510.09023}, doi = {10.1145/3442188.3445922}")
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")


def test_a_field_name_inside_a_value_is_not_a_field():
    field = "url = {https://example.org/?eprint=2510.09023}, doi = {10.1145/3442188.3445922}"
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")


def test_a_commented_out_field_is_not_read():
    """A stale identifier kept behind `%` must not win over the live one."""
    field = ("% doi = {10.1234/old},\n  doi = {10.1145/3442188.3445922},"
             " note = {https://example.org/a%20b}, % eprint = {2510.09023}\n")
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("doi", "10.1145/3442188.3445922")


def test_a_commented_out_entry_is_not_read():
    """A stale entry kept behind `%` must not win over the live one after it."""
    html = ("<head><title>Prompt Injection as Role Confusion</title></head>"
            "<body><p>Beats the baseline by 95%.</p><pre>\n"
            "% @article{old, title={Prompt Injection as Role Confusion},"
            " doi={10.1234/wrong}}\n"
            "@article{ye2026, title={Prompt Injection as Role Confusion},"
            " url={https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://role-confusion.github.io/", html)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


def test_an_entry_inside_a_comment_block_is_not_read():
    """A stale entry kept inside `@comment{...}` must not win over the live one."""
    html = ("<head><title>Prompt Injection as Role Confusion</title></head><body><pre>"
            "@comment{ @article{old, title={Prompt Injection as Role Confusion},"
            " doi={10.1234/wrong}} }\n"
            "@article{ye2026, title={Prompt Injection as Role Confusion},"
            " url={https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://role-confusion.github.io/", html)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


def test_a_sici_doi_keeps_its_angle_brackets():
    """A literal `<` in a page is written `&lt;`, so it is text, not a tag."""
    field = ("doi = {10.1002/(SICI)1097-0258(19980815/30)17:15/16"
             "&lt;1661::AID-SIM968&gt;3.0.CO;2-2}")
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == (
        "doi", "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2")


def test_a_percent_earlier_on_a_minified_line_does_not_hide_the_entry():
    html = ("<head><title>Prompt Injection as Role Confusion</title></head>"
            "<body><p>Beats the baseline by 95%.</p>"
            + BIBTEX % "Prompt Injection as Role Confusion" + "</body>")
    client = FakePageClient("https://role-confusion.github.io/", html)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


@pytest.mark.parametrize("bibtex", [
    'title = "Schr{\\"o}dinger Methods for Quantum Control"',
    'title = {Schr\\"{o}dinger Methods for Quantum Control}',
])
def test_an_accented_title_matches_the_page(bibtex):
    html = ("<head><title>Schr&ouml;dinger Methods for Quantum Control</title></head>"
            f"<body><pre>@article{{s2026, {bibtex},"
            " url = {https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://quantum.example.org/", html)
    ref = ingest.resolve_page("https://quantum.example.org/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


def test_a_link_that_only_contains_a_doi_is_not_one():
    """Only a link written as a DOI names one; a URL can have any path."""
    field = "url = {https://example.org/10.1145/3442188.3445922}"
    client = FakePageClient("https://role-confusion.github.io/", PROJECT_PAGE % field)
    with pytest.raises(ValueError, match="paste the arXiv ID"):
        ingest.resolve_page("https://role-confusion.github.io/", client)


def test_a_word_printed_by_a_tex_macro_is_part_of_the_title():
    html = ("<head><title>A LaTeX Workflow for Science</title></head><body>"
            "<pre>@article{w2026, title={A {\\LaTeX} Workflow for Science},"
            " url={https://arxiv.org/abs/2603.12277}}</pre></body>")
    client = FakePageClient("https://latex.example.org/", html)
    ref = ingest.resolve_page("https://latex.example.org/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


BIBTEX = ("<pre>@article{ye2026, title={%s},"
          " url={https://arxiv.org/abs/2603.12277}}</pre>")


@pytest.mark.parametrize("head,title", [
    ("<title>Safety &amp; Alignment Methods</title>", "Safety \\& Alignment Methods"),
    ("<title>Home</title></head><body><h1>Prompt <em>Injection</em> as<br>Role Confusion</h1>",
     "Prompt Injection as Role Confusion"),
    ('<meta name="citation_title" content="Project page">'
     '<meta property="og:title" content="Prompt Injection as Role Confusion">',
     "Prompt Injection as Role Confusion"),
    ('<title>Home</title>'
     '<meta content="Prompt Injection as Role Confusion" property="og:title">',
     "Prompt Injection as Role Confusion"),
])
def test_the_page_title_is_read_as_text_wherever_it_is(head, title):
    html = f"<head>{head}</head><body>{BIBTEX % title}</body>"
    client = FakePageClient("https://role-confusion.github.io/", html)
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")


@pytest.mark.parametrize("junk", [
    "@a{" * 130_000,
    "@comment{" * 50_000,
    "<!--" * 50_000,
    "<h1>" * 50_000,
    "<h1 " * 90_000,
    "<" * 200_000,
    "<head" * 50_000,
    "<head>" + "<title>" * 50_000 + "</head>",
], ids=["entry", "comment-block", "html-comment", "h1", "h1-attrs", "tag", "head", "title"])
def test_unterminated_markup_is_read_in_one_pass(junk):
    """A page full of openers that never close must not take quadratic time."""
    html = ("<h1>Prompt Injection as Role Confusion</h1>"
            + BIBTEX % "Prompt Injection as Role Confusion" + junk)
    client = FakePageClient("https://role-confusion.github.io/", html)
    started = time.monotonic()
    ref = ingest.resolve_page("https://role-confusion.github.io/", client)
    assert (ref.kind, ref.value) == ("arxiv", "2603.12277")
    assert time.monotonic() - started < 5


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
AMR_TITLE = "Abstract Meaning Representation for Sembanking"


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
    "10.1234/amr.2013.1": AMR_TITLE,
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


AMR_PAGE_ONE = f"""
{AMR_TITLE}
Proceedings of the Made-Up Workshop, 2013.  https://doi.org/10.1234/amr.2013.1

Abstract
We describe a sembank of simple, whole-sentence semantic structures.
"""


def test_a_title_that_says_abstract_is_still_identity(monkeypatch, tmp_path):
    """The word in the title is not the heading, and cutting there left
    nothing above the cut for the title to be found in."""
    identity_probe(monkeypatch, AMR_PAGE_ONE)
    meta = ingest.guess_from_pdf(tmp_path / "upload.pdf", None, "upload.pdf")
    assert meta["doi"] == "10.1234/amr.2013.1"
    assert meta["title"] == AMR_TITLE


CITES_AMR_PAGE = f"""
{JOURNAL_TITLE}

Abstract. We build on {AMR_TITLE} (doi:10.1234/amr.2013.1) and show that the
annotations do not survive distribution shift.
"""


def test_a_cited_title_that_says_abstract_is_still_not_identity(monkeypatch, tmp_path):
    """Passing over the title's own "abstract" does not open the abstract up."""
    calls = identity_probe(monkeypatch, CITES_AMR_PAGE)
    meta = ingest.guess_from_pdf(tmp_path / "working-paper.pdf", None, "working-paper.pdf")

    assert calls["doi"] == ["10.1234/amr.2013.1"], "the DOI was never checked"
    assert meta["doi"] == "", f"filed under the paper it cites: {meta['title']}"


CITES_AMR_WITHOUT_A_HEADING = f"""
{JOURNAL_TITLE}
A short note. We build on {AMR_TITLE} (doi:10.1234/amr.2013.1) and show that
the annotations do not survive distribution shift.
"""


def test_a_cited_title_that_says_abstract_on_a_page_with_no_heading_is_not_identity(
        monkeypatch, tmp_path):
    """With no heading after it, nothing says the printing is the page's own
    title rather than a citation of it, so the cut stays at the word."""
    calls = identity_probe(monkeypatch, CITES_AMR_WITHOUT_A_HEADING)
    meta = ingest.guess_from_pdf(tmp_path / "note.pdf", None, "note.pdf")

    assert calls["doi"] == ["10.1234/amr.2013.1"], "the DOI was never checked"
    assert meta["doi"] == "", f"filed under the paper it cites: {meta['title']}"


def test_only_the_titles_own_printing_of_the_word_is_passed_over():
    """A later "abstract" in a citation is not skipped for the title's sake."""
    text = f"{JOURNAL_TITLE}\nAbstract\nWe cite {AMR_TITLE}.\n"
    assert ingest.front_matter(text, AMR_TITLE) == f"{JOURNAL_TITLE}\n"
    own = f"{AMR_TITLE}\nAbstract\nWe describe a sembank.\n"
    assert ingest.front_matter(own, AMR_TITLE) == f"{AMR_TITLE}\n"


CITES_AMR_THEN_SAYS_ABSTRACT = f"""
{JOURNAL_TITLE}
A short note. We build on {AMR_TITLE} (doi:10.1234/amr.2013.1) and show that
the annotations abstract away from the text they were drawn from.
"""


def test_a_later_abstract_in_prose_does_not_pass_a_cited_title_over(
        monkeypatch, tmp_path):
    """Only a heading after the printing says it is the page's own title;
    the word in a sentence does not."""
    calls = identity_probe(monkeypatch, CITES_AMR_THEN_SAYS_ABSTRACT)
    meta = ingest.guess_from_pdf(tmp_path / "note.pdf", None, "note.pdf")

    assert calls["doi"] == ["10.1234/amr.2013.1"], "the DOI was never checked"
    assert meta["doi"] == "", f"filed under the paper it cites: {meta['title']}"


def test_the_heading_after_the_title_can_be_set_several_ways():
    for heading in ("Abstract\n", "ABSTRACT\n", "Abstract. We", "Abstract—We",
                    "Abstract: We", "Abstract We", "  Abstract We"):
        own = f"{AMR_TITLE}\n{heading} describe a sembank.\n"
        cut = ingest.front_matter(own, AMR_TITLE)
        assert cut.rstrip(" ") == f"{AMR_TITLE}\n", heading


# --- a reference already on file is recognized before any lookup ----------

def refuse(*args, **kwargs):
    raise AssertionError("a lookup went out for a paper already on file")


@pytest.fixture
def no_lookups(monkeypatch):
    for name in ("fetch_arxiv", "fetch_crossref", "fetch_pdf", "resolve_page"):
        monkeypatch.setattr(ingest, name, refuse)


def paper_on_file(key: str, **fields) -> str:
    """A paper with its PDF, which is what `already_stored` asks for."""
    store.save_paper(store.new_paper(key, **fields))
    store.pdf_path(key).write_bytes(b"%PDF-1.4\nbody")
    return key


def test_a_stored_arxiv_reference_is_answered_without_a_lookup(no_lookups):
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941v1", "url": ""})
    assert ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941v2", "")) == ("doe2026study", False)


def test_a_stored_doi_is_answered_without_a_lookup(no_lookups):
    paper_on_file("doe2026study", doi="10.1038/example",
                  source={"kind": "doi", "id": "10.1038/example", "url": ""})
    assert ingest.ingest_ref(ingest.Ref("doi", "10.1038/example", "")) == ("doe2026study", False)


def test_a_stored_pdf_link_is_not_downloaded_again(no_lookups):
    url = "https://journal.example.org/a.pdf"
    paper_on_file("doe2026study", source={"kind": "url", "id": url, "url": url, "pdf_url": url})
    assert ingest.ingest_ref(ingest.Ref("pdf", url, "")) == ("doe2026study", False)


def test_an_unrelated_reference_is_still_fetched(monkeypatch):
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""})
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda ident, client: {
        "title": "Another", "authors": [], "year": 2026, "abstract": "", "venue": "",
        "doi": "", "source": {"kind": "arxiv", "id": ident, "url": "", "pdf_url": ""},
    })
    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06942", ""))
    assert created and key != "doe2026study"


def test_a_paper_without_its_pdf_is_looked_up_again():
    """The quick answer must not stand in front of the download retry."""
    store.save_paper(store.new_paper(
        "doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""}))
    assert ingest.already_stored(ingest.Ref("arxiv", "2602.06941", "")) is None


def test_a_landing_page_is_resolved_before_it_can_be_recognized(monkeypatch):
    """A page names nothing until it is read, so the quick answer waits for that."""
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""})
    html = '<head><link rel="canonical" href="https://arxiv.org/abs/2602.06941"></head>'
    page = ingest.Ref("page", "https://arxiv.org/abs/2602.06941", "")
    monkeypatch.setattr(ingest, "fetch_arxiv", refuse)

    assert ingest.already_stored(page) is None, "a page cannot be recognized unresolved"
    client = FakePageClient("https://arxiv.org/abs/2602.06941", html)
    assert ingest.ingest_ref(page, client) == ("doe2026study", False)


# --- and the web app says so in the response, without a job ---------------

@pytest.fixture
def web(monkeypatch):
    monkeypatch.setattr(server, "_jobs", {})
    monkeypatch.setattr(server._pool, "submit", refuse)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        yield client


def test_a_reference_already_here_is_reported_without_a_job(web):
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""})
    store.add_claim("doe2026study", {"text": "Steering recovers."})

    body = web.post("/api/ingest",
                    json={"text": "arxiv.org/abs/2602.06941", "extract": True}).json()

    assert body == {"queued": 0, "unknown": [],
                    "known": [{"ref": "arxiv.org/abs/2602.06941", "key": "doe2026study"}]}
    assert web.get("/api/jobs").json()["jobs"] == []


def test_a_paper_still_waiting_to_be_read_is_queued(monkeypatch):
    """Re-pasting a reference is how an extraction that failed is retried."""
    monkeypatch.setattr(server, "_jobs", {})
    submitted = []
    monkeypatch.setattr(server._pool, "submit", lambda *args: submitted.append(args))
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""})

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = client.post("/api/ingest",
                           json={"text": "arxiv.org/abs/2602.06941", "extract": True}).json()

    assert (body["queued"], body["known"]) == (1, [])
    assert len(submitted) == 1


def test_a_paper_with_no_claims_is_not_queued_when_nothing_will_read_it(web):
    """With analysis off there is nothing left for a job to do."""
    config.set_ai_enabled(False)
    paper_on_file("doe2026study", source={"kind": "arxiv", "id": "2602.06941", "url": ""})

    body = web.post("/api/ingest",
                    json={"text": "arxiv.org/abs/2602.06941", "extract": True}).json()

    assert body["queued"] == 0
    assert [entry["key"] for entry in body["known"]] == ["doe2026study"]
