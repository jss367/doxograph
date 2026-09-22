"""BibTeX output is escaped and balanced."""

from __future__ import annotations

import pytest

from doxograph import bib, ingest, store


# --- BibTeX escaping ------------------------------------------------------

def test_a_url_is_written_verbatim():
    """biblatex and url.sty print a url as written, so an escape reaches the
    page as a backslash and the link no longer resolves."""
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", authors=["Jane Doe"], year=2026,
        source={"kind": "url", "id": "u", "url": "https://ex.org/~jane/a_b%20c?q=1&r=2#s"},
    ))
    text = bib.render()
    assert "url = {https://ex.org/~jane/a_b%20c?q=1&r=2#s}" in text
    # every field line must still close its brace
    for line in text.splitlines():
        if " = {" in line:
            assert line.rstrip(",").endswith("}"), line


def test_a_doi_is_written_verbatim():
    store.save_paper(store.new_paper("doe2026study", title="A Study", authors=["Jane Doe"],
                                     year=2026, venue="Nature", doi="10.1000/a%2Fb_c"))
    assert "doi = {10.1000/a%2Fb_c}" in bib.render()


def test_a_brace_in_a_url_is_percent_encoded():
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", authors=["Jane Doe"], year=2026, doi="10.1000/}x",
        source={"kind": "url", "id": "u", "url": "https://ex.org/a}b{c"},
    ))
    text = bib.render()
    assert "url = {https://ex.org/a%7Db%7Bc}" in text
    assert "doi = {10.1000/%7Dx}" in text


def test_a_caret_is_escaped():
    store.save_paper(store.new_paper("doe2026study", title="O(n^2) scaling",
                                     authors=["Jane Doe"], year=2026))
    assert r"title = {O(n\textasciicircum{}2) scaling}" in bib.render()


# --- Authors --------------------------------------------------------------

@pytest.mark.parametrize("authors,expected", [
    (["Research and Development Team"], "{Research and Development Team}"),
    (["ATLAS Collaboration"], "{ATLAS Collaboration}"),
    (["Society for Neuroscience", "Jane Doe"], "{Society for Neuroscience} and Jane Doe"),
    (["AT&T Laboratory"], r"{AT\&T Laboratory}"),
    (["Ludwig van Beethoven", "Jean de la Fontaine"],
     "Ludwig van Beethoven and Jean de la Fontaine"),
])
def test_an_institutional_author_is_one_literal_name(authors, expected):
    """Ingest keeps a Crossref institutional author as one string, and BibTeX
    split it on "and" or cited its last word as a surname."""
    store.save_paper(store.new_paper("doe2026study", title="A Study",
                                     authors=authors, year=2026))
    assert f"author = {{{expected}}}" in bib.render()


@pytest.mark.parametrize("authors,expected", [
    (["", ""], "Unknown"),
    ([" ", "\t"], "Unknown"),
    (None, "Unknown"),
    (["", "Jane Doe", "  "], "Jane Doe"),
])
def test_blank_authors_are_left_out(authors, expected):
    store.save_paper(store.new_paper("doe2026study", title="A Study",
                                     authors=authors, year=2026))
    assert f"author = {{{expected}}}" in bib.render()


@pytest.mark.parametrize("title,expected", [
    ("study}", r"study\textbraceright{}"),
    ("a{b}c", r"a\textbraceleft{}b\textbraceright{}c"),
    ("100% {done}", r"100\% \textbraceleft{}done\textbraceright{}"),
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


def test_every_bibtex_entry_has_balanced_braces():
    """A literal brace used to unbalance the field and swallow the rest.

    Every brace is counted, escaped or not. BibTeX and biber both count a brace
    whether or not a backslash comes before it, so `\\{` unbalanced the field
    just as a bare one did, and a count that skipped it passed that output.
    """
    for n, title in enumerate(["study}", "{a}", "plain", "a{b", "100% }x{"]):
        store.save_paper(store.new_paper(f"doe2026s{n}", title=title,
                                         authors=[f"A B{n}"], year=2026, venue="Journal"))
    text = bib.render()
    for entry_text in text.split("\n\n"):
        if not entry_text.strip():
            continue
        assert entry_text.count("{") == entry_text.count("}"), entry_text
        # every field line closes what it opens
        for line in entry_text.splitlines():
            if " = {" in line:
                assert line.rstrip(",").endswith("}"), line
                assert line.count("{") == line.count("}"), line


def test_a_metadata_free_upload_named_with_a_brace_round_trips(monkeypatch):
    """The exact case from the report: an upload called `study}.pdf`."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    ingest.ingest_pdf_bytes(b"%PDF-1.4\n" + bytes(32), "study}.pdf")
    text = bib.render()
    assert r"\textbraceright{}" in text
    assert text.count("{") == text.count("}"), text
