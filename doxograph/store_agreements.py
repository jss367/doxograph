"""Where the papers agree: groups of claims from several papers that
assert the same finding.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from . import config, records, storage, store
from .store_tensions import TENSION_STATUSES


# --- where the papers agree ------------------------------------------------
#
# An agreement is a group of claims from two or more papers that assert the
# same finding. Topics group claims by subject and tensions find where they
# differ; this is the case in between, and the common one: several papers
# saying the same thing, which is what "how much evidence do I have for X"
# needs counted. Found by a model pass per topic, reviewed like tensions, and
# kept in one file since each group belongs to several papers at once.

AGREEMENT_STATUSES = TENSION_STATUSES



def agreements_path() -> Path:
    return config.data_dir() / "agreements.json"


@contextlib.contextmanager
def agreements_lock() -> Iterator[None]:
    """Guard `agreements.json`. Nests inside `vocab_lock` alone, as the
    tensions lock does, and is never held with it."""
    with store._reentrant_file_lock(config.locks_dir() / "agreements.lock"):
        yield


def _read_agreements() -> dict[str, Any]:
    loaded = storage.read_model(agreements_path(), records.Agreements).model_dump(exclude_unset=True)
    loaded.setdefault("seq", 0)
    loaded.setdefault("agreements", [])
    loaded.setdefault("passes", {})
    return loaded


def load_agreements() -> list[dict[str, Any]]:
    return list(_read_agreements()["agreements"])


def _save_agreements(data: dict[str, Any]) -> None:
    store.write_json(agreements_path(), data)


def _agreement_papers(ids: Iterable[str], live: dict[str, dict[str, Any]]) -> set[str | None]:
    return {live[i].get("paper") for i in ids if i in live}

def agreement_pass(topic: str) -> str | None:
    """The signature of the last agreements pass over `topic`, if any."""
    return (_read_agreements().get("passes") or {}).get(topic)


def record_agreements(topic: str, found: list[dict[str, Any]], claims_by_id: dict[str, dict[str, Any]],
                      signature: str | None = None) -> dict[str, Any]:
    """Merge one topic's model output into the file.

    `found` is a list of `{"claims": [id, ...], "note"}`. The rules follow
    `record_tensions`, with one addition for groups, which can grow: a group
    the model returns that contains an existing one is that agreement with
    more members, and it goes back to open, since the new member is new
    evidence nobody has looked at. If it contains several existing ones,
    as when two pairs found separately turn out to be one finding, the
    earliest of them grows and the rest fold into it, topics included, so
    one finding is one card. A returned group contained in an existing
    one adds nothing and is kept. Members whose claims no longer exist are
    dropped, and a group left with claims from fewer than two papers goes.
    A dropped member's fingerprint is kept, so the reduced group reads as
    stale until someone judges it: the note and decision on file were about
    a group that no longer exists.

    Returns `{"added": n, "grown": n, "reopened": n, "kept": n}`.
    """
    with store.vocab_lock(), agreements_lock():
        data = _read_agreements()
        live, unreadable = store._live_claims()
        existing = []
        pruned: set[str] = set()
        for record in data["agreements"]:
            if store._held_back(record.get("claims", []), live, unreadable):
                existing.append(record)
                continue
            ids = [i for i in record.get("claims", []) if i in live]
            if len(_agreement_papers(ids, live)) < 2:
                continue
            record["claims"] = ids
            was = set(record.get("topics") or [])
            record["topics"] = sorted(t for t in record.get("topics", [])
                                      if all(t in (live[i].get("tags") or []) for i in ids))
            pruned |= was - set(record["topics"])
            existing.append(record)
        added = grown = reopened = kept = 0
        unattached = False
        for item in found:
            ids = sorted({i for i in item.get("claims", []) if i in claims_by_id and i in live})
            if len(_agreement_papers(ids, live)) < 2:
                continue
            note = (item.get("note") or "").strip()
            fingerprints = {i: store.claim_fingerprint(claims_by_id[i]) for i in ids}
            topic_live = all(topic in (live[i].get("tags") or []) for i in ids)
            wanted = set(ids)
            # The answer describes the claims as the prompt showed them. If one
            # was edited during the call, the answer is about text that no
            # longer exists, and the record on file, decided against text a
            # reviewer could see, stands: nothing grows, folds, or reopens.
            stale = any(fingerprints[i] != store.claim_fingerprint(live[i]) for i in ids)
            # Every record the returned group covers, in file order; the first
            # is the one that stays. Failing that, a record that covers it.
            # A record held back for an unreadable paper is not on the table:
            # its members are not all in `live`, and nothing can be decided
            # about a group whose claims cannot be seen.
            on_file = [r for r in existing if not store._held_back(r["claims"], live, unreadable)]
            contained = [r for r in on_file if set(r["claims"]) <= wanted]
            current = contained[0] if contained else next(
                (r for r in on_file if wanted < set(r["claims"])), None)
            if current is not None and stale:
                kept += 1
                continue
            if current is not None:
                have = set(current["claims"])
                # The members the record will hold: the returned group when it
                # grows the record, the record's own when it is a part of it.
                members = wanted | have
                topics = {topic} | set(current.get("topics", []))
                for other in contained[1:]:
                    topics |= set(other.get("topics", []))
                    existing.remove(other)
                current["topics"] = sorted(t for t in topics
                                           if all(t in (live[i].get("tags") or []) for i in members))
                # Growing a group can drop a topic the new member does not
                # carry, which is as much a pruning as the one above.
                pruned |= topics - set(current["topics"])
                if wanted > have:
                    current["claims"] = ids
                    current["fingerprints"] = fingerprints
                    current.update(note=note or current.get("note", ""), status="open", found=store.now())
                    grown += 1
                    continue
                if wanted < have:
                    kept += 1   # a part of what is already on file adds nothing
                    # But the topic goes unattached where a member of the
                    # larger record does not carry it, and this answer is not
                    # on file under the topic at all. Recording the signature
                    # would skip the pass that has to run once that member
                    # goes, and nothing else would ever put the topic back.
                    unattached = unattached or topic not in (current.get("topics") or [])
                    continue
                if current.get("fingerprints") == fingerprints:
                    # As the tensions pass: the group stands, and takes the new
                    # wording only while nobody has ruled on the old. Whatever
                    # the rerun said, including nothing: the words on file were
                    # written under a prompt this answer was not given, and
                    # keeping them would put them in this answer's mouth.
                    if current.get("status") == "open":
                        current.update(note=note)
                    kept += 1
                    continue
                current.update(note=note, fingerprints=fingerprints, status="open", found=store.now())
                reopened += 1
                continue
            data["seq"] = int(data.get("seq") or 0) + 1
            record = {
                "id": f"a{data['seq']}",
                "claims": ids,
                "topics": [topic] if topic_live else [],
                "note": note,
                "status": "open",
                "found": store.now(),
                "fingerprints": fingerprints,
            }
            existing.append(record)
            added += 1
        data["agreements"] = existing
        # Every topic taken off a record on the way through, whether before the
        # answer was read or while a group grew past it: each has to be found
        # again when it comes back, so its recorded pass goes with it.
        store._forget_passes(data, pruned)
        # Recorded only on the way out, only while the topic still holds the
        # claims it was asked about — see `record_tensions` — and only if
        # everything the answer said is on file under the topic.
        if signature and not unattached and store._topic_unchanged(topic, claims_by_id, live):
            data.setdefault("passes", {})[topic] = signature
        _save_agreements(data)
        return {"added": added, "grown": grown, "reopened": reopened, "kept": kept}


def set_agreement_status(agreement_id: str, status: str) -> dict[str, Any]:
    """Record the reviewer's decision; as for tensions, deciding refreshes
    the fingerprints and reopening does not. A decision is about the members
    that still exist, so it also drops deleted ones from the record, as long
    as two papers remain; that is what the reviewer saw and judged."""
    if status not in AGREEMENT_STATUSES:
        raise ValueError(f"status must be one of {AGREEMENT_STATUSES}, not {status!r}")
    with agreements_lock():
        data = _read_agreements()
        record: dict[str, Any]
        for record in data["agreements"]:
            if record.get("id") == agreement_id:
                record["status"] = status
                record["decided"] = store.now()
                if status != "open":
                    live, unreadable = store._live_claims()
                    ids = [i for i in record.get("claims", []) if i in live]
                    if not store._held_back(record.get("claims", []), live, unreadable) \
                            and len(_agreement_papers(ids, live)) >= 2:
                        record["claims"] = ids
                        record["fingerprints"] = {i: store.claim_fingerprint(live[i]) for i in ids}
                _save_agreements(data)
                return record
    raise KeyError(agreement_id)


def delete_agreement(agreement_id: str) -> None:
    """Remove an agreement, and forget the passes that found it; see
    `delete_tension` for why."""
    with agreements_lock():
        data = _read_agreements()
        before = len(data["agreements"])
        gone = [r for r in data["agreements"] if r.get("id") == agreement_id]
        data["agreements"] = [r for r in data["agreements"] if r.get("id") != agreement_id]
        if len(data["agreements"]) == before:
            raise KeyError(agreement_id)
        store._forget_passes(data, {topic for r in gone for topic in (r.get("topics") or [])})
        _save_agreements(data)


def agreement_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every agreement still backed by claims from two papers, joined to
    those claims, with `stale`, `n_papers`, and topics filtered to what every
    member still carries. Stale when a member was edited or removed since the
    agreement was found or decided: the fingerprints name the members the
    judgment was about, so a set of them other than the live members is stale.

    `topics_on_file` is the unfiltered set the filtering starts from. A group
    can lose a member and keep going — two papers is all it needs — and the
    topics that member alone had dropped become good again for the ones left.
    The page has to work that out for itself while a delete waits out its undo,
    and it cannot recover a topic from a list this function has already taken
    it out of, so the set it filters from is sent along with the result."""
    live = {c["id"]: c for c in (rows if rows is not None else store.claim_rows())}
    out = []
    for record in load_agreements():
        ids = [i for i in record.get("claims", []) if i in live]
        if len(_agreement_papers(ids, live)) < 2:
            continue
        fingerprints = record.get("fingerprints") or {}
        row = dict(record)
        row["claims"] = [live[i] for i in ids]
        row["n_papers"] = len(_agreement_papers(ids, live))
        row["topics_on_file"] = sorted(record.get("topics", []))
        row["topics"] = [t for t in row["topics_on_file"]
                         if all(t in (live[i].get("tags") or []) for i in ids)]
        row["stale"] = (set(fingerprints) != set(ids)
                        or any(fingerprints[i] != store.claim_fingerprint(live[i]) for i in ids))
        out.append(row)
    order = {s: n for n, s in enumerate(AGREEMENT_STATUSES)}
    out.sort(key=lambda r: (order.get(r.get("status", ""), 9), -r["n_papers"], r.get("found") or ""))
    return out
