"""The papers' own text is searched without a model."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import __main__, ingest, search, server, store

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
    assert search.terms("Steering, recovery -- STEERING") == ["steering", "recovery"]
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
