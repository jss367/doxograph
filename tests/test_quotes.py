"""A claim's quote is checked against the paper's own text."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from doxograph import __main__, extract, quotes, server, store

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
