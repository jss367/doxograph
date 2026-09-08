"""Claims and papers as records: ids, proposals, ledger links, typed patches."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import export, extract, server, store


# --- proposed topics: accepting and discarding are different -------------

def paper_with_proposals() -> None:
    paper = store.new_paper("doe2026study", title="A Study")
    paper["proposed_tags"] = [
        {"name": "wanted", "description": "keep this one"},
        {"name": "unwanted", "description": "discard this one"},
    ]
    store.save_paper(paper)


def test_discarding_a_proposal_keeps_it_out_of_the_vocabulary():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"discard": ["unwanted"]}
        )
    assert response.status_code == 200
    assert response.json()["discarded"] == ["unwanted"]
    assert store.tag_names() == []
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["wanted"]


def test_accepting_a_proposal_adds_it_with_its_description():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"accept": ["wanted"]}
        )
    assert response.json()["accepted"] == ["wanted"]
    assert store.load_tags() == [{"name": "wanted", "description": "keep this one"}]
    assert [t["name"] for t in store.load_paper("doe2026study")["proposed_tags"]] == ["unwanted"]


def test_accept_and_discard_in_one_request():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.post(
            "/api/papers/doe2026study/proposed-tags",
            json={"accept": ["wanted"], "discard": ["unwanted"]},
        )
    assert store.tag_names() == ["wanted"]
    assert store.load_paper("doe2026study")["proposed_tags"] == []


def test_unknown_proposal_names_are_ignored():
    paper_with_proposals()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/api/papers/doe2026study/proposed-tags", json={"accept": ["never-proposed"]}
        )
    assert response.json()["accepted"] == []
    assert store.tag_names() == []


# --- a blank hand-added claim is a draft, not a reviewed claim ------------

def test_blank_claim_is_not_marked_reviewed():
    store.save_paper(store.new_paper("doe2026study"))
    draft = store.add_claim("doe2026study", {})
    assert draft["reviewed"] is False
    assert store.load_paper("doe2026study")["status"] == "extracted"


def test_claim_added_with_text_is_reviewed():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "A holds for B."})
    assert claim["reviewed"] is True


def test_add_claim_honors_an_explicit_reviewed_flag():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "A holds for B.", "reviewed": False})
    assert claim["reviewed"] is False
    assert store.load_paper("doe2026study")["status"] == "extracted"


def test_creating_a_claim_through_the_api_stores_the_whole_patch():
    store.save_paper(store.new_paper("doe2026study"))
    store.add_tag("alpha")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/papers/doe2026study/claims", json={
            "text": "A holds for B.", "kind": "method", "strength": "headline",
            "tags": ["alpha"], "evidence": "n = 10", "quote": "verbatim", "locator": "p. 1",
            "reviewed": True,
        })
    claim = response.json()
    assert (claim["text"], claim["kind"], claim["strength"]) == ("A holds for B.", "method", "headline")
    assert claim["tags"] == ["alpha"] and claim["reviewed"] is True
    assert len(store.load_paper("doe2026study")["claims"]) == 1


def test_summary_exposes_updated_so_the_client_can_version_its_cache():
    store.save_paper(store.new_paper("doe2026study"))
    summary = store.summarize(store.load_paper("doe2026study"))
    assert summary["updated"]

    store.add_claim("doe2026study", {"text": "A holds for B."})
    later = store.summarize(store.load_paper("doe2026study"))
    assert later["updated"] >= summary["updated"]


# --- ledger links must name a real claim ---------------------------------

def test_extraction_drops_a_link_to_a_claim_that_does_not_exist():
    store.save_ledger([{"id": "L1", "text": "A real claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "s", "relevance": "r", "proposed_tags": [],
        "claims": [{
            "text": "A finding.", "kind": "finding", "strength": "headline", "tags": [],
            "evidence": "", "quote": "", "locator": "",
            "ledger_links": [
                {"claim": "L1", "relation": "supports", "note": "real"},
                {"claim": "L9", "relation": "supports", "note": "invented"},
                {"claim": "", "relation": "supports", "note": "empty"},
            ],
        }],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    links = paper["claims"][0]["ledger_links"]
    assert [l["claim"] for l in links] == ["L1"]
    assert links[0]["note"] == "real"


def test_a_link_is_dropped_if_the_ledger_changed_during_the_call():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "A finding.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "",
                    "ledger_links": [{"claim": "L1", "relation": "supports", "note": "n"}]}],
    }
    store.save_ledger([{"id": "L2", "text": "Renumbered."}])   # while the model worked
    paper = extract.merge_extraction("doe2026study", payload)
    assert paper["claims"][0]["ledger_links"] == []


def test_the_api_cannot_attach_a_bogus_ledger_link():
    store.save_ledger([{"id": "L1", "text": "A claim."}])
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {
        "text": "X.",
        "ledger_links": [{"claim": "L1", "relation": "supports", "note": "ok"},
                         {"claim": "nope", "relation": "supports", "note": "bad"}],
    })
    assert [l["claim"] for l in claim["ledger_links"]] == ["L1"]

    updated = store.update_claim("doe2026study", claim["id"], {
        "ledger_links": [{"claim": "also-nope", "relation": "contradicts", "note": ""}]})
    assert updated["ledger_links"] == []


def test_a_link_with_no_ledger_at_all_is_dropped():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {
        "text": "X.", "ledger_links": [{"claim": "L1", "relation": "supports", "note": ""}]})
    assert claim["ledger_links"] == []


def test_a_deleted_claims_id_is_never_reused():
    """A recycled id lets an in-flight retag or PATCH land on the replacement."""
    store.save_paper(store.new_paper("doe2026study"))
    first = store.add_claim("doe2026study", {"text": "One."})
    second = store.add_claim("doe2026study", {"text": "Two."})
    assert (first["id"], second["id"]) == ("doe2026study-c1", "doe2026study-c2")

    store.delete_claim("doe2026study", second["id"])       # the highest one
    third = store.add_claim("doe2026study", {"text": "Three."})
    assert third["id"] == "doe2026study-c3", "the deleted id was recycled"


def test_ids_stay_unique_after_deleting_everything():
    store.save_paper(store.new_paper("doe2026study"))
    seen = set()
    for _ in range(5):
        claim = store.add_claim("doe2026study", {"text": "X."})
        seen.add(claim["id"])
        store.delete_claim("doe2026study", claim["id"])
    assert len(seen) == 5, seen
    assert store.load_paper("doe2026study")["claim_seq"] == 5


def test_the_counter_is_derived_for_a_corpus_written_before_it_existed():
    """An older paper file has claims but no `claim_seq`."""
    paper = store.new_paper("doe2026study")
    paper["claims"] = [
        {"id": "doe2026study-c1", "text": "One.", "tags": [], "ledger_links": []},
        {"id": "doe2026study-c7", "text": "Seven.", "tags": [], "ledger_links": []},
    ]
    del paper["claim_seq"]
    store.save_paper(paper)

    claim = store.add_claim("doe2026study", {"text": "Next."})
    assert claim["id"] == "doe2026study-c8", "the counter did not continue past the highest id"


def test_extraction_does_not_reuse_ids_across_runs():
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": f"Claim {n}.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}
                   for n in range(3)],
    }
    first = extract.merge_extraction("doe2026study", payload)
    first_ids = [c["id"] for c in first["claims"]]

    # Keep one, then re-extract: the fresh claims must not take retired ids.
    store.update_claim("doe2026study", first_ids[0], {"reviewed": True})
    second = extract.merge_extraction("doe2026study", payload)
    second_ids = [c["id"] for c in second["claims"]]

    assert len(set(second_ids)) == len(second_ids)
    reissued = (set(second_ids) - {first_ids[0]}) & set(first_ids)
    assert reissued == set(), f"ids were reissued: {reissued}"


def test_a_hand_written_claim_after_extraction_gets_a_fresh_id():
    store.save_paper(store.new_paper("doe2026study"))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "Extracted.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}],
    }
    extracted = extract.merge_extraction("doe2026study", payload)
    used = {c["id"] for c in extracted["claims"]}
    manual = store.add_claim("doe2026study", {"text": "By hand."})
    assert manual["id"] not in used


# --- the legacy claim sequence -------------------------------------------

def legacy_paper(reviewed_ids=(), unreviewed_ids=()):
    """A paper written before `claim_seq` existed."""
    paper = store.new_paper("doe2026study", title="A Study")
    del paper["claim_seq"]
    paper["claims"] = [
        {"id": cid, "text": f"Claim {cid}.", "kind": "finding", "strength": "aside",
         "tags": [], "evidence": "", "quote": "", "locator": "", "ledger_links": [],
         "reviewed": cid in reviewed_ids}
        for cid in list(reviewed_ids) + list(unreviewed_ids)
    ]
    store.save_paper(paper)
    return paper


def test_ensure_claim_seq_reads_every_claim():
    paper = legacy_paper(reviewed_ids=("doe2026study-c2",),
                         unreviewed_ids=("doe2026study-c9",))
    assert store.ensure_claim_seq(paper) == 9
    assert paper["claim_seq"] == 9


def test_re_extraction_does_not_reissue_an_unreviewed_legacy_id():
    """The highest id belonged to a claim the reviewed-only filter discards."""
    legacy_paper(reviewed_ids=("doe2026study-c2",),
                 unreviewed_ids=("doe2026study-c9",))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": "Fresh.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    fresh = [c for c in paper["claims"] if c["text"] == "Fresh."]
    assert len(fresh) == 1
    assert fresh[0]["id"] == "doe2026study-c10", (
        f"reissued a discarded claim's id: {fresh[0]['id']}")


def test_re_extraction_on_a_wholly_unreviewed_legacy_paper():
    """The real corpus shape: no claim_seq and nothing reviewed yet."""
    legacy_paper(unreviewed_ids=tuple(f"doe2026study-c{n}" for n in range(1, 17)))
    payload = {
        "summary": "", "relevance": "", "proposed_tags": [],
        "claims": [{"text": f"Fresh {n}.", "kind": "finding", "strength": "aside", "tags": [],
                    "evidence": "", "quote": "", "locator": "", "ledger_links": []}
                   for n in range(3)],
    }
    paper = extract.merge_extraction("doe2026study", payload)
    ids = [c["id"] for c in paper["claims"]]
    assert ids == ["doe2026study-c17", "doe2026study-c18", "doe2026study-c19"], ids


# --- a paper patch is typed, like every other write ----------------------

def test_a_paper_patch_refuses_a_year_that_is_not_a_number():
    """A string year sorted against every other paper's number and broke export."""
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"year": "not a year"}).status_code == 422
        assert client.patch("/api/papers/doe2026study", json={"authors": "Jane Roe"}).status_code == 422

    assert store.load_paper("doe2026study")["year"] == 2026
    export.render()   # still sortable


