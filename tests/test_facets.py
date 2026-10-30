"""Facets: questions the reader defines, asked of every claim."""

from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

from doxograph import config, extract, job_queue, server, store


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
    first, second = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}},
                                            {"id": second, "facets": {"model": []}}]})
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
    assert all("model" not in (c.get("facet_basis") or {}) for c in paper["claims"])


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
    first, second = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}},
                                            {"id": second, "facets": {"model": []}}]})
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
    # The skipped claim keeps the paper stale, and a normal retry reaches it
    # alone: the claim already filled is not asked about again.
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == ["model"]
    retry = use_model(monkeypatch, {"assignments": [{"id": second, "facets": {"model": ["Llama 3 70B"]}}]})
    extract.fill_facets("doe2026study")
    assert claims()[second]["facets"] == {"model": ["Llama 3 70B"]}
    assert first not in retry.messages.calls[0]["messages"][0]["content"]
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == []


def test_a_claim_the_answer_leaves_out_stays_to_be_filled(monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})
    extract.fill_facets("doe2026study")
    paper = store.load_paper("doe2026study")
    assert store.stale_facets(paper, store.load_facets()) == ["model"]
    assert store.stale_facet_papers([paper], store.load_facets()) == {"model": 1}


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
    assert "model" not in (paper["claims"][0].get("facet_basis") or {})


def test_a_reread_leaves_kept_claims_to_be_filled(monkeypatch, corpus):
    """A re-read gives values only to the claims it writes. A reviewed claim
    it keeps was never asked about the new facet, so the paper stays stale
    and a normal fill reaches that claim."""
    first, second = corpus
    store.update_claim("doe2026study", first, {"reviewed": True})
    store.add_facet("model", "models")
    payload = {"summary": "s", "relevance": "r", "proposed_tags": [], "claims": [{
        "text": "A fresh claim on Llama.", "kind": "finding", "strength": "headline", "tags": [],
        "evidence": "", "quote": "", "locator": "", "ledger_links": [], "facets": {"model": ["Llama"]},
    }]}
    paper = extract.merge_extraction("doe2026study", payload, prompt_facets=store.load_facets(), known_values={})
    fresh = next(c["id"] for c in paper["claims"] if c["id"] != first)
    assert store.stale_facets(paper, store.load_facets()) == ["model"]

    fake = use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})
    extract.fill_facets("doe2026study")
    content = fake.messages.calls[0]["messages"][0]["content"]
    assert first in content and fresh not in content
    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert claims()[fresh]["facets"] == {"model": ["Llama"]}
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == []


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
    monkeypatch.setattr(job_queue, "_submit_job", lambda function, job, *args: submitted.append(args))
    assert api.post("/api/facets/fill", json={}).json() == {"queued": 1}
    assert submitted == [(["doe2026study"], False)]


# --- history, backups, archives ---------------------------------------------------

def test_a_restored_claim_keeps_its_hand_set_facets_through_a_later_fill(api, monkeypatch, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["GPT-4 (March)"]}})
    store.delete_claim("doe2026study", first)
    history = api.get("/api/notebook/history/doe2026study").json()
    entry = next(e for e in history["entries"] if e["claim"] == first and e["fields"] is not None)
    restored = api.post("/api/notebook/history/doe2026study/restore",
                        json={"revision": entry["id"], "expected": history["expected"]})
    assert restored.status_code == 200, restored.text
    assert claims()[first]["facets_hand"] == ["model"]

    store.describe_facet("model", "the models a result was measured on")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}}]})
    extract.fill_facets("doe2026study", force=True)
    assert claims()[first]["facets"] == {"model": ["GPT-4 (March)"]}


