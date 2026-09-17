"""Claims that resemble each other, worked out from their words alone."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import pairs, server, store

RECOVERS = "Llama-3 70B recovers the original task in 46% of rollouts after steering."
RECOVERY_RATE = "Steered Llama-3 70B returns to the original task in roughly half of rollouts."
ELSEWHERE = "Sparse autoencoders trained on GPT-2 recover interpretable features."


def a_corpus() -> tuple[str, str, str]:
    ids = []
    for key, text in (("doe2026recovery", RECOVERS),
                      ("li2025steer", RECOVERY_RATE),
                      ("ng2026saes", ELSEWHERE)):
        store.save_paper(store.new_paper(key, title=key, authors=["A Name"], year=2026))
        ids.append(store.add_claim(key, {"text": text, "tags": ["recovery-rate"]})["id"])
    return tuple(ids)


def test_words_are_stemmed_and_the_dull_ones_dropped():
    assert pairs.stem("scales") == pairs.stem("scale")
    assert pairs.stem("steering") == pairs.stem("steer")
    assert pairs.stem("recovered") == pairs.stem("recovers")
    assert pairs.stem("recoveries") == "recovery"
    found = pairs.words({"text": "The models recover.", "evidence": "Llama-3 70B"})
    assert "the" not in found and "llama" in found and "70b" in found
    assert "model" in found and pairs.words({"text": "one model"}) >= {"model"}


def test_a_word_in_every_claim_counts_for_nothing():
    rows = [{"id": "a", "text": "steering changes recovery"},
            {"id": "b", "text": "steering changes nothing"}]
    weight = pairs.weights(rows)
    assert weight["steer"] < weight["recovery"]


def test_the_claims_that_resemble_one_come_back_best_first():
    a, b, c = a_corpus()
    rows = store.claim_rows()
    found = pairs.similar_to(a, rows)
    assert [hit["claim"] for hit in found][:1] == [b]
    assert all(hit["score"] > 0 for hit in found)
    # A claim is never compared with its own paper's claims.
    assert all(not hit["claim"].startswith("doe2026recovery") for hit in found)


def test_a_claim_with_nothing_like_it_gets_an_empty_answer():
    store.save_paper(store.new_paper("solo2026", title="Alone"))
    only = store.add_claim("solo2026", {"text": "Penguins huddle to conserve warmth."})
    assert pairs.similar_to(only["id"], store.claim_rows()) == []


def test_the_similar_route_names_the_papers_and_404s_for_a_stranger():
    a, b, _ = a_corpus()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        found = client.get(f"/api/papers/doe2026recovery/claims/{a}/similar").json()
        assert [hit["claim"] for hit in found["similar"]][:1] == [b]
        assert found["similar"][0]["paper"] == "li2025steer"
        assert found["similar"][0]["text"] == RECOVERY_RATE
        # The claim has to be on the paper it is asked for.
        assert client.get(f"/api/papers/ng2026saes/claims/{a}/similar").status_code == 404
        assert client.get("/api/papers/doe2026recovery/claims/nope/similar").status_code == 404


def test_a_verb_and_its_past_tense_meet():
    for word, other in (("scaled", "scale"), ("scales", "scaled"), ("refined", "refines"),
                        ("recovered", "recovers"), ("steering", "steer"),
                        ("studied", "studies"), ("applied", "applies"),
                        ("controlled", "controls"), ("mapped", "maps"),
                        ("planned", "plans"), ("added", "adds"),
                        ("analysis", "analyses"), ("hypothesis", "hypotheses"),
                        ("diagnosis", "diagnoses"),
                        # And the plurals the -sis rule must not swallow.
                        ("phase", "phases"), ("dose", "doses")):
        assert pairs.stem(word) == pairs.stem(other), (word, other)
