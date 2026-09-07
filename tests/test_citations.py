"""Citation markers survive blank and institutional authors."""

from __future__ import annotations

from doxograph import __main__, export, extract, ingest, store


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