def test_restoring_history_reconciles_facets_changed_since(api, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    store.update_claim("doe2026study", first, {"facets": {"model": ["gpt4"], "dataset": ["MMLU"]}})
    store.update_claim("doe2026study", first, {"facets": {"model": [], "dataset": []}})
    store.rewrite_facet_values("model", ["gpt4"], "GPT-4")
    store.delete_facet("dataset")
    history = api.get("/api/notebook/history/doe2026study").json()
    entry = next(e for e in history["entries"]
                 if e["claim"] == first and (e["fields"] or {}).get("facets", {}).get("dataset"))
    api.post("/api/notebook/history/doe2026study/restore",
             json={"revision": entry["id"], "expected": history["expected"]}).raise_for_status()
    claim = claims()[first]
    assert claim["facets"] == {"model": ["GPT-4"]}
    assert claim["facets_hand"] == ["model"]


def test_restoring_a_paper_backup_reconciles_its_facets(api, corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    store.update_claim("doe2026study", first, {"facets": {"model": ["gpt4", "none"], "dataset": ["MMLU"]}})
    store.update_claim("doe2026study", first, {"text": "Edited after the backup."})
    store.rewrite_facet_values("model", ["gpt4"], "GPT-4")
    store.rewrite_facet_values("model", ["none"], "")
    store.delete_facet("dataset")
    versions = [v for v in api.get("/api/workspace/backups").json()["versions"]
                if v["path"] == "papers/doe2026study.json"]
    def claim_in(version: dict) -> dict:
        saved = json.loads(api.get(f"/api/workspace/backups/{version['id']}").content)
        # The earliest backups predate the claim.
        return next((c for c in saved["claims"] if c["id"] == first), {})

    version = next(v for v in versions if claim_in(v).get("text") == "Steering recovers on GPT-4."
                   and "dataset" in claim_in(v).get("facets", {}))
    response = api.post(f"/api/workspace/backups/{version['id']}/restore", json={"expected": version["expected"]})
    assert response.status_code == 200, response.text
    claim = claims()[first]
    assert claim["text"] == "Steering recovers on GPT-4."
    assert claim["facets"] == {"model": ["GPT-4"]}
    assert claim["facets_hand"] == ["model"]


def test_an_archive_waits_out_a_facet_rewrite_instead_of_capturing_half_of_it(monkeypatch, corpus):
    import threading
    import zipfile

    from doxograph import workspace_archive

    first, _ = corpus
    store.save_paper(store.new_paper("li2025other", title="Other"))
    store.add_facet("model", "models")
    other = store.add_claim("li2025other", {"text": "y", "facets": {"model": ["gpt4"]}})["id"]
    store.update_claim("doe2026study", first, {"facets": {"model": ["gpt4"]}})

    halfway, release = threading.Event(), threading.Event()
    original = store.save_paper

    def pause_after_the_first_paper(paper):
        original(paper)
        if not halfway.is_set():
            halfway.set()
            assert release.wait(5)

    monkeypatch.setattr(store, "save_paper", pause_after_the_first_paper)
    rewrite = threading.Thread(target=store.rewrite_facet_values, args=("model", ["gpt4"], "GPT-4"))
    rewrite.start()
    assert halfway.wait(5)
    built: list = []
    archive = threading.Thread(target=lambda: built.append(workspace_archive.build()))
    archive.start()
    archive.join(0.3)
    assert archive.is_alive(), "the archive did not wait for the facet rewrite"
    release.set()
    rewrite.join(5)
    archive.join(10)
    monkeypatch.setattr(store, "save_paper", original)
    path = built[0]
    try:
        with zipfile.ZipFile(path) as zipped:
            facets = yaml.safe_load(zipped.read("data/facets.yaml"))
            assert facets["facets"][0]["aliases"] == {"gpt4": "GPT-4"}
            for key, claim_id in (("doe2026study", first), ("li2025other", other)):
                paper = json.loads(zipped.read(f"data/papers/{key}.json"))
                assert next(c for c in paper["claims"] if c["id"] == claim_id)["facets"] == {"model": ["GPT-4"]}
    finally:
        path.unlink()


# --- interrupted facet changes ----------------------------------------------------

def _two_papers_with(value: str) -> tuple[str, str]:
    store.save_paper(store.new_paper("a2026", title="A"))
    store.save_paper(store.new_paper("b2026", title="B"))
    store.add_facet("model", "models")
    first = store.add_claim("a2026", {"text": "x", "facets": {"model": [value]}})["id"]
    second = store.add_claim("b2026", {"text": "y", "facets": {"model": [value]}})["id"]
    return first, second


def _fail_once(monkeypatch, module, name: str, after: int = 0):
    """Make `module.name` raise on its call after `after` successful ones, once."""
    original = getattr(module, name)
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == after + 1:
            raise OSError("disk went away")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, flaky)
    return original


@pytest.mark.parametrize("failing, after", [("save_paper", 1), ("_save_facets", 0)])
def test_an_interrupted_value_rename_finishes_on_the_next_lock(monkeypatch, failing, after):
    first, second = _two_papers_with("gpt4")
    original = _fail_once(monkeypatch, store, failing, after)
    with pytest.raises(store.StorageRecoveryError):
        store.rewrite_facet_values("model", ["gpt4"], "GPT-4")
    assert store.facet_change_path().exists()
    monkeypatch.setattr(store, failing, original)

    store.recover_storage()

    assert not store.facet_change_path().exists()
    assert claims("a2026")[first]["facets"] == {"model": ["GPT-4"]}
    assert claims("b2026")[second]["facets"] == {"model": ["GPT-4"]}
    assert store.clean_facet_values(["gpt4"], store.load_facets()[0]) == ["GPT-4"]


def test_an_interrupted_removal_does_not_let_the_value_come_back(monkeypatch):
    first, second = _two_papers_with("none")
    original = _fail_once(monkeypatch, store, "_save_facets")
    with pytest.raises(store.StorageRecoveryError):
        store.rewrite_facet_values("model", ["none"], "")
    monkeypatch.setattr(store, "_save_facets", original)

    # Any later facet edit takes the lock and finishes the change first.
    store.update_claim("a2026", first, {"facets": {"model": ["None", "GPT-4"]}})

    assert claims("a2026")[first]["facets"] == {"model": ["GPT-4"]}
    assert not claims("b2026")[second].get("facets")
    assert not store.facet_change_path().exists()


def test_an_interrupted_delete_finishes_and_blocks_archives_until_then(monkeypatch, api):
    from fastapi import HTTPException

    from doxograph import workspace_archive

    first, second = _two_papers_with("GPT-4")
    original = _fail_once(monkeypatch, store, "save_paper", 1)
    with pytest.raises(store.StorageRecoveryError):
        store.delete_facet("model")
    monkeypatch.setattr(store, "save_paper", original)
    with pytest.raises(HTTPException) as refused:
        workspace_archive.build()
    assert refused.value.status_code == 409

    state = api.get("/api/state").json()   # the state read runs recovery

    assert state["facets"] == []
    assert not claims("a2026")[first].get("facets") and not claims("b2026")[second].get("facets")
    assert not store.facet_change_path().exists()


def test_a_recovery_that_cannot_finish_is_reported_with_its_intent_file(monkeypatch, api):
    _two_papers_with("gpt4")
    store.write_json(store.facet_change_path(), {"version": 1, "kind": "bogus"})
    state = api.get("/api/state").json()
    assert any(issue["path"] == str(store.facet_change_path()) for issue in state["storage_issues"])


def test_a_fill_leaves_a_claims_current_facets_alone(monkeypatch, corpus):
    """Claim one needs `model`, claim two needs `dataset`: both are sent, and
    each takes only the facet it needed from the answer."""
    first, second = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    paper = store.load_paper("doe2026study")
    model, dataset = store.load_facets()
    for claim in paper["claims"]:
        if claim["id"] == first:
            claim["facets"] = {"dataset": ["MMLU"]}
            store.mark_facets_filled(claim, [dataset])
        else:
            claim["facets"] = {"model": ["Llama 3 8B"]}
            store.mark_facets_filled(claim, [model])
    store.save_paper(paper)
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4"], "dataset": []}},
        {"id": second, "facets": {"model": [], "dataset": ["GSM8K"]}},
    ]})

    extract.fill_facets("doe2026study")

    assert claims()[first]["facets"] == {"dataset": ["MMLU"], "model": ["GPT-4"]}
    assert claims()[second]["facets"] == {"dataset": ["GSM8K"], "model": ["Llama 3 8B"]}
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == []


