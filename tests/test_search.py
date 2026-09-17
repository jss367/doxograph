"""The papers' own text is searched without a model."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import __main__, ingest, quotes, search, server, store

from pdfs import minimal_pdf

STEERING = [
    "Steering and recovery in language models",
    "We steer the residual stream and watch what happens.\n"
    "Recovery under steering is a path-dependent outcome across all three model scales.",
    "Sandbagging is not what this paper is about, though we mention it once.",
]
GAIT = [
    "Quadruped gait control",
    "A quadruped recovers its gait after a push. Recovery here means something else entirely.",
]


def a_corpus() -> None:
    for key, title, pages in (("roe2026steering", "Steering and recovery", STEERING),
                              ("ling2025gait", "Quadruped gait control", GAIT)):
        store.save_paper(store.new_paper(key, title=title, authors=["Ada Roe"], year=2026))
        store.pdf_path(key).write_bytes(minimal_pdf(pages))


def test_a_query_is_its_words_and_every_one_has_to_be_there():
    assert search.terms("Steering, recovery -- STEERING") == ["Steering", "recovery"]
    a_corpus()
    both = search.search_papers("steering recovery")
    assert [hit["key"] for hit in both] == ["roe2026steering"]
    # "recovery" alone is in both, and the paper that says it more comes first.
    assert [hit["key"] for hit in search.search_papers("recovery")] == ["roe2026steering", "ling2025gait"]
    # A prefix, not a stem: "recovery" does not reach "recovers".
    assert [hit["occurrences"] for hit in search.search_papers("recovery")] == [2, 1]
    assert search.search_papers("steering penguins") == []
    assert search.search_papers("   ") == []


def test_a_term_matches_from_the_start_of_a_word_and_not_inside_one():
    a_corpus()
    assert [hit["key"] for hit in search.search_papers("steer")] == ["roe2026steering"]
    assert search.search_papers("eering") == []


def test_a_hit_says_where_in_the_paper_it_is():
    a_corpus()
    hit = search.search_papers("sandbagging")[0]
    assert hit["key"] == "roe2026steering" and hit["occurrences"] == 1
    passage = hit["passages"][0]
    assert passage["page"] == 3
    assert "Sandbagging is not what this paper is about" in passage["text"]


def test_a_paper_with_no_claims_is_still_searched():
    """The paper a text search is most likely to be looking for is the one
    nothing has been read out of yet."""
    a_corpus()
    assert store.load_paper("roe2026steering")["claims"] == []
    assert [hit["key"] for hit in search.search_papers("residual stream")] == ["roe2026steering"]


def test_the_text_is_read_once_and_reused():
    a_corpus()
    assert search.search_papers("steering")
    assert store.text_path("roe2026steering").exists()
    # The stored text is what gets searched, which is how we can tell.
    store.text_path("roe2026steering").write_text("penguins only", encoding="utf-8")
    search._texts.clear()
    assert [hit["key"] for hit in search.search_papers("penguins")] == ["roe2026steering"]


def test_a_pdf_arriving_brings_its_text_with_it(tmp_path):
    store.save_paper(store.new_paper("wu2026silent", title="Silent"))
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(minimal_pdf("A quiet paper about sandbagging."))
    assert ingest.publish_pdf("wu2026silent", staged) is True
    assert "sandbagging" in store.text_path("wu2026silent").read_text(encoding="utf-8").lower()


def test_the_search_route_ranks_papers_and_names_them():
    a_corpus()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        found = client.get("/api/search", params={"q": "steering recovery"}).json()
        assert found["terms"] == ["steering", "recovery"]
        assert [paper["key"] for paper in found["papers"]] == ["roe2026steering"]
        assert found["papers"][0]["title"] == "Steering and recovery"
        assert found["papers"][0]["authors"] == ["Ada Roe"]
        assert client.get("/api/search", params={"q": ""}).json()["papers"] == []


def test_the_search_command_prints_the_passages(capsys):
    a_corpus()
    assert __main__.main(["search", "sandbagging"]) == 0
    out = capsys.readouterr().out
    assert "roe2026steering" in out and "p. 3" in out
    assert __main__.main(["search", "penguins"]) == 1


def test_a_passage_does_not_run_across_a_page_break():
    a_corpus()
    hit = search.search_papers("steer")[0]
    first = hit["passages"][0]
    # The term is in the title on page 1; the passage stops where the page does.
    assert first["page"] == 1
    assert "residual stream" not in first["text"]


def test_a_papers_length_is_weighed_against_the_whole_corpus():
    """BM25 discounts a paper for being longer than the corpus average. Taking
    that average over the papers that matched would make a paper's score depend
    on which other papers happened to match, which a ranking must not do."""
    store.save_paper(store.new_paper("hit", title="hit"))
    store.pdf_path("hit").write_bytes(minimal_pdf("A paper that mentions sandbagging once."))

    def filler(key: str, words: int) -> None:
        store.save_paper(store.new_paper(key, title=key))
        store.pdf_path(key).write_bytes(minimal_pdf(" ".join(["penguin"] * words)))

    filler("short", 5)
    short = search.search_papers("sandbagging")[0]["score"]
    # One paper swapped for a longer one holding none of the query's words:
    # the corpus is the same size and the term is still in one paper, so the
    # only thing that moved is the average length the hit is measured against.
    store.delete_paper("short")
    filler("long", 400)
    assert search.search_papers("sandbagging")[0]["score"] > short


def test_a_term_is_searched_as_it_was_typed():
    """Case folding expands some letters — ß to ss — and the papers are
    searched as they are written, where IGNORECASE cannot put them back."""
    assert search.terms("Steering RECOVERY steering") == ["Steering", "RECOVERY"]
    store.save_paper(store.new_paper("strasse", title="Strasse"))
    store.pdf_path("strasse").write_bytes(minimal_pdf("Die Straße war lang und leer."))
    assert [hit["key"] for hit in search.search_papers("Straße")] == ["strasse"]
    assert [hit["key"] for hit in search.search_papers("straße")] == ["strasse"]


def test_case_is_out_of_the_question_on_both_sides():
    """`re.IGNORECASE` cannot map SS to ß: both sides are folded instead."""
    store.save_paper(store.new_paper("strasse", title="Strasse"))
    store.pdf_path("strasse").write_bytes(minimal_pdf("Die Straße war lang und leer."))
    for query in ("Straße", "straße", "STRASSE", "strasse"):
        assert [hit["key"] for hit in search.search_papers(query)] == ["strasse"], query
    # And the passage is quoted as the paper writes it, not as it was folded.
    assert "Straße" in search.search_papers("STRASSE")[0]["passages"][0]["text"]


def test_a_folded_position_reads_back_to_the_papers_own_characters():
    folded, offsets = search.fold_with_offsets("Die Straße war")
    assert folded == search.fold("Die Straße war")
    assert len(offsets) == len(folded)
    at = folded.index("strasse")
    assert "Die Straße war"[offsets[at]:].startswith("Straße")


def test_the_text_is_stored_before_the_papers_lock_is_let_go(monkeypatch, tmp_path):
    """Two publishes of one key must not interleave: one could read its PDF,
    the other replace it and store its text, and the first then write the text
    of a paper that is no longer there. Reading it under the lock says they
    cannot."""
    import threading

    from doxograph import ingest

    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    second = tmp_path / "second.pdf"
    second.write_bytes(minimal_pdf("The second paper, about penguins."))
    other = threading.Thread(target=ingest.publish_pdf, args=("doe2026study", second))
    blocked = []
    original = search.cache_text

    def cache_text(key: str) -> None:
        if not blocked:
            other.start()
            other.join(timeout=0.3)
            # Still waiting on the lock this publish holds, which is the point.
            blocked.append(other.is_alive())
        original(key)

    monkeypatch.setattr(search, "cache_text", cache_text)
    first = tmp_path / "first.pdf"
    first.write_bytes(minimal_pdf("The first paper, about sandbagging."))
    assert ingest.publish_pdf("doe2026study", first) is True
    assert blocked == [True]

    other.join(timeout=5)
    # The second publish lands whole, text and all, once the first lets go.
    stored = store.text_path("doe2026study").read_text(encoding="utf-8")
    assert "penguins" in stored and "sandbagging" not in stored


def test_a_passage_comes_back_already_cut_around_its_terms():
    store.save_paper(store.new_paper("strasse", title="Strasse"))
    store.pdf_path("strasse").write_bytes(minimal_pdf("Die Straße war lang. Die Straße war leer."))
    passage = search.search_papers("STRASSE")[0]["passages"][0]
    assert [p["text"] for p in passage["parts"] if p["mark"]] == ["Straße", "Straße"]
    # The pieces are the passage, whole and in order.
    assert "".join(p["text"] for p in passage["parts"]) == passage["text"]

    # Two terms in one passage come back in order and do not overlap.
    store.save_paper(store.new_paper("two", title="Two"))
    store.pdf_path("two").write_bytes(minimal_pdf("Recovery under steering is path-dependent."))
    passage = search.search_papers("steering recovery")[0]["passages"][0]
    assert [p["text"] for p in passage["parts"] if p["mark"]] == ["Recovery", "steering"]


def test_a_word_broken_across_a_line_is_one_word_to_a_search():
    """A two-column paper breaks words at every line, and `quotes.tidy`
    already knows to put them back for reading."""
    store.save_paper(store.new_paper("split", title="Split"))
    store.pdf_path("split").write_bytes(
        minimal_pdf("We study the transfor-\nmation of steered activations."))
    assert [hit["key"] for hit in search.search_papers("transformation")] == ["split"]
    # And the passage still quotes the paper, hyphen closed up as it is read.
    passage = search.search_papers("transformation")[0]["passages"][0]
    assert "transformation" in passage["text"]
    assert [p["text"] for p in passage["parts"] if p["mark"]] == ["transformation"]


def a_paper_of(key: str, text: str) -> None:
    """A paper whose text is written straight to the store. The test PDFs
    carry Latin-1 alone, and the text cache is what a search reads anyway."""
    store.save_paper(store.new_paper(key, title=key))
    store.text_path(key).parent.mkdir(parents=True, exist_ok=True)
    store.text_path(key).write_text(text, encoding="utf-8")


def test_a_term_in_a_script_without_spaces_is_found_inside_a_run():
    """Every character of 语言模型能力 is a word character, so a word boundary
    only ever matches at the start of the run."""
    a_paper_of("cjk", "A study of 语言模型能力.")
    assert [hit["key"] for hit in search.search_papers("模型")] == ["cjk"]
    passage = search.search_papers("模型")[0]["passages"][0]
    assert [p["text"] for p in passage["parts"] if p["mark"]] == ["模型"]
    # A Latin term still has to start a word.
    a_paper_of("latin", "Steering works.")
    assert search.search_papers("eering") == []


def test_the_folded_text_is_kept_to_a_size_not_a_count(monkeypatch):
    monkeypatch.setattr(search, "TEXT_BUDGET", 400)
    for key in ("one", "two", "three"):
        a_paper_of(key, " ".join(["penguin"] * 40))
        assert search.folded_text(key)
    # Three papers of ~320 characters do not fit in 400, and the oldest goes.
    assert search._texts_size <= 400
    assert len(search._texts) < 3
    assert store.text_path("one").exists()      # dropped from memory, not from disk


def test_a_paper_without_spaces_is_measured_by_its_characters():
    """`\\w+` over unspaced text finds one word however long the paper is, so
    every Chinese paper would be the same length and none discounted."""
    filler = "能力很强。"                      # says nothing about 模型
    assert search._length(filler * 50) > search._length(filler)
    a_paper_of("brief", f"语言模型的{filler}")
    a_paper_of("long", f"语言模型的{filler * 200}")
    # One mention each: the brief one spends a larger part of itself on it.
    assert [hit["key"] for hit in search.search_papers("模型")] == ["brief", "long"]


def test_a_word_split_at_a_page_break_is_one_word_to_a_search():
    a_paper_of("split", "We measure the transfor-\x0cmation of steered activations.")
    assert [hit["key"] for hit in search.search_papers("transformation")] == ["split"]


def test_a_passage_keeps_the_word_that_matched_across_a_page_break():
    a_paper_of("split", "Text before.\x0cWe measure the transfor-\x0cmation of activations.")
    passage = search.search_papers("transformation")[0]["passages"][0]
    assert [p["text"] for p in passage["parts"] if p["mark"]] == ["transformation"]
    assert "transformation" in passage["text"]


def test_an_accent_is_found_however_the_pdf_spells_it():
    """A PDF gives an accented letter whole or as a letter and a mark; a query
    is typed whichever way the keyboard does it."""
    a_paper_of("decomposed", "A study of the cafe\u0301 as a workplace.")   # e + acute
    for query in ("caf\u00e9", "cafe\u0301", "CAF\u00c9", "cafe"):
        assert [hit["key"] for hit in search.search_papers(query)] == ["decomposed"], repr(query)
    # And the passage still quotes the paper as it spells it.
    passage = search.search_papers("caf\u00e9")[0]["passages"][0]
    assert [part["text"] for part in passage["parts"] if part["mark"]] == ["cafe\u0301"]


def test_lao_is_a_script_without_spaces_too():
    a_paper_of("lao", "A paper about ພາສາລາວ.")
    assert [hit["key"] for hit in search.search_papers("ລາວ")] == ["lao"]


def test_text_older_than_its_pdf_is_read_again(tmp_path):
    """A paper replaced by hand leaves the old text in place."""
    import os

    store.save_paper(store.new_paper("swapped", title="Swapped"))
    store.pdf_path("swapped").write_bytes(minimal_pdf("A paper about sandbagging."))
    assert [hit["key"] for hit in search.search_papers("sandbagging")] == ["swapped"]

    store.pdf_path("swapped").write_bytes(minimal_pdf("A paper about penguins."))
    stamp = store.text_path("swapped").stat().st_mtime_ns + 1_000_000
    os.utime(store.pdf_path("swapped"), ns=(stamp, stamp))
    search._texts.clear()
    quotes._cache.clear()
    assert search.search_papers("sandbagging") == []
    assert [hit["key"] for hit in search.search_papers("penguins")] == ["swapped"]


def test_a_query_saying_one_word_two_ways_says_it_once():
    a_paper_of("cafe", "A study of the cafe as a workplace.")
    assert search.terms("café cafe") == ["café"]
    assert search.search_papers("café cafe")[0]["occurrences"] == 1


def test_the_command_says_nothing_found_when_every_hit_has_gone(capsys, monkeypatch):
    a_corpus()
    assert __main__.main(["search", "sandbagging"]) == 0
    capsys.readouterr()
    # The paper is removed between the search and the reading of it.
    monkeypatch.setattr(store, "load_paper", _gone)
    assert __main__.main(["search", "sandbagging"]) == 1
    assert "nothing in the papers' text" in capsys.readouterr().err


def _gone(key):
    raise KeyError(key)


def test_the_command_refuses_a_limit_that_shows_nothing(capsys):
    import pytest
    a_corpus()
    for bad in ("0", "-3"):
        with pytest.raises(SystemExit):
            __main__.main(["search", "--limit", bad, "sandbagging"])
        assert "not a number of papers to show" in capsys.readouterr().err


def test_two_terms_either_side_of_a_page_break_are_two_passages():
    """A passage is cut to one page, so terms on opposite sides of a break
    cannot both be shown in one."""
    a_paper_of("split", "Recovery under steering.\x0cSandbagging is measured separately.")
    hit = search.search_papers("recovery sandbagging")[0]
    assert len(hit["passages"]) == 2
    marked = [part["text"] for passage in hit["passages"] for part in passage["parts"] if part["mark"]]
    assert sorted(marked) == ["Recovery", "Sandbagging"]
