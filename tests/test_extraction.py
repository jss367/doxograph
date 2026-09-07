"""Extraction merges the model answer into a paper that may have changed meanwhile."""

from __future__ import annotations

import json
import threading
import time

from doxograph import config, extract, store


# --- a tag deleted during the call must not come back --------------------

def extraction_payload(tags, proposed=()):
    return {
        "summary": "", "relevance": "",
        "proposed_tags": [{"name": n, "description": ""} for n in proposed],
        "claims": [{"text": "A finding.", "kind": "finding", "strength": "aside",
                    "tags": list(tags), "evidence": "", "quote": "", "locator": "",
                    "ledger_links": []}],
    }


def test_a_tag_deleted_during_the_call_is_not_written_back():
    store.add_tag("alpha")
    store.add_tag("doomed")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())          # what the model was shown

    store.delete_tag("doomed")                    # the user deletes it mid-call

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "doomed"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha"], "a deleted tag came back"
    assert [t["name"] for t in paper["proposed_tags"]] == []


def test_a_tag_renamed_during_the_call_does_not_reappear():
    store.add_tag("old")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())

    store.rename_tag("old", "new")

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["old"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == []
    assert "old" not in store.tag_names()


def test_a_genuinely_new_tag_is_still_proposed():
    """Only names the vocabulary just lost are dropped, not invented ones."""
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "invented"], proposed=["invented"]),
        prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha", "invented"]
    assert [t["name"] for t in paper["proposed_tags"]] == ["invented"]


def test_a_deleted_tag_re_proposed_by_the_model_is_allowed():
    """If the model puts the name forward as new, that is a proposal, not an echo."""
    store.add_tag("doomed")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())
    store.delete_tag("doomed")

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["doomed"], proposed=["doomed"]),
        prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["doomed"]
    assert [t["name"] for t in paper["proposed_tags"]] == ["doomed"]


def test_without_a_snapshot_nothing_is_dropped():
    """Callers that do not pass `prompt_tags` keep the previous behavior."""
    store.save_paper(store.new_paper("doe2026study"))
    paper = extract.merge_extraction("doe2026study", extraction_payload(["whatever"]))
    assert paper["claims"][0]["tags"] == ["whatever"]


# --- the prompt and its snapshot must come from one read -----------------

def test_the_prompt_and_the_snapshot_come_from_one_vocabulary_read(monkeypatch):
    """Two reads let a tag added between them reach the prompt but not the snapshot.

    Asserted by counting: the vocabulary is read exactly once before the prompt
    is handed to the model, so the two cannot disagree.
    """
    store.add_tag("alpha")
    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    reads = []
    real_load_tags = store.load_tags
    monkeypatch.setattr(store, "load_tags",
                        lambda: (reads.append(1), real_load_tags())[1])

    seen = {}

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            seen["prompt"] = kwargs["messages"][0]["content"][1]["text"]
            seen["reads_before_prompt"] = len(reads)
            payload = {"summary": "", "relevance": "", "proposed_tags": [], "claims": []}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    extract.extract_paper("doe2026study")

    assert "- alpha:" in seen["prompt"], "the vocabulary did not reach the prompt"
    assert seen["reads_before_prompt"] == 1, (
        f"the vocabulary was read {seen['reads_before_prompt']} times before the prompt; "
        "a tag arriving between reads would be in the prompt but not the snapshot")


def test_a_tag_added_just_before_the_call_is_still_recognised():
    """The regression the single read prevents, asserted at the merge."""
    store.add_tag("alpha")
    store.add_tag("late-arrival")
    store.save_paper(store.new_paper("doe2026study"))
    prompt_tags = set(store.tag_names())      # both names were shown

    store.delete_tag("late-arrival")          # deleted while the call ran

    paper = extract.merge_extraction(
        "doe2026study", extraction_payload(["alpha", "late-arrival"]), prompt_tags=prompt_tags)
    assert paper["claims"][0]["tags"] == ["alpha"]
    assert [t["name"] for t in paper["proposed_tags"]] == []


