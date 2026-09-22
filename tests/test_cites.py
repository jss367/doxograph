"""Which papers cite which, read out of their reference lists."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from doxograph import __main__, cites, quotes, search, server, store

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
    # "Literature" and "Works" name a bibliography only in company: a paper's
    # reading of the field comes before its references, and taken as the
    # heading it would make the body into the list.
    assert cites.reference_text("Literature\nWe build on Attention is all you need.\n"
                                "References\n[1] A paper.\n").strip() == "[1] A paper."
    assert cites._is_heading("literaturecited") and cites._is_heading("workscited")
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
    assert [e.squashed for e in
            cites.entries("[1] A paper. 2017.\n[2] Another paper. 2018.")] == [
        "apaper2017", "anotherpaper2018"]
    # A marker the extraction left touching its entry is still a marker,
    # unless what touches it is another digit: `3.5` is a section number.
    assert [e.squashed for e in
            cites.entries("[1]A paper. 2017.\n[2]Another paper. 2018.")] == [
        "apaper2017", "anotherpaper2018"]
    assert len(cites.entries("1.Smith, J. A paper.\n2.Jones, A. Another.")) == 2
    assert len(cites.entries("3.5 of the paper says so.\n3.6 says otherwise.")) == 1
    for numbering in ("1. ", "(1) ", "1) "):
        assert len(cites.entries(f"{numbering}A paper.\n{numbering.replace('1', '2')}Another.")) == 2
    # A marker the extraction left alone on its line numbers the list too.
    assert [e.squashed for e in cites.entries("1\nA paper. 2017.\n2\nAnother paper. 2018.")] == [
        "apaper2017", "anotherpaper2018"]
    # But a bare number at the foot or the head of a page is the page's own,
    # and cutting the list there would part an entry from its second line.
    assert len(cites.entries("Vaswani, A. Attention is all you need.\n12\n"
                             "\farXiv:1706.03762, 2017.\n")) == 1
    # What tells them apart is the counting: a marker carries on from the one
    # before it, wherever on the page it falls.
    assert len(cites.entries("1\nA paper. 2017.\n\f2\nAnother paper. 2018.\n")) == 2
    # Unless the list numbers itself another way, where a bare number is the
    # page's however well it counts.
    assert len(cites.entries("[11] A paper. 2017.\n12\n\farXiv:1706.03762, 2017.\n")) == 1
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
    for heading in ("A. REFERENCES", "B. Bibliography", "VI. LITERATURE CITED", "3. References",
                    "(A) References", "[A] Bibliography", "List of References",
                    # The space after the label is what an extraction loses.
                    "A.References", "IV.References", "[A]References",
                    # A supplement numbers its own: neither a letter nor a
                    # roman numeral accounts for "S1".
                    "S1. References", "A1. Bibliography",
                    # And numbers it within its section, in figures or not.
                    "A.1 References", "S1.2 Bibliography",
                    "A.I References", "IV.A References"):
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


def test_a_line_of_prose_is_not_a_heading_with_its_first_word_taken_off():
    """"See References" and "No references" are prose, and reading the rest of
    the paper as a bibliography invents citations out of it."""
    for prose in ("See References", "No references", "Cf. references", "Our references"):
        assert cites.reference_text(f"Body.\n{prose}\n[1] A paper.\n") == "", prose
    # The labels that are labels still come off.
    for heading in ("A. REFERENCES", "VI. LITERATURE CITED", "3. References", "References"):
        assert cites.reference_text(f"Body.\n{heading}\n[1] A paper.\n").strip() == "[1] A paper.", heading


def test_a_marker_alone_on_its_line_still_starts_an_entry():
    """pypdf puts the number on its own line often enough, and a list that
    does not split is read as one work."""
    assert [e.squashed for e in
            cites.entries("[1]\nA paper. 2017.\n[2]\nAnother. 2018.")] == [
        "apaper2017", "another2018"]
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1]\nA Vaswani et al. arXiv:1706.03762, 2017.\n"
        "[2]\nA Vaswani et al. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["preprint", "published"]


def test_a_label_is_a_label_because_of_what_follows_it():
    """"A. REFERENCES" is a heading; "A reference" is the start of a line."""
    for heading in ("A. REFERENCES", "VI. LITERATURE CITED", "3. References", "References",
                    "B) Bibliography"):
        assert cites.reference_text(f"Body.\n{heading}\n[1] A paper.\n").strip() == "[1] A paper.", heading
    for prose in ("A reference", "No references", "See References", "I reference the above"):
        assert cites.reference_text(f"Body.\n{prose}\n[1] A paper.\n") == "", prose


def test_three_twins_and_two_printings_keep_them_all():
    """One printing is beside an identifier and the other is beside nothing;
    the second cites one of the two unidentified versions and the list does
    not say which."""
    for key, extra in (("preprint", {"source": {"kind": "arxiv", "id": "1706.03762"}}),
                       ("published", {}), ("reissue", {})):
        _paper(key, ATTENTION, [ATTENTION, "Text."], **extra)
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nVaswani, A. Attention is all you need. arXiv:1706.03762, 2017.\n"
        "Vaswani, A. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == [
        "preprint", "published", "reissue"]


def test_a_heading_is_built_out_of_heading_words():
    """Listing the phrases meant meeting each new one for the first time."""
    for heading in ("References and Recommended Reading", "References and Notes",
                    "Selected Bibliography", "Works Cited", "Key References",
                    "References and Further Reading", "Literature Cited",
                    "Bibliography (Primary Sources)"):
        text = f"Body of the paper.\n{heading}\n[1] A paper.\n"
        assert cites.reference_text(text).strip() == "[1] A paper.", heading
    for prose in ("References to Figure 2 show", "Referenced work", "No references"):
        assert cites.reference_text(f"Body.\n{prose}\n[1] A.\n") == "", prose


def test_one_unnumbered_entry_naming_a_twin_by_its_identifier_cites_that_twin():
    """The shared title and the preprint's id are printed together on one
    line, which is one entry naming one work — the published version is not
    cited by an entry that says arXiv."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nVaswani, A. Attention is all you need. arXiv:1706.03762, 2017.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["preprint"]


