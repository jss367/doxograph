"""Retagging reassigns topics without undoing anything a person did meanwhile."""

from __future__ import annotations

import json

import pytest

from doxograph import extract, server, store


# --- retag must not overwrite edits made while the model was working -----

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

    def create(self, **kwargs):
        if self.during_call:
            self.during_call()  # stands in for a concurrent edit while we wait
        return FakeResponse(self.payload)


class FakeClient:
    def __init__(self, payload: dict, during_call=None):
        self.messages = FakeMessages(payload, during_call)


@pytest.fixture
def retag_corpus():
    store.add_tag("alpha")
    store.add_tag("beta")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    first = store.add_claim("doe2026study", {"text": "First claim.", "tags": ["alpha"]})
    second = store.add_claim("doe2026study", {"text": "Second claim.", "tags": ["alpha"]})
    return first["id"], second["id"]


def test_retag_preserves_an_edit_made_during_the_model_call(monkeypatch, retag_corpus):
    """A claim rewritten during the call keeps its text and its old tags.

    The answer for it describes wording that no longer exists, so it is not
    applied. Every other claim still takes the answer.
    """
    first_id, second_id = retag_corpus

    def concurrent_edit():
        store.update_claim("doe2026study", second_id, {"text": "Corrected by hand.", "reviewed": True})

    payload = {"assignments": [
        {"id": first_id, "tags": ["beta"]},
        {"id": second_id, "tags": ["beta"]},
    ]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload, concurrent_edit))

    extract.retag_paper("doe2026study")

    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[second_id]["text"] == "Corrected by hand."   # the edit survived
    assert claims[second_id]["reviewed"] is True
    assert claims[second_id]["tags"] == ["alpha"]              # tags for the old text
    assert claims[first_id]["tags"] == ["beta"]                # untouched claim retagged


def test_retag_drops_tags_absent_from_the_vocabulary(monkeypatch, retag_corpus):
    first_id, _ = retag_corpus
    payload = {"assignments": [{"id": first_id, "tags": ["beta", "invented"]}]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload))

    extract.retag_paper("doe2026study")
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[first_id]["tags"] == ["beta"]


def test_retag_leaves_claims_the_model_did_not_mention(monkeypatch, retag_corpus):
    first_id, second_id = retag_corpus
    payload = {"assignments": [{"id": first_id, "tags": ["beta"]}]}
    monkeypatch.setattr(extract, "client", lambda: FakeClient(payload))

    extract.retag_paper("doe2026study")
    claims = {c["id"]: c for c in store.load_paper("doe2026study")["claims"]}
    assert claims[second_id]["tags"] == ["alpha"]