def test_a_correction_made_during_a_re_read_survives(monkeypatch):
    """A claim edited while the model was reading is newer than its answer."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    claim = store.add_claim("doe2026study",
                            {"text": "Original wording.", "tags": [], "reviewed": False})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.update_claim("doe2026study", claim["id"], {"text": "Corrected by hand."})
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    texts = [c["text"] for c in paper["claims"]]
    assert "Corrected by hand." in texts, "the correction was overwritten by the re-read"
    assert "A fresh claim." in texts


def test_a_claim_written_during_a_re_read_survives(monkeypatch):
    """A claim added by hand while the model read is not in the snapshot."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            store.add_claim("doe2026study",
                            {"text": "Written by hand.", "tags": [], "reviewed": False})
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    texts = [c["text"] for c in paper["claims"]]
    assert "Written by hand." in texts, "a hand-written claim was discarded"
    assert "A fresh claim." in texts


def test_an_untouched_unreviewed_claim_is_still_replaced(monkeypatch):
    """The rule only spares claims that changed; a re-read still replaces."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_claim("doe2026study",
                    {"text": "Original wording.", "tags": [], "reviewed": False})

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            payload = {"summary": "", "relevance": "", "proposed_tags": [],
                       "claims": [{"text": "A fresh claim.", "kind": "finding",
                                   "strength": "aside", "tags": [], "evidence": "",
                                   "quote": "", "locator": "", "ledger_links": []}]}
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})
    paper = extract.extract_paper("doe2026study")

    assert [c["text"] for c in paper["claims"]] == ["A fresh claim."]


# --- post-merge stabilization --------------------------------------------

def test_overlapping_extractions_are_serialized_without_blocking_edits(monkeypatch):
    """A second re-read starts only after the first merge has completed."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")

    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    second_started = threading.Event()
    call_guard = threading.Lock()
    call_count = 0

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            nonlocal call_count
            with call_guard:
                call_count += 1
                call_number = call_count
            if call_number == 1:
                first_entered.set()
                assert release_first.wait(5), "test did not release the first extraction"
            else:
                second_entered.set()
            payload = {
                "summary": "",
                "relevance": "",
                "proposed_tags": [],
                "claims": [{
                    "text": f"Extraction {call_number}.",
                    "kind": "finding",
                    "strength": "aside",
                    "tags": [],
                    "evidence": "",
                    "quote": "",
                    "locator": "",
                    "ledger_links": [],
                }],
            }
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type("R", (), {"content": [block], "stop_reason": "end_turn", "usage": None})()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(extract, "_pdf_block", lambda key: {"type": "text", "text": "pdf"})

    errors = []

    def run(second=False):
        if second:
            second_started.set()
        try:
            extract.extract_paper("doe2026study")
        except BaseException as exc:  # collect worker failures for the assertion thread
            errors.append(exc)

    first = threading.Thread(target=run)
    second = threading.Thread(target=run, kwargs={"second": True})
    first.start()
    assert first_entered.wait(5), "the first extraction never reached the model"
    second.start()
    assert second_started.wait(5)
    time.sleep(0.2)
    assert not second_entered.is_set(), "the second model call overlapped the first"

    # The extraction lock must not be the paper lock: an ordinary edit can land
    # while the model call is parked and is reconciled by the merge.
    manual = store.add_claim("doe2026study", {"text": "Written while reading."})
    release_first.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert second_entered.is_set()
    claims = store.load_paper("doe2026study")["claims"]
    assert [claim["text"] for claim in claims] == ["Written while reading.", "Extraction 2."]
    assert manual["id"] == claims[0]["id"]


def test_extraction_lock_is_held_across_processes(tmp_path):
    """The server and CLI cannot re-read one paper at the same time."""
    import subprocess
    import sys
    import textwrap

    log = tmp_path / "extraction-order.log"
    script = textwrap.dedent(f"""
        import os
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import store
        with store.extraction_lock("doe2026study"):
            open({str(log)!r}, "a").write("child\\n")
    """)

    with store.extraction_lock("doe2026study"):
        child = subprocess.Popen([sys.executable, "-c", script])
        time.sleep(0.5)
        blocked = child.poll() is None
        log.write_text("parent\n")
    child.wait(timeout=15)

    assert blocked, "the child did not wait for the extraction lock"
    assert child.returncode == 0
    assert log.read_text().split() == ["parent", "child"]