def test_restoring_an_entry_from_before_facets_keeps_the_claim_filled(api, monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "models")
    use_model(monkeypatch, {"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}},
                                            {"id": second, "facets": {"model": []}}]})
    extract.fill_facets("doe2026study")
    paper = store.load_paper("doe2026study")
    paper["history"] = [*paper.get("history", []), {
        "id": "legacy", "at": store.now(), "claim": first,
        "fields": {"text": "An older wording.", "kind": "finding", "strength": "supporting", "tags": [],
                   "evidence": "", "quote": "", "locator": "", "ledger_links": [], "note": "",
                   "reviewed": False}}]
    store.write_json(store.paper_path("doe2026study"), paper)
    history = api.get("/api/notebook/history/doe2026study").json()
    api.post("/api/notebook/history/doe2026study/restore",
             json={"revision": "legacy", "expected": history["expected"]}).raise_for_status()

    assert claims()[first]["text"] == "An older wording."
    assert claims()[first]["facets"] == {"model": ["GPT-4"]}
    assert store.stale_facets(store.load_paper("doe2026study"), store.load_facets()) == []


def test_a_comma_inside_a_value_survives_an_unrelated_edit(corpus):
    """The editor splits on commas, so no stored value holds one: a value read
    as "Llama 2, 7B" is stored as one value and comes back from the editor's
    comma-separated field unchanged."""
    first, _ = corpus
    store.add_facet("model", "models")
    facet = store.load_facets()[0]
    assert store.clean_facet_values(["Llama 2, 7B", "llama 2 7b"], facet) == ["Llama 2 7B"]
    paper = store.load_paper("doe2026study")
    claim = next(c for c in paper["claims"] if c["id"] == first)
    claim["facets"] = store.clean_claim_facets({"model": ["Llama 2, 7B", "GPT-4"]}, [facet])
    store.save_paper(paper)

    # What the editor sends back for an untouched field: the values joined
    # with ", " and split on commas again.
    shown = ", ".join(claims()[first]["facets"]["model"])
    sent = [v.strip() for v in shown.split(",") if v.strip()]
    store.update_claim("doe2026study", first, {"text": "Reworded.", "facets": {"model": sent}})

    assert claims()[first]["facets"] == {"model": ["GPT-4", "Llama 2 7B"]}
    assert "facets_hand" not in claims()[first]
    store.rewrite_facet_values("model", ["Llama 2 7B"], "Llama 2, 13B")
    assert claims()[first]["facets"] == {"model": ["GPT-4", "Llama 2 13B"]}


