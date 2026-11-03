"""What the papers hold, by topic: one written synthesis per topic.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import config, records, storage, store


# --- what the papers hold, by topic ---------------------------------------
#
# A synthesis is one topic's state of the question, written from its claims.
# They live in one file rather than on the papers, since each one draws on
# every paper in the topic. Written by the model, corrected by hand.

SYNTHESIS_SOURCES = ["model", "hand"]



def syntheses_path() -> Path:
    return config.data_dir() / "syntheses.json"


@contextlib.contextmanager
def syntheses_lock() -> Iterator[None]:
    """Guard `syntheses.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a write from
    the model takes both so it cannot land halfway through a rename. It is
    never held together with `tensions_lock`."""
    with store._reentrant_file_lock(config.locks_dir() / "syntheses.lock"):
        yield


def _read_syntheses() -> dict[str, Any]:
    """Read the file, reporting where it is malformed rather than reading it
    as empty: every writer starts here and would write the empty reading back
    over it. Only a missing file is empty."""
    loaded = storage.read_model(syntheses_path(), records.Syntheses).model_dump(exclude_unset=True)
    loaded.setdefault("syntheses", {})
    return loaded


def load_syntheses() -> dict[str, dict[str, Any]]:
    return dict(_read_syntheses()["syntheses"])


def _save_syntheses(data: dict[str, Any]) -> None:
    """Caller holds `syntheses_lock`."""
    store.write_json(syntheses_path(), data)


def topic_claims(topic: str, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [r for r in (rows if rows is not None else store.claim_rows()) if topic in (r.get("tags") or [])]


def synthesis_prompt_basis(topic: str, tags: list[dict[str, Any]] | None = None,
                           context: str | None = None) -> str:
    """What a synthesis was asked, apart from its claims and tensions.

    The prompt version, the model asked, the topic's name, its description and
    the research context — all of them decide what comes back, and none of
    them is part of what makes a synthesis stale, so without this a rename, a
    reworded description, a rewritten context or a change of model would never
    be written again.

    `context` is what the caller is putting in the prompt, so that the
    synthesis is filed under the context the model was actually given; read
    here only for a write that has no prompt behind it. A list rather than
    five lines run together, since the description and the context both hold
    newlines of their own.
    """
    tags = store.load_tags() if tags is None else tags
    description = next((t.get("description", "") for t in tags if t["name"] == topic), "")
    context = store.load_context() if context is None else context
    return json.dumps([config.PASS_VERSION, config.SYNTHESIS_VERSION, config.analysis_identity(),
                       topic, description, context])


def synthesis_basis(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Half of what a synthesis rests on: the set of claims, what each said,
    and which paper it came from. A claim added, removed, or edited (text,
    evidence, kind) changes it, and so does a correction to the author, year
    or title — the listing names those and the model is told to cite papers by
    author and year. Reviewing a claim does not: the prompt marks unreviewed
    claims, but a review pass over a corpus does not change what any claim
    says, and staling every synthesis while it runs would leave the mark
    meaning nothing."""
    return {r["id"]: synthesis_claim_basis(r) for r in rows}


def synthesis_claim_basis(claim: dict[str, Any]) -> str:
    """One claim as a synthesis rests on it: what it says, and whose paper it
    is. The same string the tensions merge compares."""
    return store.claim_fingerprint(claim)


def synthesis_tensions(topic: str, tensions: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """The other half: the topic's tensions as the prompt shows them, by id,
    with kind, status, whether the prompt said a claim had changed since it was
    judged, and the note. Dismissed ones are left out of the prompt, so
    dismissing one changes this as much as confirming one, or a pass finding a
    new pair, does; so does re-judging a stale one against the current text.
    The note is in because the prompt carries it: a rerun that says the same
    disagreement in different words is a different thing to write a synthesis
    from. It goes in whole rather than as a digest, because the page works the
    same comparison out for itself while a delete is held, and it cannot hash
    anything without waiting for `crypto.subtle`. `tensions` is
    `tension_rows` output, whose `topics` are already filtered to what both
    claims still carry. A record from before a field was kept holds a shorter
    list, which never compares equal to these, so it reads as stale until
    rewritten: what the model was told is not known."""
    return {t["id"]: [t.get("kind"), t.get("status"), bool(t.get("stale")),
                      t.get("note") or ""]
            for t in sorted(tensions, key=lambda t: t["id"])
            if topic in t.get("topics", []) and t.get("status") != "dismissed"}


# `before` for a `record_synthesis` call that has no snapshot to check against:
# a direct write, with no model call in between to be overtaken.
UNCHECKED = object()


def record_synthesis(topic: str, text: str, claims_by_id: dict[str, dict[str, Any]],
                     tensions: list[dict[str, Any]] | None = None, source: str = "model",
                     before: dict[str, Any] | None | object = UNCHECKED,
                     basis: str | None = None) -> dict[str, Any] | None:
    """Write one topic's synthesis.

    `claims_by_id` is every claim the model was shown, as it stood when the
    prompt was built, and `tensions` the `tension_rows` it was shown alongside
    them; the basis is fingerprinted from those, not from disk, so a claim
    edited or a tension confirmed during the call leaves the synthesis stale
    rather than silently current. With no `tensions` given the ones on file
    now are used: a direct write, with no call in between to be overtaken.

    `before` is the record on file for this topic when the prompt was built,
    or None if there was none. The web app leaves edit and delete enabled
    while a synthesis job runs, so a reviewer can correct or delete the text
    while the model is thinking. That decision is newer than this answer and
    must not be written over, as extraction leaves a claim edited during its
    call alone: if the record on file no longer matches `before`, whether it
    was edited, deleted, or created meanwhile, nothing is written. A record
    found exactly as the call left it is not a change, so a repeat run writes
    as usual.

    Holds `vocab_lock` so a rename cannot interleave: `_retag_all` moves the
    record to the new name under that lock, and a result arriving after sees
    that no live claim carries the old name and is dropped rather than
    recreating the topic. Returns the record, or None when it was dropped.
    """
    if source not in SYNTHESIS_SOURCES:
        raise ValueError(f"source must be one of {SYNTHESIS_SOURCES}, not {source!r}")
    # The response schema admits an empty string, so a model that answers
    # with nothing would otherwise be written as a blank record and, on a
    # rewrite, replace a useful synthesis. Refuse it as the hand-edit path
    # does: the caller reports a failure and the saved text stands.
    text = (text or "").strip()
    if not text:
        raise ValueError("the model returned an empty synthesis; nothing written")
    with store.vocab_lock(), syntheses_lock():
        live = topic_claims(topic)
        if not live:
            return None
        data = _read_syntheses()
        if before is not UNCHECKED and data["syntheses"].get(topic) != before:
            return None
        record = {
            "topic": topic,
            "text": text,
            "source": source,
            "written": store.now(),
            # The prompt it was written under, topic name and description
            # included: both are in what the model was given, and a rename or
            # a reworded description changes the question. Taken as the prompt
            # was built, not as things stand now — a description edited during
            # the call describes a question this answer was never asked. A
            # record with none of this is older than the field and counts as
            # older than the prompt.
            "prompt_basis": basis if basis is not None else synthesis_prompt_basis(topic),
            "claims": {i: synthesis_claim_basis(c) for i, c in claims_by_id.items()
                       if topic in (c.get("tags") or [])},
            "tensions": synthesis_tensions(topic, store.tension_rows() if tensions is None else tensions),
        }
        data["syntheses"][topic] = record
        _save_syntheses(data)
        return record


def set_synthesis_text(topic: str, text: str) -> dict[str, Any]:
    """Record a correction by hand. It is a judgment against the claims and
    tensions as they stand now, so the basis is refreshed and the synthesis
    stops being stale."""
    text = (text or "").strip()
    if not text:
        raise ValueError("a synthesis cannot be empty; delete it instead")
    with store.vocab_lock(), syntheses_lock():
        data = _read_syntheses()
        if topic not in data["syntheses"]:
            raise KeyError(topic)
        record: dict[str, Any] = data["syntheses"][topic]
        record.update(text=text, source="hand", written=store.now(),
                      prompt_basis=synthesis_prompt_basis(topic),
                      claims=synthesis_basis(topic_claims(topic)),
                      tensions=synthesis_tensions(topic, store.tension_rows()))
        _save_syntheses(data)
        return record


def delete_synthesis(topic: str) -> None:
    with syntheses_lock():
        data = _read_syntheses()
        if data["syntheses"].pop(topic, None) is None:
            raise KeyError(topic)
        _save_syntheses(data)


def synthesis_rows(rows: list[dict[str, Any]] | None = None,
                   tensions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every synthesis whose topic still has claims, with `stale` set when a
    claim in the topic was added, removed, or edited since it was written, or
    a tension in it was found, confirmed, dismissed, or deleted. A stale
    synthesis is still shown: it may still be right, and the reader decides
    whether to rewrite it. One whose topic has no claims left is dropped from
    view; `_retag_all` drops it from the file when the tag goes. A record
    written before tensions were part of the basis has none on file and reads
    as stale while the topic has any: what the model saw is not known."""
    rows = rows if rows is not None else store.claim_rows()
    tensions = store.tension_rows(rows) if tensions is None else tensions
    out = []
    for topic, record in sorted(load_syntheses().items()):
        live = topic_claims(topic, rows)
        if not live:
            continue
        row = dict(record)
        row["topic"] = topic
        row["stale"] = (synthesis_basis(live) != (record.get("claims") or {})
                        or synthesis_tensions(topic, tensions) != (record.get("tensions") or {}))
        row["n_claims"] = len(live)
        row["n_papers"] = len({r["paper"] for r in live})
        out.append(row)
    return out


def synthesis_topics(rows: list[dict[str, Any]] | None = None) -> list[str]:
    """Topics worth a synthesis by default: claims from at least two papers.
    One paper's claims can be synthesized too, by naming the topic."""
    return store.tension_topics(rows)
