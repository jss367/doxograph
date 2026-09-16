"""A claim's quote is checked against the paper's own text."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from doxograph import __main__, extract, ingest, quotes, server, store

from pdfs import minimal_pdf

SENTENCE = "Recovery under steering is a path-dependent outcome across all three model scales."


def paper_with_pdf(key: str = "doe2026recovery", text: str = SENTENCE) -> str:
    store.save_paper(store.new_paper(key, title="Recovery"))
    store.pdf_path(key).write_bytes(minimal_pdf(text))
    return key


# --- matching -------------------------------------------------------------

def test_squash_ignores_case_punctuation_and_ligatures():
    assert quotes.squash("Path-Dependent,\n outcome") == "pathdependentoutcome"
    assert quotes.squash("eﬃcient") == "efficient"   # the ffi ligature


def test_squash_keeps_letters_in_every_script_and_drops_accents():
    assert quotes.squash("研究结果 — Résumé") == "研究结果resume"
    assert quotes.squash("Модель восстанавливается") == "модельвосстанавливается"


def test_a_cjk_quote_is_checked_like_any_other():
    hay = quotes.squash("实验表明，被引导的模型在大约一半的运行中恢复到原任务。")
    assert quotes.coverage(quotes.squash("被引导的模型在大约一半的运行中恢复"), hay) == 1.0
    assert quotes.coverage(quotes.squash("模型从不恢复到原任务"), hay) == 0.0


def test_errors_at_an_anchor_seam_do_not_hide_a_match():
    # Two wrong characters where the two full-length anchors meet, in a quote
    # of the minimum length: no full anchor survives, the half-length ones do.
    assert quotes.coverage("abcdefghijklmnopqrstuvwx", "abcdefghijkZYnopqrstuvwx") >= quotes._COVERAGE


def test_a_verbatim_quote_is_found_despite_line_breaks_and_hyphenation(tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(minimal_pdf("steering is a path-dependent out- come across"))
    assert quotes.verify(pdf, "steering is a path-dependent outcome across") is True


def test_a_paraphrase_is_not_found(tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(minimal_pdf(SENTENCE))
    assert quotes.verify(pdf, "Steering recovery depends on the path taken, at every scale.") is False


def test_a_quote_with_one_garbled_word_still_counts():
    hay = quotes.squash(SENTENCE)
    needle = quotes.squash(SENTENCE.replace("path-dependent", "path-dependant"))
    assert quotes.coverage(needle, hay) >= quotes._COVERAGE


def test_a_short_quote_must_match_exactly():
    hay = quotes.squash(SENTENCE)
    assert quotes.coverage(quotes.squash("Recovery under steering"), hay) == 1.0
    assert quotes.coverage(quotes.squash("Recovery under steer1ng"), hay) == 0.0


def test_nothing_to_check_is_none(tmp_path):
    assert quotes.verify(tmp_path / "missing.pdf", SENTENCE) is None
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(minimal_pdf(SENTENCE))
    assert quotes.verify(pdf, "") is None


def test_a_pdf_with_no_text_leaves_quotes_unchecked(tmp_path):
    # A scanned paper opens without error, but every page extracts to "".
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(minimal_pdf(""))
    assert quotes.pdf_text(pdf) is None
    assert quotes.verify(pdf, SENTENCE) is None
    assert quotes.verify(pdf, SENTENCE) is None     # the cached answer says the same
    key = paper_with_pdf(text="")
    claim = store.add_claim(key, {"text": "A.", "quote": SENTENCE})
    assert claim["quote_verified"] is None
    assert store.summarize(store.load_paper(key))["n_unverified"] == 0


def test_the_text_cache_notices_a_replaced_pdf(tmp_path):
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(minimal_pdf("first version of the paper text here"))
    assert quotes.verify(pdf, "first version of the paper text here") is True
    pdf.write_bytes(minimal_pdf("a wholly different second version of it"))
    import os
    os.utime(pdf, ns=(pdf.stat().st_atime_ns, pdf.stat().st_mtime_ns + 1_000_000))
    assert quotes.verify(pdf, "first version of the paper text here") is False


# --- the store records the check ------------------------------------------

def test_a_hand_added_claim_has_its_quote_checked():
    key = paper_with_pdf()
    good = store.add_claim(key, {"text": "A.", "quote": "Recovery under steering is a path-dependent outcome"})
    bad = store.add_claim(key, {"text": "B.", "quote": "Steering never recovers under any condition"})
    none = store.add_claim(key, {"text": "C."})
    assert good["quote_verified"] is True
    assert bad["quote_verified"] is False
    assert none["quote_verified"] is None


def test_editing_the_quote_rechecks_it():
    key = paper_with_pdf()
    claim = store.add_claim(key, {"text": "A.", "quote": "not in the paper at all, this one"})
    assert claim["quote_verified"] is False
    updated = store.update_claim(key, claim["id"], {"quote": "across all three model scales"})
    assert updated["quote_verified"] is True
    # Editing something else leaves the verdict alone.
    updated = store.update_claim(key, claim["id"], {"text": "B."})
    assert updated["quote_verified"] is True


def test_extraction_checks_every_fresh_quote():
    key = paper_with_pdf()
    payload = {
        "summary": "s", "relevance": "r", "proposed_tags": [],
        "claims": [
            {"text": "Real.", "kind": "finding", "strength": "headline", "tags": [],
             "evidence": "", "quote": "Recovery under steering is a path-dependent outcome",
             "locator": "p. 1", "ledger_links": []},
            {"text": "Invented.", "kind": "finding", "strength": "supporting", "tags": [],
             "evidence": "", "quote": "Steering always recovers within ten steps.",
             "locator": "p. 2", "ledger_links": []},
        ],
    }
    paper = extract.merge_extraction(key, payload)
    verdicts = {c["text"]: c["quote_verified"] for c in paper["claims"]}
    assert verdicts == {"Real.": True, "Invented.": False}
    assert store.summarize(paper)["n_unverified"] == 1


def test_verify_quotes_backfills_a_paper():
    key = paper_with_pdf()
    paper = store.load_paper(key)
    paper["claims"] = [store.new_claim(paper, text="A.", quote=SENTENCE)]
    del paper["claims"][0]["quote_verified"]     # as a corpus from before the check looks
    store.save_paper(paper)
    assert store.verify_quotes(key)["claims"][0]["quote_verified"] is True


def test_verify_route_and_command(capsys):
    key = paper_with_pdf()
    store.add_claim(key, {"text": "A.", "quote": "Steering always recovers within ten steps."})
    store.add_claim(key, {"text": "No quote, so nothing to count."})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(f"/api/papers/{key}/verify")
        assert response.json() == {"key": key, "n_unverified": 1}
        assert client.post("/api/papers/nobody/verify").status_code == 404
    assert __main__.main(["verify"]) == 1
    assert "0 of 1 quotes found, 1 not in the PDF" in capsys.readouterr().out
    store.update_claim(key, store.load_paper(key)["claims"][0]["id"], {"quote": SENTENCE})
    assert __main__.main(["verify", key]) == 0
    assert "1 of 1 quotes found" in capsys.readouterr().out
    # A key that is not a paper fails the run, not just a line on stderr.
    assert __main__.main(["verify", key, "nobody"]) == 1
    assert "nobody: no such paper" in capsys.readouterr().err


# --- reading the quote back out of the paper ------------------------------

PAGE_ONE = "An introduction that says very little about anything at all."
PAGE_TWO = (
    "We ran the experiment three times.\n"
    "Recovery under steering is a path-dependent out-\ncome across all three\n"
    "model scales. The effect is smaller at 7B."
)


def two_page_pdf(tmp_path) -> Path:
    pdf = tmp_path / "two.pdf"
    pdf.write_bytes(minimal_pdf([PAGE_ONE, PAGE_TWO]))
    return pdf


def test_the_squashed_text_carries_an_offset_into_the_paper():
    raw = "Straße — eﬃcient Résumé 42"
    text = quotes.build(raw)
    assert text.squashed == quotes.squash(raw)
    assert len(text.offsets) == len(text.squashed)
    # The ligature's three letters all point back at the one character.
    at = text.squashed.index("ffi")
    assert [raw[text.offsets[i]] for i in (at, at + 1, at + 2)] == ["ﬃ"] * 3
    assert raw[text.offsets[text.squashed.index("42")]] == "4"


def test_locate_gives_the_page_and_the_papers_own_sentence(tmp_path):
    found = quotes.locate(two_page_pdf(tmp_path), "Recovery under steering is a path-dependent outcome")
    assert found["found"] is True
    assert (found["page"], found["pages"]) == (2, 2)
    # The sentence is whole, and the word broken across two lines is rejoined.
    assert found["suggestion"] == (
        "Recovery under steering is a path-dependent outcome across all three model scales."
    )
    assert found["before"] == "We ran the experiment three times."
    assert found["after"] == "The effect is smaller at 7B."


def test_a_reworded_quote_still_finds_the_sentence_it_came_from(tmp_path):
    found = quotes.locate(two_page_pdf(tmp_path),
                          "Steering recovery is path dependant across all three model scales.")
    assert found["found"] is False
    assert found["page"] == 2
    assert found["suggestion"].startswith("Recovery under steering is a path-dependent")


def test_a_quote_from_nowhere_near_the_paper_has_no_passage_to_show(tmp_path):
    found = quotes.locate(two_page_pdf(tmp_path),
                          "Emperor penguins in Antarctica huddle to conserve warmth in winter.")
    assert found["found"] is False
    assert (found["page"], found["suggestion"], found["before"]) == (None, "", "")


def test_locate_has_nothing_to_say_without_a_quote_or_a_pdf(tmp_path):
    assert quotes.locate(tmp_path / "missing.pdf", SENTENCE) is None
    assert quotes.locate(two_page_pdf(tmp_path), "   ") is None


def test_a_sentence_is_not_cut_at_an_abbreviation(tmp_path):
    pdf = tmp_path / "abbrev.pdf"
    pdf.write_bytes(minimal_pdf(
        "Earlier work disagrees. Following Smith et al. (2024), we hold that the\n"
        "recovery rate is stable. That is our finding."))
    found = quotes.locate(pdf, "we hold that the recovery rate is stable")
    assert found["suggestion"] == (
        "Following Smith et al. (2024), we hold that the recovery rate is stable."
    )


def test_word_diff_marks_the_wording_and_not_the_punctuation():
    diff = quotes.word_diff("Steering recovery is path dependant.",
                            "Steering recovery is path-dependent.")
    assert [part["op"] for part in diff] == ["equal", "quote", "paper"]
    assert diff[1]["text"] == "path dependant."
    assert diff[2]["text"] == "path-dependent."
    # Case and punctuation drift alone is not a difference worth showing.
    assert quotes.word_diff("the Model recovers",
                            "the model recovers") == [{"op": "equal", "text": "the model recovers"}]


def test_a_locator_names_a_page_or_it_does_not():
    assert quotes.locator_page("p. 4") == 4
    assert quotes.locator_page("pp. 4-5") == 4
    assert quotes.locator_page("page 12, Sec. 3.1") == 12
    assert quotes.locator_page("Table 2") is None
    assert quotes.locator_page("") is None


# --- the extracted text is kept between runs ------------------------------

def test_the_text_is_extracted_once_and_read_from_the_cache_after(tmp_path):
    pdf, cache = two_page_pdf(tmp_path), tmp_path / "text" / "two.txt"
    assert quotes.verify(pdf, "Recovery under steering", cache) is True
    assert PAGE_ONE in cache.read_text(encoding="utf-8")

    # A stale cache is what gets read, which is how we can tell it was used.
    cache.write_text("wholly different text about penguins", encoding="utf-8")
    quotes._cache.clear()
    assert quotes.verify(pdf, "Recovery under steering", cache) is False

    # Until the PDF itself is newer, and then the paper is read again.
    os.utime(pdf, ns=(pdf.stat().st_atime_ns, cache.stat().st_mtime_ns + 1_000_000))
    quotes._cache.clear()
    assert quotes.verify(pdf, "Recovery under steering", cache) is True


def test_a_scanned_paper_is_not_parsed_again_on_every_claim(tmp_path):
    pdf, cache = tmp_path / "scan.pdf", tmp_path / "text" / "scan.txt"
    pdf.write_bytes(minimal_pdf(""))
    assert quotes.paper_text(pdf, cache) is None
    assert cache.exists() and not cache.read_text(encoding="utf-8").strip()


# --- the passage behind a claim, for the reviewer -------------------------

def paper_with_pages() -> str:
    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering"))
    store.pdf_path(key).write_bytes(minimal_pdf([PAGE_ONE, PAGE_TWO]))
    return key


def test_a_checked_quote_records_the_page_it_was_found_on():
    key = paper_with_pages()
    claim = store.add_claim(key, {"text": "A.", "quote": "Recovery under steering is a path-dependent"})
    assert (claim["quote_verified"], claim["quote_page"]) == (True, 2)
    # The text is kept for the next claim, and for the next run.
    assert store.text_path(key).exists()
    away = store.add_claim(key, {"text": "B.", "quote": "Penguins huddle to conserve warmth in winter."})
    assert (away["quote_verified"], away["quote_page"]) == (False, None)


def test_the_context_offers_the_papers_own_wording():
    key = paper_with_pages()
    claim = store.add_claim(key, {
        "text": "A.", "locator": "p. 1",
        "quote": "Steering recovery is path dependant across all three model scales."})
    found = store.quote_context(key, claim["id"])
    assert found["available"] is True
    assert found["page"] == 2 and found["locator_page"] == 1
    assert found["suggestion"].startswith("Recovery under steering is a path-dependent")
    assert {part["op"] for part in found["diff"]} == {"equal", "quote", "paper"}
    # Taking the wording is an ordinary edit, and it makes the quote check out.
    updated = store.update_claim(key, claim["id"], {"quote": found["suggestion"]})
    assert updated["quote_verified"] is True
    assert store.quote_context(key, claim["id"])["diff"] == []


def test_the_context_says_why_there_is_nothing_to_show():
    key = paper_with_pages()
    blank = store.add_claim(key, {"text": "A."})
    assert store.quote_context(key, blank["id"])["available"] is False
    assert "no quote" in store.quote_context(key, blank["id"])["reason"]

    store.pdf_path(key).unlink()
    quoted = store.add_claim(key, {"text": "B.", "quote": SENTENCE})
    missing = store.quote_context(key, quoted["id"])
    assert missing["available"] is False and "no PDF" in missing["reason"]

    store.pdf_path(key).write_bytes(minimal_pdf(""))
    scanned = store.quote_context(key, quoted["id"])
    assert scanned["available"] is False and "scan" in scanned["reason"]


def test_the_context_route_answers_for_a_claim_and_404s_otherwise():
    key = paper_with_pages()
    claim = store.add_claim(key, {"text": "A.", "quote": "Recovery under steering"})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        found = client.get(f"/api/papers/{key}/claims/{claim['id']}/quote-context").json()
        assert found["page"] == 2 and found["found"] is True
        assert client.get(f"/api/papers/{key}/claims/nope/quote-context").status_code == 404
        assert client.get(f"/api/papers/nobody/claims/{claim['id']}/quote-context").status_code == 404


def test_deleting_a_paper_takes_its_extracted_text_with_it():
    key = paper_with_pages()
    store.add_claim(key, {"text": "A.", "quote": "Recovery under steering"})
    assert store.text_path(key).exists()
    store.delete_paper(key)
    assert not store.text_path(key).exists()


# --- what the passage must not get wrong ----------------------------------

def test_a_sentence_running_over_a_page_break_is_kept_whole(tmp_path):
    pdf = tmp_path / "split.pdf"
    pdf.write_bytes(minimal_pdf([
        "We find that recovery under steering is a path-dependent",
        "outcome across all three model scales. The effect is smaller at 7B.",
    ]))
    found = quotes.locate(pdf, "recovery under steering is a path-dependent outcome")
    assert found["found"] is True
    # Cut at the page break, the suggestion would end at "path-dependent" and
    # taking the paper's wording would save half a sentence as the quote.
    assert found["suggestion"].endswith("across all three model scales.")
    assert found["page"] == 1


def test_a_quote_that_appears_twice_says_so_rather_than_naming_a_page(tmp_path):
    line = "Recovery under steering is a path-dependent outcome across all scales."
    pdf = tmp_path / "twice.pdf"
    pdf.write_bytes(minimal_pdf([f"First page. {line}", f"Second page. {line}"]))
    found = quotes.locate(pdf, line)
    assert (found["found"], found["repeated"], found["page"]) == (True, True, 1)
    # A quote in one place is not repeated, and its page can be believed.
    once = quotes.locate(tmp_path / "twice.pdf", "First page.")
    assert once["repeated"] is False


def test_a_replaced_pdf_takes_the_stored_text_with_it(tmp_path):
    key = "doe2026recovery"
    store.save_paper(store.new_paper(key, title="Recovery"))
    store.pdf_path(key).write_bytes(minimal_pdf(SENTENCE))
    claim = store.add_claim(key, {"text": "A.", "quote": SENTENCE})
    assert claim["quote_verified"] is True
    assert store.text_path(key).exists()

    # A PDF published by `os.replace` keeps the staged file's mtime, which can
    # be older than the text stored for the paper it replaces.
    staged = tmp_path / "staged.pdf"
    staged.write_bytes(minimal_pdf("A wholly different paper about penguins."))
    old = store.text_path(key).stat().st_mtime_ns - 5_000_000
    os.utime(staged, ns=(old, old))
    assert ingest.publish_pdf(key, staged) is True
    assert not store.text_path(key).exists()

    quotes._cache.clear()
    assert store.verify_quotes(key)["claims"][0]["quote_verified"] is False


def test_stored_text_as_old_as_its_pdf_is_not_trusted(tmp_path):
    pdf, cache = tmp_path / "p.pdf", tmp_path / "text" / "p.txt"
    pdf.write_bytes(minimal_pdf(SENTENCE))
    assert quotes.verify(pdf, SENTENCE, cache) is True
    cache.write_text("penguins", encoding="utf-8")
    stamp = pdf.stat().st_mtime_ns
    os.utime(cache, ns=(stamp, stamp))
    quotes._cache.clear()
    assert quotes.verify(pdf, SENTENCE, cache) is True    # read from the PDF again