def test_a_paper_patch_leaves_the_fields_it_does_not_name_alone():
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026,
                                     venue="A Journal", notes="a note"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/doe2026study", json={"title": "A Better Study"})

    assert response.status_code == 200
    paper = store.load_paper("doe2026study")
    assert paper["title"] == "A Better Study"
    assert (paper["venue"], paper["notes"], paper["year"]) == ("A Journal", "a note", 2026)


def test_a_paper_patch_can_clear_a_year():
    """`None` is a value to write; only an absent field is left alone."""
    store.save_paper(store.new_paper("doe2026study", title="A Study", year=2026))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study", json={"year": None}).status_code == 200
    assert store.load_paper("doe2026study")["year"] is None


def test_a_paper_patch_ignores_a_field_that_is_not_the_users_to_set():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        client.patch("/api/papers/doe2026study", json={"title": "A Study", "claims": ["nonsense"]})
    assert store.load_paper("doe2026study")["claims"] == []


# --- a claim patch is typed, like a paper patch ---------------------------

def test_a_claim_patch_refuses_an_unknown_kind_or_strength():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "X."})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        path = f"/api/papers/doe2026study/claims/{claim['id']}"
        assert client.patch(path, json={"kind": "hunch"}).status_code == 422
        assert client.patch(path, json={"strength": "strong"}).status_code == 422
        assert client.patch(path, json={"reviewed": "yes please"}).status_code == 422
        assert client.post("/api/papers/doe2026study/claims", json={"text": 5}).status_code == 422
    assert store.load_paper("doe2026study")["claims"][0]["kind"] == "finding"