def test_a_bare_identifier_in_an_uncut_list_leaves_the_title_to_its_twin():
    """No entry markers, a bare arXiv id for the preprint, and one printing of
    the shared title for the published version."""
    _paper("preprint", ATTENTION, [ATTENTION, "Text."], source={"kind": "arxiv", "id": "1706.03762"})
    _paper("published", ATTENTION, [ATTENTION, "Text."], doi="10.5555/3295222.3295349")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nVaswani, A. arXiv:1706.03762, 2017.\n"
        "Vaswani, A. Attention is all you need. NeurIPS, 2017.",
    ])
    assert sorted(e["to"] for e in cites.edges() if e["from"] == "citing") == ["preprint", "published"]


def test_an_entry_marker_at_the_top_of_a_page_starts_an_entry():
    assert [e.squashed for e in
            cites.entries("[1] A paper. 2017.\x0c[2] Another paper. 2018.")] == [
        "apaper2017", "anotherpaper2018"]


def test_editing_a_claim_does_not_throw_away_the_citations():
    """A claim, a topic or a tension moving does not change a bibliography,
    and rereading every paper for it is the whole corpus twice over."""
    a_corpus()
    assert cites.edges()
    before = list(cites._cache)
    assert before, "the answer was stored"

    store.add_claim("roe2026steering", {"text": "A claim.", "tags": ["steering"]})
    store.add_tag("steering", "Adding a direction to activations.")
    assert list(cites._cache) == before, "a claim moved the key"
    assert cites.edges() and list(cites._cache) == before

    # What the citations are read from does move it.
    paper = store.load_paper("roe2026steering")
    store.save_paper({**paper, "title": "Steering and recovery, revisited"})
    cites.edges()
    assert list(cites._cache) != before


