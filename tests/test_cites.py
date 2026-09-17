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


def test_a_list_that_names_both_twins_cites_both():
    """The preprint by its identifier in one entry, the published version by
    its title in another: two printings of the title, two citations."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] A Vaswani et al. Attention is all you need. arXiv:1706.03762, 2017.\n"
        "[2] A Vaswani et al. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["preprint", "published"]


def test_a_heading_is_measured_by_its_letters_not_its_line():
    """pypdf can put a space between every letter of a small-capitals
    heading, which makes a short heading a long line."""
    spaced = "R E F E R E N C E S  A N D  F U R T H E R  R E A D I N G"
    assert len(spaced) > 40
    assert cites.reference_text(f"Body.\n{spaced}\n[1] A paper.\n").strip() == "[1] A paper."
    # A long line of prose is still prose, however it squashes.
    long_prose = "References were consulted for every claim in this section of the paper."
    assert cites.reference_text(f"Body.\n{long_prose}\n[1] A paper.\n") == ""


def test_a_twin_cited_by_a_bare_identifier_takes_no_title_printing():
    """One entry is an arXiv id with no title; the title in the next entry
    belongs to the other version, however close together they are printed."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] A Vaswani et al. arXiv:1706.03762, 2017.\n"
        "[2] A Vaswani et al. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["preprint", "published"]


def test_a_numbered_list_is_read_one_entry_at_a_time():
    assert cites.entries("[1] A paper. 2017.\n[2] Another paper. 2018.") == [
        "apaper2017", "anotherpaper2018"]
    for numbering in ("1. ", "(1) ", "1) "):
        assert len(cites.entries(f"{numbering}A paper.\n{numbering.replace('1', '2')}Another.")) == 2
    # An author-year list says nothing about where its entries are, and is
    # read whole, as it always was.
    whole = cites.entries("Vaswani, A. Attention is all you need. 2017.\nRoe, A. Steering. 2026.")
    assert len(whole) == 1


def test_an_entry_names_one_work_even_where_a_title_contains_another():
    """The long title's entry cites the long paper; the short paper's own
    entry, printed right after it, cites the short one."""
    _paper("short", "Attention is all you need", ["Attention is all you need", "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. Attention is all you need for image restoration. 2026.\n"
        "[2] Vaswani et al. Attention is all you need. 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["long", "short"]


def test_an_unnumbered_bibliography_keeps_every_citation():
    """No entry markers means the list is read whole, and reading it whole
    must not turn a page of references into a single work."""
    _paper("cited", ATTENTION, [ATTENTION, "Text."])
    _paper("other", "Steering and recovery in language models",
           ["Steering and recovery in language models", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nVaswani, A. Attention is all you need. NeurIPS, 2017.\n"
        "Roe, A. Steering and recovery in language models. 2026.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["cited", "other"]


def test_a_roman_numeral_does_not_eat_the_heading_it_numbers():
    for heading in ("VI. LITERATURE CITED", "IV. References", "Literature Cited"):
        text = f"Body.\n{heading}\n[1] A paper.\n"
        assert cites.reference_text(text).strip() == "[1] A paper.", heading


def test_one_doi_recorded_twice_is_one_identifier():
    paper = {"key": "x", "title": "A paper with a long enough title to look for",
             "doi": "10.1234/abcd", "source": {"kind": "doi", "id": "10.1234/abcd"}}
    assert cites.fingerprints(paper).count("101234abcd") == 1


def test_an_unnumbered_list_citing_a_contained_title_later_cites_both():
    """The shorter title's first printing is inside the longer one's; its own
    citation is further down, and there are no entry markers to separate them."""
    _paper("short", ATTENTION, [ATTENTION, "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nSomebody. Attention is all you need for image restoration. 2026.\n"
        "Vaswani, A. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["long", "short"]


def test_an_identifier_settles_a_twin_whatever_its_length():
    """The DOI is longer than the shared title, and the claim still belongs at
    the title: that is the place the two papers are arguing over."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."],
           doi="10.5555/3295222.3295349.an.unusually.long.suffix")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] A Vaswani et al. Attention is all you need. "
        "https://doi.org/10.5555/3295222.3295349.an.unusually.long.suffix, 2017.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["published"]


def test_a_bibliography_with_one_item_may_call_itself_reference():
    assert cites.reference_text("Body.\nReference\n[1] A paper.\n").strip() == "[1] A paper."
    assert cites.reference_text("Body.\nReferences\n[1] A paper.\n").strip() == "[1] A paper."
    assert cites.reference_text("Body.\nReferenced below.\n[1] A paper.\n") == ""


def test_an_unnumbered_list_naming_both_twins_cites_both():
    """The identifier sits beside one printing of the shared title and there
    are no entry markers to say which; as many printings as claimants means
    each of them has one."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nVaswani, A. Attention is all you need. arXiv:1706.03762, 2017.\n"
        "Vaswani, A. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["preprint", "published"]


def test_a_section_label_in_letters_comes_off_the_heading():
    """Appendices number their sections by letter: "A. REFERENCES"."""
    for heading in ("A. REFERENCES", "B. Bibliography", "VI. LITERATURE CITED", "3. References"):
        text = f"Body.\n{heading}\n[1] A paper.\n"
        assert cites.reference_text(text).strip() == "[1] A paper.", heading
    # A prefix that leaves prose behind is still prose.
    assert cites.reference_text("Body.\nSee references below.\n[1] A.\n") == ""
    assert cites.reference_text("Body.\nReferenced work.\n[1] A.\n") == ""


def test_a_paper_cited_by_its_identifier_survives_its_title_being_embedded():
    """One entry names it by DOI alone; another cites a paper whose longer
    title contains its title, and there are no entry markers to separate them."""
    _paper("short", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nSomebody. Attention is all you need for image restoration. 2026.\n"
        "Vaswani, A. https://doi.org/10.5555/3295222.3295349, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["long", "short"]