def test_a_claim_patch_applies_only_the_fields_it_names_and_cleans_tags():
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "evidence": "n = 3", "tags": ["alpha"]})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch(f"/api/papers/doe2026study/claims/{claim['id']}",
                                json={"tags": ["Beta Gamma", "alpha", " ", "alpha"]})
    updated = response.json()
    assert updated["tags"] == ["alpha", "beta-gamma"]
    assert updated["evidence"] == "n = 3" and updated["text"] == "X."


def test_a_claim_patch_refuses_an_unknown_ledger_relation_and_an_unknown_field():
    store.save_ledger([{"id": "L1", "text": "Mine."}])
    store.save_paper(store.new_paper("doe2026study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        bad = client.post("/api/papers/doe2026study/claims", json={
            "text": "X.", "ledger_links": [{"claim": "L1", "relation": "sort-of", "note": ""}]})
        assert bad.status_code == 422
        good = client.post("/api/papers/doe2026study/claims", json={
            "text": "X.", "id": "doe2026study-c99", "paper": "other",
            "ledger_links": [{"claim": "L1", "relation": "supports"}]})
    claim = good.json()
    assert claim["id"] == "doe2026study-c1"           # the id is not the caller's to set
    assert claim["ledger_links"] == [{"claim": "L1", "relation": "supports", "note": ""}]


def test_a_null_in_a_claim_patch_is_not_written():
    """`{"tags": null}` used to be stored as None and break tag_counts, and
    `{"text": null}` on creation reached `.strip()`. A null is not sent."""
    store.save_paper(store.new_paper("doe2026study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        updated = client.patch(f"/api/papers/doe2026study/claims/{claim['id']}",
                               json={"tags": None, "evidence": "n = 4"}).json()
        assert updated["tags"] == ["alpha"] and updated["evidence"] == "n = 4"
        created = client.post("/api/papers/doe2026study/claims", json={"text": None}).json()
        assert created["text"] == "" and created["reviewed"] is False
        assert client.get("/api/state").status_code == 200
