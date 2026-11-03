"""Tensions between papers, and the per-topic pass bookkeeping they share
with agreements and syntheses.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from . import config, records, storage, store


# --- tensions between papers ---------------------------------------------
#
# A tension is a pair of claims from two different papers that pull against
# each other on the same question. They are found by a model pass over each
# topic and then reviewed like claims: open until somebody confirms or
# dismisses them. They live in one file rather than inside the paper files
# because each one belongs to two papers at once.

TENSION_KINDS = ["contradiction", "tension"]
TENSION_STATUSES = ["open", "confirmed", "dismissed"]



def tensions_path() -> Path:
    return config.data_dir() / "tensions.json"


@contextlib.contextmanager
def tensions_lock() -> Iterator[None]:
    """Guard `tensions.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a tension merge
    takes both so it cannot land halfway through a rename. The pass itself
    reads papers without their locks and writes only here."""
    with store._reentrant_file_lock(config.locks_dir() / "tensions.lock"):
        yield


def _read_tensions() -> dict[str, Any]:
    """Read the ledger, reporting where it is malformed rather than reading it
    as empty. Every writer starts here, so an empty reading would be written
    back over the file: every decision gone and ids restarting at t1. Only a
    missing file is an empty ledger. Writes are atomic, so a half-written file
    is never seen; anything unreadable was edited by hand or damaged."""
    loaded = storage.read_model(tensions_path(), records.Tensions).model_dump(exclude_unset=True)
    loaded.setdefault("seq", 0)
    loaded.setdefault("tensions", [])
    loaded.setdefault("passes", {})
    return loaded


def load_tensions() -> list[dict[str, Any]]:
    return list(_read_tensions()["tensions"])


def _save_tensions(data: dict[str, Any]) -> None:
    """Caller holds `tensions_lock`."""
    store.write_json(tensions_path(), data)


def _pair(ids: Iterable[str]) -> tuple[str, str]:
    a, b = sorted(ids)
    return (a, b)


def _live_claims() -> tuple[dict[str, dict[str, Any]], bool]:
    """Every claim on disk by id, and whether any paper could not be read.

    A merge prunes records against the corpus as it is now, and a claim whose
    paper is unreadable is not gone: the file is there, and the reader refuses
    it so a writer cannot replace it. Dropping the tensions and agreements
    that rest on it would be the loss the reader guards against, so while a
    paper cannot be read, a record with a claim that is not in `live` is left
    exactly as the file has it. A claim id names its paper only by convention
    (`next_claim_id`); an older or hand-written id need not, so the missing
    claim cannot be told from the unreadable paper's, and neither is pruned.
    """
    issues: list[records.StorageIssue] = []
    papers = store.all_papers(issues=issues)
    return {c["id"]: c for c in store.claim_rows(papers)}, bool(issues)


def _held_back(ids: Iterable[str], live: dict[str, dict[str, Any]], unreadable: bool) -> bool:
    """Whether a record's claims include one that cannot be seen right now."""
    return unreadable and any(i not in live for i in ids)


def _shared_topics(tension: dict[str, Any], live: dict[str, dict[str, Any]]) -> list[str]:
    """The stored topics both claims still carry. A topic removed from either
    claim in the editor leaves the tension with it: a pass under that topic
    can no longer return the pair, since the edited claim is no longer in the
    prompt, and nothing else would ever take the name off. A tension left
    with no topics is still a tension."""
    tags = [set(live[i].get("tags") or []) for i in tension.get("claims", [])]
    return sorted(t for t in tension.get("topics", []) if all(t in s for s in tags))


def claim_fingerprint(claim: dict[str, Any]) -> str:
    """What a tension's judgment rests on: everything `_tension_listing` shows
    the model about a claim, whose paper included. If the text, evidence or
    kind changes the judgment was made about a claim that no longer exists,
    and if the paper's author, year or title changes the note the model wrote
    may name the wrong people."""
    return json.dumps([claim.get("text", ""), claim.get("evidence", ""),
                       claim.get("kind", "finding"), cite_head(claim)])


def cite_head(claim: dict[str, Any]) -> str:
    """How a cross-paper prompt names a claim's paper.

    `_tension_listing` writes the cited surname, "et al." where there is more
    than one author, the year and the title. Adding a coauthor changes the
    heading without changing any one of those fields, so the count is in here
    too.
    """
    authors = claim.get("paper_authors") or []
    key = claim.get("paper") or ""
    head = store.cite_surname(authors, key)
    if len(authors) > 1:
        head += " et al."
    return f"{head} ({claim.get('paper_year') or 'n.d.'}): {claim.get('paper_title') or key}"


def _forget_passes(data: dict[str, Any], topics: set[str]) -> None:
    """Drop the recorded pass for each of `topics`. Caller saves."""
    passes = data.get("passes") or {}
    for topic in topics:
        passes.pop(topic, None)
    data["passes"] = passes


def _topic_unchanged(topic: str, shown: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]]) -> bool:
    """Whether a topic still holds the claims a pass was asked about, as it
    was asked about them. Anything else and the answer describes a question
    that is no longer being put."""
    def basis(claims: dict[str, dict[str, Any]]) -> dict[str, str]:
        return {i: claim_fingerprint(c) for i, c in claims.items()
                if topic in (c.get("tags") or [])}
    was = basis(shown)
    return bool(was) and was == basis(live)


def pass_signature(topic: str, rows: list[dict[str, Any]], extra: str = "") -> str:
    """What a per-topic pass was asked about, in one string.

    Everything the prompt carries: which claims are in the topic, what each one
    says, and which paper it is from — the listing names the authors, year and
    title, and correcting any of those is a correction the model should see —
    plus `extra` for the parts that are not claims at all: the topic's
    description, the research context, the prompt itself. A pass whose
    signature is unchanged would be asked exactly what it was asked last time,
    and the merge would keep the answer it already has, so it is not asked.
    """
    payload = [topic, extra] + sorted(
        f"{r['id']} {r.get('paper')} {claim_fingerprint(r)}" for r in rows
    )
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()[:16]


def tension_pass(topic: str) -> str | None:
    """The signature of the last tensions pass over `topic`, if any."""
    return (_read_tensions().get("passes") or {}).get(topic)



def record_tensions(topic: str, found: list[dict[str, Any]], claims_by_id: dict[str, dict[str, Any]],
                    signature: str | None = None) -> dict[str, Any]:
    """Merge one topic's model output into the file.

    `found` is a list of `{"claims": [id, id], "kind", "note"}`; `claims_by_id`
    is every claim the model was shown, as it stood when the prompt was built.

    Rules, in order:
    - A pair whose claims no longer both exist is dropped, wherever it came from.
    - A pair already on file whose claims are unchanged is left exactly as it
      is: its status is a decision somebody made and the model does not get to
      remake it. A repeat run costs the reviewer nothing.
    - A pair already on file whose claims have changed is refreshed and set
      back to open: the old verdict was about different text.
    - Unless the change landed while the call was in flight. Then the answer
      describes text nobody can see any more, and the record on file is left
      alone, as extraction leaves a claim edited during its call alone: a
      decision the reviewer made meanwhile against the new text is newer than
      this answer and must not be reopened by it. `tension_rows` says stale if
      nobody has re-judged the pair.
    - A new pair is added as open.
    - A pair the model did not return this time is kept. The pass is per topic
      and a claim can carry several topics, so absence from one topic's answer
      says nothing; and a confirmed tension is the reviewer's, not the model's.
    - `topic` is attached only if both claims still carry it now. The tag can
      be renamed or deleted while the model call is in flight; `_retag_all`
      has already rewritten the tensions on file by then, and this late result
      must not put the old name back. The pair itself is still recorded, and
      the next pass under the new name attaches that.
    - Stored topics that either claim no longer carries are dropped from every
      tension on file, whether or not the model returned it this time. A claim
      edit does not touch this file, so `tension_rows` applies the same filter
      at read time; this write is where the file catches up.

    The merge holds `vocab_lock` so that last rule cannot fire halfway through
    a rename, when the claims already carry the new name and the tensions
    still carry the old: it would read the old name as dropped and take it
    off before `_retag_all` could convert it. Vocabulary before tensions, as
    `_retag_all` takes them.

    Returns `{"added": n, "reopened": n, "kept": n}`.
    """
    with store.vocab_lock(), tensions_lock():
        data = _read_tensions()
        # Prune against the corpus as it is now, not as the prompt saw it: a
        # claim deleted during the call must not come back as half a tension.
        live, unreadable = _live_claims()
        existing = [t for t in data["tensions"]
                    if len(t.get("claims", [])) == 2
                    and (all(i in live for i in t["claims"]) or _held_back(t["claims"], live, unreadable))]
        pruned: set[str] = set()
        for tension in existing:
            if _held_back(tension["claims"], live, unreadable):
                continue
            was = set(tension.get("topics") or [])
            tension["topics"] = _shared_topics(tension, live)
            pruned |= was - set(tension["topics"])
        # A topic taken off a record here is a topic whose pairs have to be
        # found again when it comes back. Its recorded pass goes with it, or
        # restoring the tag would reproduce the signature and skip the run.
        _forget_passes(data, pruned)
        by_pair = {_pair(t["claims"]): t for t in existing}
        added = reopened = kept = 0
        for item in found:
            ids = [i for i in item.get("claims", []) if i in claims_by_id and i in live]
            if len(set(ids)) != 2:
                continue
            a, b = _pair(ids)
            if live[a].get("paper") == live[b].get("paper"):
                continue    # a paper in tension with itself is not what this is for
            # Fingerprint what the model was shown, not what is on disk now. The
            # judgment is about the prompt text; if a claim was edited during
            # the call, `tension_rows` must be able to see that and say stale.
            fingerprints = {a: claim_fingerprint(claims_by_id[a]), b: claim_fingerprint(claims_by_id[b])}
            note = (item.get("note") or "").strip()
            kind = item.get("kind") if item.get("kind") in TENSION_KINDS else TENSION_KINDS[-1]
            topic_live = all(topic in (live[i].get("tags") or []) for i in (a, b))
            current = by_pair.get((a, b))
            if current is not None:
                if topic_live and topic not in current.setdefault("topics", []):
                    current["topics"].append(topic)
                    current["topics"].sort()
                if current.get("fingerprints") == fingerprints:
                    # The same pair over the same text: the finding stands, and
                    # keeps the day it was found and whatever was decided about
                    # it. A newer model or a newer prompt can still put it in
                    # better words — unless a reviewer has ruled on the words
                    # it has, which is a decision about what it says.
                    if current.get("status") == "open":
                        current.update(kind=kind, note=note)
                    kept += 1
                    continue
                if any(fingerprints[i] != claim_fingerprint(live[i]) for i in (a, b)):
                    kept += 1   # changed during the call: see the docstring
                    continue
                current.update(kind=kind, note=note, fingerprints=fingerprints,
                               status="open", found=store.now())
                reopened += 1
                continue
            data["seq"] = int(data.get("seq") or 0) + 1
            record = {
                "id": f"t{data['seq']}",
                "claims": [a, b],
                "topics": [topic] if topic_live else [],
                "kind": kind,
                "note": note,
                "status": "open",
                "found": store.now(),
                "fingerprints": fingerprints,
            }
            existing.append(record)
            by_pair[(a, b)] = record
            added += 1
        data["tensions"] = existing
        # Recorded only on the way out, so a failed call is asked again, and
        # only while the topic still holds the claims it was asked about. A
        # tag taken off one of them during the call — or off all of them —
        # means this answer was about a topic that no longer exists in that
        # shape; writing its signature down would skip the pass that has to
        # run when the tag comes back.
        if signature and _topic_unchanged(topic, claims_by_id, live):
            data.setdefault("passes", {})[topic] = signature
        _save_tensions(data)
        return {"added": added, "reopened": reopened, "kept": kept}


def set_tension_status(tension_id: str, status: str) -> dict[str, Any]:
    """Record the reviewer's decision.

    Confirming or dismissing is a judgment about the claims as they read now,
    so it also refreshes the fingerprints: a tension that went stale because a
    claim was edited stops being stale once someone has decided it against the
    current text. Reopening is not a judgment and leaves the fingerprints
    alone, so a reopened tension stays stale until it is re-judged.
    """
    if status not in TENSION_STATUSES:
        raise ValueError(f"status must be one of {TENSION_STATUSES}, not {status!r}")
    with tensions_lock():
        data = _read_tensions()
        tension: dict[str, Any]
        for tension in data["tensions"]:
            if tension.get("id") == tension_id:
                tension["status"] = status
                tension["decided"] = store.now()
                if status != "open":
                    live = {c["id"]: c for c in store.claim_rows()}
                    ids = tension.get("claims", [])
                    if all(i in live for i in ids):
                        tension["fingerprints"] = {i: claim_fingerprint(live[i]) for i in ids}
                _save_tensions(data)
                return tension
    raise KeyError(tension_id)


def delete_tension(tension_id: str) -> None:
    """Remove a tension, and forget the passes that found it.

    Deleting is not dismissing: a dismissed pair stays on file and a repeat
    pass leaves the decision alone, but a deleted one is gone, and the topics
    it was found under have to be asked again for it to come back. Their
    signatures would otherwise match and the pass would skip them.
    """
    with tensions_lock():
        data = _read_tensions()
        before = len(data["tensions"])
        gone = [t for t in data["tensions"] if t.get("id") == tension_id]
        data["tensions"] = [t for t in data["tensions"] if t.get("id") != tension_id]
        if len(data["tensions"]) == before:
            raise KeyError(tension_id)
        _forget_passes(data, {topic for t in gone for topic in (t.get("topics") or [])})
        _save_tensions(data)


def tension_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every tension whose claims still exist, joined to those claims.

    Each row carries `claims` as the two claim rows (not ids), plus `stale`,
    true when either claim has been edited since the tension was found. A stale
    tension is still shown: the reviewer decides whether the edit settled it.
    `topics` is the stored list filtered to what both claims still carry, so a
    topic taken off a claim in the editor disappears here at once even though
    the file is only rewritten by the next `record_tensions`.
    """
    live = {c["id"]: c for c in (rows if rows is not None else store.claim_rows())}
    out = []
    for tension in load_tensions():
        ids = tension.get("claims", [])
        if len(ids) != 2 or not all(i in live for i in ids):
            continue
        fingerprints = tension.get("fingerprints") or {}
        row = dict(tension)
        row["claims"] = [live[i] for i in ids]
        row["topics"] = _shared_topics(tension, live)
        row["stale"] = any(fingerprints.get(i) != claim_fingerprint(live[i]) for i in ids)
        out.append(row)
    order = {s: n for n, s in enumerate(TENSION_STATUSES)}
    out.sort(key=lambda t: (order.get(t.get("status", ""), 9), t.get("found") or ""), reverse=False)
    return out


def tension_topics(rows: list[dict[str, Any]] | None = None) -> list[str]:
    """Topics where a tension is possible: claims from at least two papers."""
    papers_by_tag: dict[str, set[str]] = {}
    for row in rows if rows is not None else store.claim_rows():
        for tag in row.get("tags", []):
            papers_by_tag.setdefault(tag, set()).add(row["paper"])
    return sorted(tag for tag, papers in papers_by_tag.items() if len(papers) >= 2)
