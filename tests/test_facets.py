"""Facets: questions the reader defines, asked of every claim."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from doxograph import config, extract, server, store


class FakeResponse:
    def __init__(self, payload: dict):
        block = type("Block", (), {"type": "text", "text": json.dumps(payload)})()
        self.content = [block]
        self.stop_reason = "end_turn"
        self.usage = None


class FakeMessages:
    def __init__(self, payload: dict, during_call=None):
        self.payload = payload
        self.during_call = during_call
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.during_call:
            self.during_call()
        return FakeResponse(self.payload)


class FakeClient:
    def __init__(self, payload: dict, during_call=None):
        self.messages = FakeMessages(payload, during_call)


def use_model(monkeypatch, payload: dict, during_call=None) -> FakeClient:
    fake = FakeClient(payload, during_call)
    monkeypatch.setattr(extract, "client", lambda: fake)
    return fake


@pytest.fixture
def corpus():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    first = store.add_claim("doe2026study", {"text": "Steering recovers on GPT-4.", "reviewed": False})
    second = store.add_claim("doe2026study", {"text": "Probes fail on Llama 3 8B.", "reviewed": False})
    return first["id"], second["id"]


def claims(key: str = "doe2026study") -> dict[str, dict]:
    return {c["id"]: c for c in store.load_paper(key)["claims"]}


# --- definitions ------------------------------------------------------------

def test_a_facet_is_stored_with_a_slug_name_and_its_description():
    store.add_facet("Model", "  The language   models evaluated ")
    assert store.load_facets() == [{"name": "model", "description": "The language models evaluated"}]
    assert yaml.safe_load(config.facets_path().read_text())["facets"][0]["name"] == "model"


def test_a_facet_name_cannot_be_used_twice():
    store.add_facet("model")
    with pytest.raises(ValueError):
        store.add_facet("Model")


def test_adding_a_facet_makes_it_stale_on_every_paper_with_claims(corpus):
    store.save_paper(store.new_paper("empty2026", title="No claims yet"))
    store.add_facet("model", "models")
    papers = store.all_papers()
    assert store.stale_facet_papers(papers, store.load_facets()) == {"model": 1}


def test_changing_a_description_stales_the_facet_again(monkeypatch, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})
    extract.fill_facets("doe2026study")
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == []

    store.describe_facet("model", "the models a result was measured on")
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == ["model"]


def test_deleting_a_facet_removes_its_values_from_every_claim(monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4"], "dataset": ["MMLU"]}},
        {"id": second, "facets": {"model": ["Llama 3 8B"], "dataset": []}},
    ]})
    extract.fill_facets("doe2026study")
    store.update_claim("doe2026study", second, {"facets": {"model": ["Llama 3 8B Instruct"]}})

    store.delete_facet("model")

    paper = store.load_paper("doe2026study")
    assert [f["name"] for f in store.load_facets()] == ["dataset"]
    assert {c["id"]: c.get("facets") for c in paper["claims"]} == {first: {"dataset": ["MMLU"]}, second: {}}
    assert "facets_hand" not in claims()[second] or claims()[second]["facets_hand"] == []
    assert "model" not in paper["facet_basis"]


# --- the fill pass ----------------------------------------------------------

def test_fill_sends_claims_and_records_values(monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "The language models evaluated")
    fake = use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4", " gpt-4 "]}},
        {"id": second, "facets": {"model": ["Llama 3 8B"]}},
    ]})

    extract.fill_facets("doe2026study")

    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert claims()[second]["facets"] == {"model": ["Llama 3 8B"]}
    sent = fake.messages.calls[0]
    assert "model: The language models evaluated" in sent["messages"][0]["content"]
    assert "Steering recovers on GPT-4." in sent["messages"][0]["content"]
    item = extract.facet_fill_schema(store.load_facets())["properties"]["assignments"]["items"]
    assert item["properties"]["facets"]["required"] == ["model"]


def test_fill_skips_a_paper_with_nothing_stale(monkeypatch, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})
    extract.fill_facets("doe2026study")

    fake = use_model(monkeypatch, {"assignments": []})
    extract.fill_facets("doe2026study")
    assert fake.messages.calls == []
    extract.fill_facets("doe2026study", force=True)
    assert len(fake.messages.calls) == 1


def test_fill_leaves_values_a_person_set(monkeypatch, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["GPT-4 (March)"]}})
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})

    extract.fill_facets("doe2026study", force=True)

    assert claims()[first]["facets"] == {"model": ["GPT-4 (March)"]}
    assert claims()[first]["facets_hand"] == ["model"]


def test_fill_does_not_overwrite_a_claim_edited_during_the_call(monkeypatch, corpus):
    first, second = corpus

    def concurrent_edit():
        store.update_claim("doe2026study", second, {"text": "Probes fail on Llama 3 70B."})

    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4"]}},
        {"id": second, "facets": {"model": ["Llama 3 8B"]}},
    ]}, concurrent_edit)

    extract.fill_facets("doe2026study")

    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert not claims()[second].get("facets")


def test_fill_drops_answers_for_a_facet_redescribed_during_the_call(monkeypatch, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]},
              lambda: store.describe_facet("model", "only open-weight models"))

    extract.fill_facets("doe2026study")

    assert not claims()[first].get("facets")
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == ["model"]


def test_fill_uses_the_spelling_already_in_the_corpus(monkeypatch, corpus):
    first, _ = corpus
    store.save_paper(store.new_paper("li2025other", title="Other"))
    store.add_claim("li2025other", {"text": "x", "facets": {}})
    store.add_facet("model", "models")
    other = store.add_claim("li2025other", {"text": "y", "facets": {"model": ["GPT-4o"]}})
    assert claims("li2025other")[other["id"]]["facets"] == {"model": ["GPT-4o"]}

    fake = use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["gpt-4O"]}}]})
    extract.fill_facets("doe2026study")

    assert claims()[first]["facets"] == {"model": ["GPT-4o"]}
    assert "In use: GPT-4o" in fake.messages.calls[0]["messages"][0]["content"]


# --- extraction ---------------------------------------------------------------

def test_extraction_without_facets_uses_the_plain_schema():
    assert extract.extraction_schema([]) is extract.EXTRACTION_SCHEMA


def test_extraction_asks_for_each_facet_on_each_claim():
    schema = extract.extraction_schema([{"name": "model", "description": "models"}])
    claim = schema["properties"]["claims"]["items"]
    assert claim["properties"]["facets"]["properties"]["model"]["description"] == "models"
    assert "facets" in claim["required"]
    # The shared schema is not changed in place.
    assert "facets" not in extract.CLAIM_SCHEMA["properties"]


def test_extraction_records_facet_values_and_marks_them_current():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_facet("model", "models")
    facets = store.load_facets()
    payload = {"summary": "s", "relevance": "r", "proposed_tags": [], "claims": [{
        "text": "Steering recovers.", "kind": "finding", "strength": "headline", "tags": [],
        "evidence": "", "quote": "", "locator": "", "ledger_links": [],
        "facets": {"model": ["GPT-4", "Unlisted"], "invented": ["x"]},
    }]}
    store.rewrite_facet_values("model", ["unlisted"], "")

    paper = extract.merge_extraction("doe2026study", payload, prompt_facets=facets, known_values={})

    assert paper["claims"][0]["facets"] == {"model": ["GPT-4"]}
    assert store.stale_facets(paper, store.load_facets()) == []


def test_extraction_drops_a_facet_deleted_during_the_call():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_facet("model", "models")
    facets = store.load_facets()
    store.delete_facet("model")
    payload = {"summary": "s", "relevance": "r", "proposed_tags": [], "claims": [{
        "text": "Steering recovers.", "kind": "finding", "strength": "headline", "tags": [],
        "evidence": "", "quote": "", "locator": "", "ledger_links": [],
        "facets": {"model": ["GPT-4"]},
    }]}

    paper = extract.merge_extraction("doe2026study", payload, prompt_facets=facets, known_values={})

    assert not paper["claims"][0].get("facets")
    assert "model" not in (paper.get("facet_basis") or {})


def test_a_reread_keeps_a_claim_whose_facets_were_corrected(corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["GPT-4"]}})
    payload = {"summary": "s", "relevance": "r", "proposed_tags": [], "claims": []}

    paper = extract.merge_extraction("doe2026study", payload, prompt_facets=store.load_facets(), known_values={})

    assert [c["id"] for c in paper["claims"]] == [first]


# --- values -------------------------------------------------------------------

def test_merging_values_rewrites_claims_and_remembers_the_alias(corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["gpt-4-0613"]}})
    store.update_claim("doe2026study", second, {"facets": {"model": ["GPT4", "Llama 3 8B"]}})

    store.rewrite_facet_values("model", ["gpt-4-0613", "GPT4"], "GPT-4")

    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert claims()[second]["facets"] == {"model": ["GPT-4", "Llama 3 8B"]}
    facet = store.load_facets()[0]
    assert facet["aliases"] == {"gpt-4-0613": "GPT-4", "gpt4": "GPT-4"}
    assert store.clean_facet_values(["GPT-4-0613"], facet) == ["GPT-4"]


def test_removing_a_value_drops_it_now_and_later(corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["none", "GPT-4"]}})

    store.rewrite_facet_values("model", ["none"], "")

    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert store.clean_facet_values(["None", "GPT-4"], store.load_facets()[0]) == ["GPT-4"]


def test_renaming_a_value_carries_older_aliases_along(corpus):
    store.add_facet("model", "models")
    store.rewrite_facet_values("model", ["gpt4"], "GPT-4")
    store.rewrite_facet_values("model", ["GPT-4"], "GPT-4 (2023)")
    facet = store.load_facets()[0]
    assert store.clean_facet_values(["gpt4"], facet) == ["GPT-4 (2023)"]
    assert store.clean_facet_values(["gpt-4"], facet) == ["GPT-4 (2023)"]


def test_counts_join_spellings_and_sort_commonest_first():
    rows = [{"facets": {"model": ["GPT-4"]}}, {"facets": {"model": ["gpt-4", "Llama"]}},
            {"facets": {"model": ["GPT-4"]}}, {"facets": {}}]
    assert store.facet_counts(rows, [{"name": "model"}]) == {"model": {"GPT-4": 3, "Llama": 1}}


# --- hand edits -----------------------------------------------------------------

def test_a_hand_edit_marks_only_the_facets_it_changed(corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    store.update_claim("doe2026study", first, {"facets": {"model": ["GPT-4"], "dataset": [], "nope": ["x"]}})
    claim = claims()[first]
    assert claim["facets"] == {"model": ["GPT-4"]}
    assert claim["facets_hand"] == ["model"]


def test_a_new_claim_by_hand_carries_its_facets():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_facet("model", "models")
    claim = store.add_claim("doe2026study", {"text": "Mine.", "facets": {"model": ["GPT-4"]}})
    assert claim["facets"] == {"model": ["GPT-4"]}
    assert claim["facets_hand"] == ["model"]


# --- the web API ----------------------------------------------------------------

@pytest.fixture
def api():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_the_api_defines_edits_and_deletes_facets(api, corpus):
    first, _ = corpus
    assert api.post("/api/facets", json={"name": "Model", "description": "models"}).status_code == 200
    assert api.post("/api/facets", json={"name": "model"}).status_code == 422
    assert api.patch("/api/facets/model", json={"description": "language models"}).json()["facets"][0][
        "description"] == "language models"
    assert api.patch(f"/api/papers/doe2026study/claims/{first}",
                     json={"facets": {"model": ["GPT-4"]}}).json()["facets"] == {"model": ["GPT-4"]}

    state = api.get("/api/state").json()
    assert state["facets"][0]["name"] == "model"
    assert state["facet_counts"] == {"model": {"GPT-4": 1}}
    assert state["facets_stale"] == {"model": 1}

    api.post("/api/facets/model/values", json={"values": ["GPT-4"], "into": "GPT-4o"})
    assert api.get("/api/state").json()["facet_counts"] == {"model": {"GPT-4o": 1}}

    assert api.delete("/api/facets/model").status_code == 200
    assert api.delete("/api/facets/model").status_code == 404
    assert api.get("/api/state").json()["facets"] == []


def test_the_fill_route_queues_only_stale_papers(api, monkeypatch, corpus):
    assert api.post("/api/facets/fill", json={}).json() == {"queued": 0}
    store.add_facet("model", "models")
    submitted = []
    monkeypatch.setattr(server, "_submit_job", lambda function, job, *args: submitted.append(args))
    assert api.post("/api/facets/fill", json={}).json() == {"queued": 1}
    assert submitted == [(["doe2026study"], False)]
