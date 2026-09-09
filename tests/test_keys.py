"""Paper keys are unique for all time and never run out."""

from __future__ import annotations

import threading

import pytest

from doxograph import ingest, store


def test_a_deleted_paper_key_is_never_issued_again():
    """A citekey identifies one paper for all time, so nothing has to prove which."""
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.add_claim("doe2026study", {"text": "One."})
    store.delete_paper("doe2026study")

    # A different paper that produces the same coarse citekey.
    key = store.reserve_key("doe2026study", title="A Study of Something Else")
    assert key == "doe2026studya", "the retired key was issued again"
    assert store.add_claim(key, {"text": "Unrelated."})["id"] == "doe2026studya-c1"


def test_keys_keep_retiring_across_incarnations():
    issued = []
    for _ in range(3):
        key = store.reserve_key("doe2026study")
        issued.append(key)
        store.add_claim(key, {"text": "X."})
        store.delete_paper(key)
    assert issued == ["doe2026study", "doe2026studya", "doe2026studyb"]
    assert store.retired_keys() == set(issued)


def test_a_key_with_no_history_is_issued_as_is():
    assert store.reserve_key("doe2026study") == "doe2026study"


def test_retiring_uses_a_lock_that_no_other_lock_nests_inside():
    """`delete_paper` holds the paper lock while retiring.

    Sharing the vocabulary lock here would invert the documented order — a tag
    rename holds vocabulary and takes paper locks — and deadlock. This asserts
    the two do not contend.
    """
    import threading

    store.add_tag("alpha")
    for n in range(3):
        store.save_paper(store.new_paper(f"doe2026s{n}"))
        store.add_claim(f"doe2026s{n}", {"text": "X.", "tags": ["alpha"]})

    errors = []
    start = threading.Barrier(2)

    def rename():
        try:
            start.wait(timeout=5)
            store.rename_tag("alpha", "renamed")
        except Exception as exc:
            errors.append(exc)

    def remove():
        try:
            start.wait(timeout=5)
            for n in range(3):
                store.delete_paper(f"doe2026s{n}")
        except Exception as exc:
            errors.append(exc)

    # Daemons: if the lock order inverts these never finish, and a non-daemon
    # thread would hang the interpreter at exit instead of failing the test.
    threads = [threading.Thread(target=rename, daemon=True),
               threading.Thread(target=remove, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "deleting a paper deadlocked against a tag rename"
    assert not errors, errors


def test_deleting_a_paper_records_its_key_even_with_no_claims():
    store.save_paper(store.new_paper("doe2026study"))
    store.delete_paper("doe2026study")
    assert store.retired_keys() == {"doe2026study"}


def test_reserving_a_key_holds_the_retirement_lock(monkeypatch):
    """Check and create must be one transaction.

    Reading the retired set and creating separately leaves a gap in which a
    concurrent `delete_paper` retires and unlinks a key; the caller then still
    believes it is free and recreates it. Asserted as the invariant, because the
    gap is microseconds and not reachable from outside.
    """
    held = []
    real = store.retired_keys

    def watched():
        held.append(bool(getattr(store._depth, "held", {}).get("retired")))
        return real()

    monkeypatch.setattr(store, "retired_keys", watched)
    store.reserve_key("doe2026study")

    assert held, "the retired set was never consulted"
    assert all(held), "the retired set was read without holding the retirement lock"


def test_key_allocation_continues_past_z():
    """The a-z ceiling became permanent once keys were retired."""
    for candidate in store.key_candidates("doe2026study"):
        store.save_paper(store.new_paper(candidate))
        if candidate.endswith("z") and len(candidate) == len("doe2026study") + 1:
            break

    assert store.reserve_key("doe2026study") == "doe2026studyaa"


def test_repeated_delete_and_readd_never_runs_out():
    """Thirty cycles on one coarse key used to fail permanently at the 27th."""
    issued = []
    for _ in range(30):
        key = store.reserve_key("doe2026study")
        issued.append(key)
        store.delete_paper(key)
    assert len(set(issued)) == 30
    assert issued[26] == "doe2026studyz"           # the old ceiling
    assert issued[27] == "doe2026studyaa"          # continued past it
    assert store.reserve_key("doe2026study") not in set(issued)


def test_many_metadata_free_uploads_of_the_same_filename(monkeypatch):
    """The reported path: several files all called paper.pdf."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    keys = []
    for n in range(30):
        key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody" + bytes([n]), "paper.pdf")
        assert created, f"upload {n} did not create a paper"
        keys.append(key)
    assert len(set(keys)) == 30


def test_key_exhaustion_still_reports_clearly(monkeypatch):
    monkeypatch.setattr(store, "MAX_KEY_CANDIDATES", 3)
    monkeypatch.setattr(store, "key_candidates", lambda base: iter([base, base + "a", base + "b"]))
    for suffix in ("", "a", "b"):
        store.save_paper(store.new_paper("doe2026study" + suffix))
    with pytest.raises(RuntimeError, match="cannot find an unused key"):
        store.reserve_key("doe2026study")