def test_a_contents_page_is_not_where_the_reference_list_starts():
    """A contents page writes the word and puts the page number under it. The
    body that follows names a paper, and reading the list from there would
    call the mention a citation."""
    _paper("attention", ATTENTION, [ATTENTION, "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper\nContents\nIntroduction\n1\nReferences\n12",
        "Introduction\nWe build on Attention is all you need throughout.",
        "References\nNobody. A work nobody wrote. 1999.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []
    # However much room is left between the entry and the page it points at.
    # Written out rather than read from a PDF: pypdf gives back no blank line
    # however many the page has.
    assert cites.reference_text(
        "Contents\nIntroduction\n1\nReferences\n\n\n\n\n12\n"
        "\fIntroduction\nWe build on Attention is all you need.\n"
        "\fReferences\nNobody. A work nobody wrote. 1999.\n"
    ) == "Nobody. A work nobody wrote. 1999.\n"


def test_a_supplement_does_not_make_the_main_list_a_contents_entry():
    """The main list's first marker comes out as a bare `1` and a supplement
    gives the paper a second heading. Read as a contents entry, the main list
    and every work it cites would be passed over."""
    _paper("attention", ATTENTION, [ATTENTION, "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "Body text about steering.",
        "References\n1\nA Vaswani et al. Attention is all you need. 2017.",
        "Additional References\nNobody. A work nobody wrote. 1999.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["attention"]


def test_a_list_whose_first_entry_is_a_number_on_its_own_line_is_still_the_list():
    """The only heading a paper has is taken however the line under it reads:
    a bare `1` there is the first entry, not a page number."""
    _paper("attention", ATTENTION, [ATTENTION, "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n1\nA Vaswani et al. Attention is all you need. 2017.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["attention"]


def test_a_doi_another_doi_begins_with_is_not_cited():
    """`10.1234/foo` reads straight through `10.1234/foo.bar` once the dots
    are squashed out, and the two are different works."""
    _paper("short", "One paper", ["One paper", "Text."], doi="10.1234/foo")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. Another work. https://doi.org/10.1234/foo.bar, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []
    # The identifier itself still cites it, version and all.
    _paper("citing2", "Another citing paper", [
        "Another citing paper",
        "References\n[1] Nobody. One paper. https://doi.org/10.1234/foo, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing2"] == ["short"]


def test_an_identifier_is_read_from_both_ends():
    """The arXiv id `1706.03762` is printed inside the DOI `10.1706/03762`,
    and ends where it ends. A DOI is not versioned either: `10.1234/foov2` is
    a DOI of its own, not a second printing of `10.1234/foo`."""
    _paper("attention", "A paper with a very long title indeed",
           ["A paper with a very long title indeed", "Text."],
           source={"kind": "arxiv", "id": "1706.03762"})
    _paper("foo", "Another paper with a long enough title",
           ["Another paper with a long enough title", "Text."], doi="10.1234/foo")
    _paper("foobar", "A third paper with a long enough title",
           ["A third paper with a long enough title", "Text."], doi="10.1234/foob.ar")
    _paper("bazqux", "A fourth paper with a long enough title",
           ["A fourth paper with a long enough title", "Text."], doi="10.1234/baz.qux-quux")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. A work nobody wrote. https://doi.org/10.1706/03762, 2026.\n"
        "[2] Nobody. Another work. https://doi.org/10.1234/foov2, 2026.\n"
        # A DOI suffix is allowed brackets and semicolons as well as dots.
        "[3] Nobody. A third work. https://doi.org/10.1234/foo(2), 2026.\n"
        # And it is allowed to end with something that reads as an arXiv id.
        "[4] Nobody. A fourth work. https://doi.org/10.9999/abc/1706.03762, 2026.\n"
        # However much punctuation stands between the DOI and the rest of it.
        "[5] Nobody. A fifth work. https://doi.org/10.1234/foo/(2), 2026.\n"
        # And text that only squashes into an identifier is not one.
        "[6] Nobody. A sixth work. Vol. 10, 1234. Foo. 2026.\n"
        # Nor is a DOI that keeps its punctuation somewhere else.
        "[7] Nobody. A seventh work. https://doi.org/10.1234/fo.obar, 2026.\n"
        # Or the same marks in a different order.
        "[8] Nobody. An eighth work. https://doi.org/10.1234/baz-qux.quux, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []


def test_an_arxiv_id_is_cited_with_or_without_its_version():
    _paper("attention", "A paper with a very long title indeed",
           ["A paper with a very long title indeed", "Text."],
           source={"kind": "arxiv", "id": "1706.03762"})
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Somebody. A work. arXiv:1706.03762v5, 2017.",
    ])
    _paper("citing2", "Another citing paper", [
        "Another citing paper",
        "References\n[1] Somebody. A work. arXiv:1706.03762, 2017.",
    ])
    # Or linked to as the PDF it is served as.
    _paper("citing3", "A third citing paper", [
        "A third citing paper",
        "References\n[1] Somebody. A work. https://arxiv.org/pdf/1706.03762v5.pdf, 2017.",
    ])

    edges = cites.edges()
    for citing in ("citing", "citing2", "citing3"):
        assert [e["to"] for e in edges if e["from"] == citing] == ["attention"], citing
    # Or wrapped onto an indented line. Written out rather than read from a
    # PDF, since pypdf gives back no indentation however the page is laid out.
    entry = quotes.build("Somebody. A work. arXiv:1706.\n    03762, 2017.")
    mark = quotes.squash("1706.03762")
    assert cites._whole(entry, entry.squashed.find(mark), len(mark), True, "1706.03762")


def test_a_title_that_is_the_start_of_a_longer_one_is_not_a_citation():
    """The longer work need not be in the corpus for the entry to be citing
    it: a title that the entry goes on writing is a title of its own."""
    _paper("attention", ATTENTION, [ATTENTION, "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. Attention is all you need for image "
        "restoration. 2026.\n"
        # A subtitle is the title going on too, however far down the page the
        # line it is written on begins.
        "[2] Nobody. Attention is all you need: for image restoration. 2027.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []
    wrapped = quotes.build("Nobody. Attention is all you need:\n"
                           "            for image restoration. 2027.")
    title = quotes.squash(ATTENTION)
    assert not cites._bounded(wrapped, wrapped.squashed.find(title), len(title))
    # A colon in front of the title is another matter: some styles put one
    # between the authors and the title, and reading it as a subtitle would
    # drop every citation in a paper written that way.
    _paper("citing4", "A fourth citing paper", [
        "A fourth citing paper",
        "References\n[1] A Vaswani, N Shazeer: Attention is all you need. NeurIPS, 2017.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing4"] == ["attention"]
    # An extraction that left a space in front of the full stop, or put it on
    # the next line, has still ended the title there.
    title = quotes.squash(ATTENTION)
    for raw in ("Nobody. Attention is all you need . NeurIPS, 2017.",
                "Nobody. Attention is all you need\n. NeurIPS, 2017."):
        spaced = quotes.build(raw)
        assert cites._bounded(spaced, spaced.squashed.find(title), len(title)), raw
    # But a colon and a subtitle is the title going on, spaced or not, and so
    # is a dash with room around it.
    for raw in ("Nobody. Attention is all you need :  for image restoration.",
                "Nobody. Attention is all you need \u2014 for image restoration."):
        spaced = quotes.build(raw)
        assert not cites._bounded(spaced, spaced.squashed.find(title), len(title)), raw
    # And the paper itself is still cited where the entry stops at its title,
    # with a full stop after it or with the break before the next entry.
    _paper("citing2", "Another citing paper", [
        "Another citing paper",
        "References\n[1] A Vaswani et al. Attention is all you need. NeurIPS, 2017.",
    ])
    _paper("citing3", "A third citing paper", [
        "A third citing paper",
        "References\n[1] A Vaswani et al. Attention is all you need\n"
        "[2] Nobody. A work nobody wrote. 1999.",
    ])
    edges = cites.edges()
    assert [e["to"] for e in edges if e["from"] == "citing2"] == ["attention"]
    assert [e["to"] for e in edges if e["from"] == "citing3"] == ["attention"]


def test_a_title_inside_a_longer_word_is_not_a_citation():
    """`Understanding neural network` reads straight through `Understanding
    neural networks` once the spaces are gone, and the two are different
    papers. A title has to start and end where a word does."""
    _paper("singular", "Understanding neural network behaviour under steering",
           ["Understanding neural network behaviour under steering", "Text."])
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. Understanding neural network behaviour under "
        "steerings and other things. 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []
    # An accent written as a mark of its own is part of its letter, and the
    # word goes on past it. Read against the entry, since the test PDFs are
    # latin-1 and a decomposed accent cannot be written into one.
    entry = quotes.build("Nobody. Understanding the cafe\u0301ine of it all. 2026.")
    title = quotes.squash("Understanding the caf\u00e9")
    assert not cites._bounded(entry, entry.squashed.find(title), len(title))

    # A hyphen or an apostrophe with more word on the other side is inside a
    # word too, wrapped onto the next line or not.
    mark = quotes.squash("Understanding neural network")
    for text in ("Nobody. Understanding neural network-based methods. 2026.",
                 "Nobody. Understanding neural network-\n  based methods. 2026.",
                 "Nobody. Understanding neural network\u2019s reach. 2026."):
        entry = quotes.build(text)
        assert not cites._bounded(entry, entry.squashed.find(mark), len(mark)), text
    # But a title that ends where the entry stops writing it is bounded,
    # wherever the line breaks after it.
    entry = quotes.build("Nobody. Understanding neural network.\nSmith, J. Another. 2026.")
    assert cites._bounded(entry, entry.squashed.find(mark), len(mark))

    # The same either side: a title that starts where a wrapped word goes on
    # starts inside that word.
    tail = quotes.squash("Based methods for language models")
    entry = quotes.build("Nobody. Network-\n  based methods for language models. 2026.")
    assert not cites._bounded(entry, entry.squashed.find(tail), len(tail))
    entry = quotes.build("Nobody. Another work.\n  Based methods for language models. 2026.")
    assert cites._bounded(entry, entry.squashed.find(tail), len(tail))


def test_two_dois_that_differ_by_what_they_end_with_are_two_papers():
    """`10.1234/foo_` is a DOI of its own. Squashing keeps no character for
    the underscore, so what the entry prints after the identifier is what
    tells the two apart."""
    _paper("plain", "A paper with a long enough title",
           ["A paper with a long enough title", "Text."], doi="10.1234/foo")
    _paper("trailing", "Another paper with a long enough title",
           ["Another paper with a long enough title", "Text."], doi="10.1234/foo_")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. A work. https://doi.org/10.1234/foo_, 2026.",
    ])
    _paper("citing2", "Another citing paper", [
        "Another citing paper",
        "References\n[1] Nobody. A work. https://doi.org/10.1234/foo, 2026.",
    ])
    edges = cites.edges()
    assert [e["to"] for e in edges if e["from"] == "citing"] == ["trailing"]
    assert [e["to"] for e in edges if e["from"] == "citing2"] == ["plain"]


def test_a_doi_that_ends_in_a_bracket_is_cited_by_its_own_printing():
    """`10.1234/foo(2)` is a DOI and `normalize_doi` keeps the pair. Squashing
    leaves nothing for the bracket, so the span a match covers ends at the 2
    and the identifier it is compared with has to end there too."""
    _paper("bracketed", "A paper with a long enough title",
           ["A paper with a long enough title", "Text."], doi="10.1234/foo(2)")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. A work. https://doi.org/10.1234/foo(2), 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["bracketed"]


SICI = "10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2"


def test_a_sici_doi_is_cited_by_its_printing():
    """A SICI code carries a `<page::id>` segment, and `ingest.DOI_RE` stores
    the DOI with it. The brackets are part of the identifier, wrapping ones or
    not."""
    _paper("sici", "A paper with a long enough title",
           ["A paper with a long enough title", "Text."], doi=SICI)
    _paper("citing", "The citing paper", [
        "The citing paper",
        f"References\n[1] Nobody. A work. https://doi.org/{SICI}, 1998.",
    ])
    _paper("wrapped", "Another citing paper", [
        "Another citing paper",
        f"References\n[1] Nobody. A work. <doi:{SICI}>.",
    ])
    edges = cites.edges()
    assert [e["to"] for e in edges if e["from"] == "citing"] == ["sici"]
    assert [e["to"] for e in edges if e["from"] == "wrapped"] == ["sici"]


def test_a_doi_a_sici_code_goes_on_from_is_not_cited():
    """What comes before the segment is not the DOI: the identifier goes on
    through the bracket. A tag after a DOI does not go on with it."""
    head = SICI.split("<")[0]
    entry = quotes.build(f"Nobody. A work. https://doi.org/{SICI}, 1998.")
    mark = quotes.squash(head)
    assert not cites._whole(entry, entry.squashed.find(mark), len(mark), printed=head)
    tagged = quotes.build("Nobody. A work. 10.1234/foo</a>, 2026.")
    mark = quotes.squash("10.1234/foo")
    assert cites._whole(tagged, tagged.squashed.find(mark), len(mark), printed="10.1234/foo")


def test_a_dash_an_extraction_wrote_another_way_is_the_same_identifier():
    """A PDF can write a hyphen as an en dash, and it is the same DOI. Read
    against the entry rather than through one, since the test PDFs are
    latin-1 and cannot carry the dash."""
    entry = quotes.build("Nobody. A work. https://doi.org/10.1234/foo\u2013bar, 2026.")
    mark = quotes.squash("10.1234/foo-bar")
    assert cites._whole(entry, entry.squashed.find(mark), len(mark),
                        printed="10.1234/foo-bar")
    # A different DOI is still a different DOI.
    assert not cites._whole(entry, entry.squashed.find(mark), len(mark),
                            printed="10.1234/fooba.r")
    # An entry that finishes with the identifier finishes it.
    last = quotes.build("Nobody. A work. https://doi.org/10.1234/foo")
    plain = quotes.squash("10.1234/foo")
    assert cites._whole(last, last.squashed.find(plain), len(plain), printed="10.1234/foo")
    # A ligature is the letters it stands for, as it is to `quotes.squash`.
    ligature = quotes.build("Nobody. A work. https://doi.org/10.1234/o\ufb03ce, 2026.")
    office = quotes.squash("10.1234/office")
    assert cites._whole(ligature, ligature.squashed.find(office), len(office),
                        printed="10.1234/office")
    # An arXiv id at the end of a DOI is that DOI, wrapped or not.
    arxiv = quotes.squash("1706.03762")
    for raw in ("Nobody. A work. https://doi.org/10.9999/abc/1706.03762, 2026.",
                "Nobody. A work. https://doi.org/10.9999/abc/\n  1706.03762, 2026."):
        inside = quotes.build(raw)
        assert not cites._whole(inside, inside.squashed.find(arxiv), len(arxiv),
                                True, "1706.03762"), raw
    # But one entry ending in a DOI and the next beginning with an arXiv id
    # are two entries, and the break between them is not part of either.
    apart = quotes.build("Somebody. Another work. https://doi.org/10.9999/foo\n"
                         "1706.03762, 2017.")
    assert cites._whole(apart, apart.squashed.find(arxiv), len(arxiv),
                        True, "1706.03762")
    # And a dash the entry writes is where the identifier goes on, so a
    # shorter DOI does not end inside a longer one.
    longer = quotes.build("Nobody. A work. https://doi.org/10.1234/foo\u2013bar, 2026.")
    short = quotes.squash("10.1234/foo")
    assert not cites._whole(longer, longer.squashed.find(short), len(short),
                            printed="10.1234/foo")


def test_an_identified_paper_still_covers_a_title_printed_inside_its_own():
    """An unnumbered list cites the longer paper by title and DOI; the shorter
    corpus title reads inside that title and is cited nowhere."""
    _paper("short", ATTENTION, [ATTENTION, "Text."])
    _paper("long", "Attention is all you need for image restoration",
           ["Attention is all you need for image restoration", "Text."],
           doi="10.1234/restoration")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\nSomebody. Attention is all you need for image restoration. "
        "https://doi.org/10.1234/restoration, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["long"]


def test_text_that_moved_while_it_was_being_read_is_not_cached():
    """A read hands back what it found. A PDF landing before it comes back
    leaves different text behind, and sampling the paper afterwards would
    record that text as the one the scan read."""
    a_corpus()
    cites.edges()                       # every paper's text written down
    cites._cache.clear()
    real = search.paper_text
    planted = False

    def watching(key: str):
        nonlocal planted
        text = real(key)
        if not planted:
            planted = True
            store.pdf_path(key).write_bytes(
                minimal_pdf(["Another printing of this paper",
                             "References\nNobody. A work nobody wrote. 1999."]))
            store.text_path(key).unlink(missing_ok=True)
            real(key)
        return text

    cites.search.paper_text = watching
    try:
        cites.edges()
    finally:
        cites.search.paper_text = real
    assert not cites._cache, "an answer was stored for text it cannot speak for"


def test_a_pdf_replaced_by_hand_is_read_again():
    """The stored text is a cache of the PDF, and a hit answers without
    reading a paper. A key made of the text alone would not move, and the map
    would go on being served the arrows of the paper that used to be there."""
    a_corpus()
    assert any(e["to"] == "vas2017attention" for e in cites.edges())

    # The paper that cited it is replaced, its stored text left behind.
    store.pdf_path("roe2026steering").write_bytes(
        minimal_pdf(["A wholly different paper", "References\nNobody. A work. 1999."]))
    stamp = store.text_path("roe2026steering").stat().st_mtime_ns + 1_000_000
    os.utime(store.pdf_path("roe2026steering"), ns=(stamp, stamp))
    assert not any(e["from"] == "roe2026steering" for e in cites.edges())


def test_correcting_a_doi_is_a_different_paper_to_look_for():
    """Where an identifier keeps its punctuation decides what an entry cites,
    so a DOI corrected in place has to move the key — nothing in the text
    changes when it does."""
    _paper("cited", "A paper with a long enough title",
           ["A paper with a long enough title", "Text."], doi="10.1234/foo.bar")
    _paper("citing", "The citing paper", [
        "The citing paper",
        "References\n[1] Nobody. A work. https://doi.org/10.1234/foob.ar, 2026.",
    ])
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == []

    paper = store.load_paper("cited")
    store.save_paper({**paper, "doi": "10.1234/foob.ar"})
    assert [e["to"] for e in cites.edges() if e["from"] == "citing"] == ["cited"]


def test_the_answer_is_filed_under_the_text_it_was_read_from():
    """Not under the text as it stands when the answer is stored: a paper
    replaced while the scan was finishing would file the old citations under
    the new text's name, and the map would go on being served them."""
    a_corpus()
    cites._cache.clear()
    cites.edges()
    was_read = cites._read_signature(
        {paper["key"]: cites._identity(paper["key"]) for paper in store.all_papers()})
    assert list(cites._cache)[0].endswith(was_read)

    # Text left behind by a paper that is no longer in the corpus is not part
    # of it, and does not throw the answer away.
    before = list(cites._cache)
    store.text_path("gone2019").write_text("A paper that is not here.", encoding="utf-8")
    assert cites.edges() and list(cites._cache) == before


def test_a_pdf_arriving_mid_scan_is_not_cached_as_though_it_were_read():
    """A paper this scan has already passed gets new text. Its name has not
    moved, so nothing but the text itself would notice."""
    a_corpus()
    cites._cache.clear()
    seen: list[str] = []
    real = search.paper_text
    planted = False

    def watching(key: str):
        nonlocal planted
        seen.append(key)
        if len(seen) == 2 and not planted:
            # The paper read first has just had its PDF replaced.
            planted = True
            first = seen[0]
            store.pdf_path(first).write_bytes(
                minimal_pdf(["A much later printing of this paper",
                             "References\nNobody. A work nobody wrote. 1999."]))
            store.text_path(first).unlink(missing_ok=True)
            search.paper_text(first)
        return real(key)

    cites.search.paper_text = watching
    try:
        cites.edges()
    finally:
        cites.search.paper_text = real
    assert not cites._cache, "an answer that did not see every paper was stored"
