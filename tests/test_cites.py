"""Which papers cite which, read out of their reference lists."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import __main__, cites, server, store

from pdfs import minimal_pdf

ATTENTION = "Attention is all you need"
STEERING = "Steering and recovery in language models"


def _paper(key: str, title: str, pages: list[str], **fields) -> None:
    store.save_paper(store.new_paper(key, title=title, year=2026, **fields))
    store.pdf_path(key).write_bytes(minimal_pdf(pages))


def a_corpus() -> None:
    _paper("vas2017attention", ATTENTION, [ATTENTION, "We propose the Transformer."],
           source={"kind": "arxiv", "id": "1706.03762"})
    _paper("roe2026steering", STEERING, [
        STEERING + "\nWe build on the Transformer.",
        "R EFERENCES\n[1] A Vaswani et al. Attention is all you need. arXiv:1706.03762v5, 2017.\n"
        "[2] Someone Else. A paper that is not in the pile. 2020.",
    ])
    _paper("li2025steer", "Steering does not wash out", [
        "Steering does not wash out",
        "7. References\nRoe, A. Steering and recovery in language\nmodels. 2026.",
    ])


def test_the_reference_list_is_what_comes_after_the_last_heading():
    text = "Body mentions references in passing.\n\fReferences\n[1] A paper.\n"
    assert cites.reference_text(text).strip() == "[1] A paper."
    # A section number, a small-capitals heading, and a colon are all the same.
    assert cites.reference_text("x\n7. REFERENCES:\n[1] A paper.\n").strip() == "[1] A paper."
    assert cites.reference_text("x\nR EFERENCES\n[1] A paper.\n").strip() == "[1] A paper."
    assert cites.reference_text("A paper with no reference list at all.") == ""


def test_a_paper_is_found_by_its_arxiv_id_or_its_title():
    a_corpus()
    assert cites.edges() == [
        {"from": "li2025steer", "to": "roe2026steering"},        # by title, broken across lines
        {"from": "roe2026steering", "to": "vas2017attention"},   # by arXiv id, version and all
    ]


def test_a_reference_to_something_outside_the_corpus_is_not_an_edge():
    a_corpus()
    assert not any(edge["to"] == "someone-else" for edge in cites.edges())
    assert len(cites.edges()) == 2


def test_a_title_too_short_to_be_sure_of_is_not_looked_for():
    short = {"key": "x", "title": "Scaling laws", "source": {}}
    assert cites.fingerprints(short) == []
    enough = {"key": "y", "title": "Scaling laws for neural language models", "source": {}}
    assert cites.fingerprints(enough) == ["scalinglawsforneurallanguagemodels"]


def test_a_doi_in_a_reference_list_counts():
    _paper("ng2026saes", "Sparse autoencoders find features", ["Sparse autoencoders find features"],
           doi="10.1234/abcd.5678")
    _paper("roe2026steering", STEERING, [
        STEERING,
        "References\nNg, S. Sparse autoencoders. https://doi.org/10.1234/abcd.5678\n",
    ])
    assert cites.edges() == [{"from": "roe2026steering", "to": "ng2026saes"}]


def test_a_new_pdf_changes_the_answer_the_cache_gives():
    a_corpus()
    assert len(cites.edges()) == 2
    _paper("wu2026silent", "A silent paper", [
        "A silent paper",
        "References\nVaswani, A. Attention is all you need. 2017.",
    ])
    assert {"from": "wu2026silent", "to": "vas2017attention"} in cites.edges()


def test_the_citations_route_and_command():
    a_corpus()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/citations").json()["edges"] == cites.edges()
    assert __main__.main(["cites"]) == 0


def test_the_command_says_so_when_nothing_cites_anything(capsys):
    _paper("solo2026", "Alone in the corpus", ["Alone in the corpus", "References\nNobody. 1999."])
    assert __main__.main(["cites"]) == 1
    assert "no paper's reference list names another" in capsys.readouterr().err


def test_a_heading_may_say_more_than_the_word():
    for heading in ("References and Notes", "REFERENCES CITED", "7. Bibliography (Primary)",
                    "R EFERENCES", "References:"):
        text = f"Body of the paper.\n{heading}\n[1] A paper.\n"
        assert cites.reference_text(text).strip() == "[1] A paper.", heading
    # But not a line that only begins with the letters by accident.
    assert cites.reference_text("x\nReferenced below.\n[1] A.\n") == ""


def test_the_article_s_own_bibliography_is_not_lost_to_the_supplement_s():
    """A paper with an appendix has two reference lists. Taking only the last
    drops every work the article itself cites."""
    _paper("cited", ATTENTION, [ATTENTION, "A paper."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("appendix-only", "A paper cited only in the appendix",
           ["A paper cited only in the appendix", "Text."])
    _paper("roe2026steering", STEERING, [
        STEERING,
        "References\n[1] A Vaswani et al. Attention is all you need. 2017.",
        "A Appendix\nMore detail.",
        "References\n[2] Somebody. A paper cited only in the appendix. 2026.",
    ])
    assert {edge["to"] for edge in cites.edges() if edge["from"] == "roe2026steering"} == {
        "cited", "appendix-only"}


def test_a_title_inside_a_longer_title_is_not_a_citation_of_both():
    _paper("short", "Attention is all you need", ["Attention is all you need", "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. Attention is all you need for image restoration. 2026.",
    ])
    assert [edge["to"] for edge in cites.edges() if edge["from"] == "citing"] == ["long"]


def test_a_line_of_prose_that_starts_with_the_word_is_not_a_heading():
    """"References to Figure 2 show…" begins with the word too, and reading the
    rest of the paper as a reference list invents citations out of its prose."""
    _paper("cited", ATTENTION, [ATTENTION, "Text."])
    _paper("prose", "A paper with no bibliography", [
        "A paper with no bibliography",
        "References to Figure 2 show\nthat Attention is all you need, as others put it.",
    ])
    assert cites.edges() == []


def test_both_papers_are_cited_when_the_list_names_both():
    """The shorter title sits inside the longer one's reference, but it is
    named on its own further down."""
    _paper("short", "Attention is all you need", ["Attention is all you need", "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. Attention is all you need for image restoration. 2026.\n"
        "[2] Vaswani et al. Attention is all you need. 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["long", "short"]


def test_a_longer_title_printed_twice_keeps_both_of_its_places():
    """The shorter title sits inside the longer one's second printing, in the
    supplement's list, and is named on its own nowhere."""
    _paper("short", "Attention is all you need", ["Attention is all you need", "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. Attention is all you need for image restoration. 2026.",
        "A Appendix\nMore detail.",
        "References\n[1] Somebody. Attention is all you need for image restoration. 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["long"]


def test_a_section_number_in_roman_comes_off_the_heading_too():
    """IEEE numbers its sections in Roman, so a bibliography opens with
    "VI. REFERENCES"."""
    for heading in ("VI. REFERENCES", "IV. Bibliography", "3. References"):
        text = f"Body of the paper.\n{heading}\n[1] A paper.\n"
        assert cites.reference_text(text).strip() == "[1] A paper.", heading
    # The letters are only taken off when a heading is what is left.
    assert cites.reference_text("x\nLiterature Cited\n[1] A.\n").strip() == "[1] A."
    assert cites.reference_text("x\nVivid examples follow.\n[1] A.\n") == ""


def test_a_paper_named_by_its_id_and_its_title_still_covers_a_shorter_title():
    """The entry carries both, and it is the title's span that covers the
    shorter title printed inside it."""
    _paper("short", "Attention is all you need", ["Attention is all you need", "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."],
           source={"kind": "arxiv", "id": "2401.00001"})
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. Attention is all you need for image restoration.\n"
        "arXiv:2401.00001, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["long"]


def test_a_reference_naming_one_of_two_twins_cites_that_one():
    """A preprint and its published version share a title and differ by
    identifier; the reference carries the title and one of the identifiers."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] A Vaswani et al. Attention is all you need.\n"
        "https://doi.org/10.5555/3295222.3295349, 2017.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["published"]