def test_a_hand_edit_leaves_the_facets_it_does_not_name(monkeypatch, corpus):
    """A form drawn before a fill landed sends only the facet the person
    changed; the fill's values for the others stand and stay the pass's."""
    first, second = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4"], "dataset": ["MMLU"]}},
        {"id": second, "facets": {"model": [], "dataset": []}}]})
    extract.fill_facets("doe2026study")

    store.update_claim("doe2026study", first, {"text": "Reworded.", "facets": {"dataset": ["GSM8K"]}})

    claim = claims()[first]
    assert claim["facets"] == {"dataset": ["GSM8K"], "model": ["GPT-4"]}
    assert claim["facets_hand"] == ["dataset"]
    store.update_claim("doe2026study", first, {"facets": {"dataset": []}})
    assert claims()[first]["facets"] == {"model": ["GPT-4"]}


def test_a_fill_landing_during_a_reread_does_not_duplicate_claims(monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4")
    fill = FakeClient({"assignments": [{"id": first, "facets": {"model": ["GPT-4"]}},
                                       {"id": second, "facets": {"model": ["Llama"]}}]})

    def fill_meanwhile():
        monkeypatch.setattr(extract, "client", lambda: fill)
        extract.fill_facets("doe2026study")

    reread = FakeClient({"summary": "s", "relevance": "r", "proposed_tags": [], "claims": [{
        "text": "A fresh claim.", "kind": "finding", "strength": "headline", "tags": [],
        "evidence": "", "quote": "", "locator": "", "ledger_links": [], "facets": {"model": ["GPT-4"]},
    }]}, fill_meanwhile)
    monkeypatch.setattr(extract, "client", lambda: reread)
    monkeypatch.setattr(extract, "paper_document", lambda key, api: {"type": "text", "text": "paper"})

    paper = extract.extract_paper("doe2026study")

    assert [c["text"] for c in paper["claims"]] == ["A fresh claim."]


def test_restoring_one_facet_keeps_the_others_filled(api, monkeypatch, corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.add_facet("dataset", "datasets")
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"model": ["GPT-4"], "dataset": ["MMLU"]}},
        {"id": second, "facets": {"model": [], "dataset": []}}]})
    extract.fill_facets("doe2026study")
    use_model(monkeypatch, {"assignments": [
        {"id": first, "facets": {"dataset": ["GSM8K"]}}, {"id": second, "facets": {"dataset": []}}]})
    store.describe_facet("dataset", "the benchmarks used")
    extract.fill_facets("doe2026study")
    history = api.get("/api/notebook/history/doe2026study").json()
    entry = next(e for e in history["entries"]
                 if e["claim"] == first and (e["fields"] or {}).get("facets", {}).get("dataset") == ["MMLU"])
    api.post("/api/notebook/history/doe2026study/restore",
             json={"revision": entry["id"], "expected": history["expected"]}).raise_for_status()

    claim = claims()[first]
    assert claim["facets"] == {"dataset": ["MMLU"], "model": ["GPT-4"]}
    model, dataset = store.load_facets()
    assert not store.claim_needs_facet(claim, model)
    assert store.claim_needs_facet(claim, dataset)


