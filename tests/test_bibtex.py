"""BibTeX output is escaped and balanced."""

from __future__ import annotations

import re

import pytest

from doxograph import bib, ingest, store


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
