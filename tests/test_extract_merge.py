from doxograph import extract, store

PAYLOAD = {
    "summary": "The paper measures recovery under steering.",
    "relevance": "Directly about the endpoint I am measuring.",
    "claims": [
        {
            "text": "Llama-3 70B returns to the task in 46% of steered rollouts.",
            "kind": "finding", "strength": "headline",
            "tags": ["recovery-rate", "New Tag"],
            "evidence": "500 rollouts, 128-token window.",
            "quote": "We observe recovery in 46% of rollouts.",
            "locator": "Table 2",
            "ledger_links": [{"claim": "L1", "relation": "supports", "note": "Same endpoint."}],
        },
        {"text": "", "kind": "finding", "strength": "aside", "tags": [], "evidence": "",
         "quote": "", "locator": "", "ledger_links": []},
    ],
    "proposed_tags": [{"name": "recovery-rate", "description": "How often a model returns to task."}],
}


def setup_paper():
    store.save_paper(store.new_paper("doe2026recovery", title="Recovery under steering"))


def test_merge_records_claims_and_drops_empty_ones():
    setup_paper()
    paper = extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    assert len(paper["claims"]) == 1
    claim = paper["claims"][0]
    assert claim["tags"] == ["new-tag", "recovery-rate"]  # slugified and sorted
    assert claim["reviewed"] is False
    assert paper["summary"].startswith("The paper measures")
    assert paper["status"] == "extracted"


def test_undeclared_tags_become_proposals():
    setup_paper()
    paper = extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    names = {t["name"] for t in paper["proposed_tags"]}
    assert names == {"recovery-rate", "new-tag"}


def test_declared_tags_are_not_proposed_again():
    setup_paper()
    store.add_tag("recovery-rate", "How often a model returns to task.")
    paper = extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    assert [t["name"] for t in paper["proposed_tags"]] == ["new-tag"]


def test_reextraction_keeps_reviewed_claims_by_default():
    setup_paper()
    extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    key = store.load_paper("doe2026recovery")["claims"][0]["id"]
    store.update_claim("doe2026recovery", key, {"reviewed": True, "text": "Hand-corrected."})

    paper = extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    texts = [c["text"] for c in paper["claims"]]
    assert "Hand-corrected." in texts
    assert len(paper["claims"]) == 2  # the kept one plus a fresh extraction


def test_replace_reviewed_discards_them():
    setup_paper()
    extract.merge_extraction("doe2026recovery", dict(PAYLOAD))
    key = store.load_paper("doe2026recovery")["claims"][0]["id"]
    store.update_claim("doe2026recovery", key, {"reviewed": True, "text": "Hand-corrected."})

    paper = extract.merge_extraction("doe2026recovery", dict(PAYLOAD), keep_reviewed=False)
    assert [c["text"] for c in paper["claims"]] == [PAYLOAD["claims"][0]["text"]]