@pytest.mark.parametrize("name", ["model:size", "Model", "model size", "-model", ""])
def test_a_hand_edited_facet_name_must_be_a_slug(api, name):
    from doxograph import storage

    config.facets_path().write_text(yaml.safe_dump({"facets": [{"name": name, "description": "x"}]}))
    with pytest.raises(storage.StorageReadError):
        store.load_facets()
    state = api.get("/api/state").json()
    assert state["facets"] == []
    assert any(issue["path"] == str(config.facets_path()) for issue in state["storage_issues"])


def test_slugged_names_are_valid_facet_names():
    for name in ["model", "training-method", "gpt4", "a-b-c"]:
        assert store.slugify(name) == name
    store.add_facet("Training Method: Kind")
    assert store.load_facets()[0]["name"] == "training-method-kind"


def test_merging_into_a_renamed_spelling_merges_into_what_it_became(corpus):
    first, second = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["A"]}})
    store.update_claim("doe2026study", second, {"facets": {"model": ["C"]}})
    store.rewrite_facet_values("model", ["A"], "B")
    store.rewrite_facet_values("model", ["C"], "A")

    assert claims()[first]["facets"] == {"model": ["B"]}
    assert claims()[second]["facets"] == {"model": ["B"]}
    facet = store.load_facets()[0]
    assert store.clean_facet_values(["a", "c", "b"], facet) == ["B"]
    assert all(target == "B" for target in facet["aliases"].values())


def test_a_rename_can_be_undone(corpus):
    first, _ = corpus
    store.add_facet("model", "models")
    store.update_claim("doe2026study", first, {"facets": {"model": ["A"]}})
    store.rewrite_facet_values("model", ["A"], "B")
    store.rewrite_facet_values("model", ["B"], "A")

    assert claims()[first]["facets"] == {"model": ["A"]}
    facet = store.load_facets()[0]
    # "b" now reads as A, and "a" is A in another case: one value.
    assert [store.facet_value_key(v) for v in store.clean_facet_values(["a", "b"], facet)] == ["a"]


def test_hand_edited_aliases_are_read_as_stored_values(corpus):
    config.facets_path().write_text(yaml.safe_dump({"facets": [{
        "name": "model", "description": "models",
        "aliases": {"LLAMA2-7B": "Llama 2, 7B", "x": "y", "y": "x", "old": "LLAMA2-7B"},
    }]}))
    facet = store.load_facets()[0]
    assert store.clean_facet_values(["llama2-7b"], facet) == ["Llama 2 7B"]
    assert store.clean_facet_values(["old"], facet) == ["Llama 2 7B"]
    assert len(store.clean_facet_values(["x"], facet)) == 1   # a cycle ends rather than loops