def test_retag_reads_the_vocabulary_while_holding_its_lock(monkeypatch):
    """The invariant: `known` is read under the vocabulary lock, not before it.

    Asserting the interleaving directly needs the deletion to land in the gap
    between the snapshot and the save, which is not reproducible from outside.
    So this asserts the property that closes the gap instead: at the moment
    retag reads the vocabulary, this thread owns the vocabulary lock.
    """
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})

    held = []
    real_tag_names = store.tag_names

    def watched_tag_names():
        held.append(store._vocab._is_owned())
        return real_tag_names()

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"assignments": [{"id": claim["id"], "tags": ["alpha"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(store, "tag_names", watched_tag_names)
    extract.retag_paper("doe2026study")

    # The prompt-building read happens before the lock; the read that decides
    # what gets written must happen under it.
    assert held, "the vocabulary was never read"
    assert held[-1] is True, "retag applied tags using a vocabulary read outside the lock"


def test_retag_drops_a_tag_that_is_no_longer_in_the_vocabulary(monkeypatch):
    """Whatever the interleaving, an undeclared tag must never be written."""
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study", {"text": "X.", "tags": ["alpha"]})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"assignments": [{"id": claim["id"], "tags": ["alpha", "never-declared"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    extract.retag_paper("doe2026study")
    assert store.load_paper("doe2026study")["claims"][0]["tags"] == ["alpha"]


def test_a_rename_during_a_retag_is_not_undone(monkeypatch):
    """The rename rewrites the claim; the stale assignment must not revert it."""
    store.add_tag("alpha")
    store.add_tag("old-name")
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claim_seq"] = 1
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": ["alpha", "old-name"], "evidence": "",
                        "quote": "", "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            # The rename lands while the model is thinking.
            store.rename_tag("old-name", "new-name")
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha", "old-name"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha", "new-name"], "the rename was undone"


def test_a_tag_deleted_during_a_retag_stays_off(monkeypatch):
    """The other half of the same rule: a vanished tag is not written back."""
    store.add_tag("alpha")
    store.add_tag("doomed")
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": ["alpha", "doomed"], "evidence": "",
                        "quote": "", "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.delete_tag("doomed")
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha", "doomed"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    result = extract.retag_paper("doe2026study")
    assert result["claims"][0]["tags"] == ["alpha"]


def retag_client(monkeypatch, during_the_call):
    """A retag call that runs `during_the_call` before answering."""
    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            during_the_call()
            payload = {"assignments": [{"id": "doe2026study-c1", "tags": ["alpha"]}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())


def study_with_tags(tags):
    paper = store.new_paper("doe2026study", title="A Study")
    paper["claims"] = [{"id": "doe2026study-c1", "text": "A finding.", "kind": "finding",
                        "strength": "aside", "tags": list(tags), "evidence": "", "quote": "",
                        "locator": "", "ledger_links": [], "reviewed": False}]
    store.save_paper(paper)


def set_claim_tags(tags):
    paper = store.load_paper("doe2026study")
    paper["claims"][0]["tags"] = sorted(tags)
    store.save_paper(paper)


def test_a_tag_a_person_adds_during_a_retag_is_kept(monkeypatch):
    """The added tag is in the vocabulary the model saw, and still survives."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["alpha"])

    retag_client(monkeypatch, lambda: set_claim_tags(["alpha", "beta"]))
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha", "beta"], "a person's edit was reversed"


def test_a_tag_a_person_removes_during_a_retag_stays_off(monkeypatch):
    """The other direction: the model's answer must not put it back."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["alpha", "beta"])

    retag_client(monkeypatch, lambda: set_claim_tags(["beta"]))
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["beta"], "a removed tag was restored"


def test_an_untouched_claim_still_takes_the_models_answer(monkeypatch):
    """The rule only stands down for claims somebody else changed."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["beta"])

    retag_client(monkeypatch, lambda: None)
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["tags"] == ["alpha"]


def test_a_claim_rewritten_during_a_retag_keeps_its_tags(monkeypatch):
    """The answer describes wording the claim no longer has."""
    store.add_tag("alpha")
    store.add_tag("beta")
    study_with_tags(["beta"])

    def rewrite():
        paper = store.load_paper("doe2026study")
        paper["claims"][0]["text"] = "Rewritten by hand."
        store.save_paper(paper)

    retag_client(monkeypatch, rewrite)
    result = extract.retag_paper("doe2026study")

    assert result["claims"][0]["text"] == "Rewritten by hand."
    assert result["claims"][0]["tags"] == ["beta"], "tags for the old wording were applied"


# --- one paper's retag failure must not cost the papers after it ----------

def test_a_retag_failure_does_not_cost_the_papers_after_it(monkeypatch):
    """Retag all runs the corpus in a fixed order; an early failure used to
    leave every later paper unretagged, unlike the tension and synthesis
    passes beside it and unlike the command line."""
    retagged = []

    def retag(key):
        if key == "a2020first":
            raise RuntimeError("the model refused")
        retagged.append(key)

    monkeypatch.setattr(extract, "retag_paper", retag)
    job = server._new_job("retag 3 papers")
    try:
        server._run_retag(job, ["a2020first", "b2021second", "c2022third"])
    finally:
        server._jobs.pop(job["id"], None)

    assert retagged == ["b2021second", "c2022third"], "a failure stopped the whole batch"
    assert job["state"] == "error"
    assert "1 of 3 papers failed" in job["detail"]


def test_a_retag_batch_that_all_works_is_done(monkeypatch):
    monkeypatch.setattr(extract, "retag_paper", lambda key: None)
    job = server._new_job("retag 2 papers")
    try:
        server._run_retag(job, ["a2020first", "b2021second"])
    finally:
        server._jobs.pop(job["id"], None)

    assert (job["state"], job["detail"]) == ("done", "2 papers")
