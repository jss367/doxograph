"""The topic vocabulary, and carrying a rename or a deletion across the corpus.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from . import config, storage, store, workspace_metadata


# --- controlled vocabulary -------------------------------------------------

DEFAULT_TAGS: list[dict[str, Any]] = []


def _vocabulary() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.tags_path(), workspace_metadata.Vocabulary,
    ).model_dump(exclude_unset=True)


def load_tags() -> list[dict[str, Any]]:
    return _vocabulary().get("tags") or []


def _retired_topics() -> set[str]:
    return set(_vocabulary().get("retired_topics", []))


def save_tags(tags: list[dict[str, Any]], *, retire: str | None = None) -> None:
    """Caller must hold `vocab_lock`; every public mutator below does."""
    ordered = sorted(tags, key=lambda t: t["name"])
    payload = _vocabulary()
    retired = set(payload.get("retired_topics", []))
    if retire is not None:
        retired.add(retire)
    # Explicitly recreating a vocabulary entry makes its name usable again.
    retired.difference_update(t["name"] for t in ordered)
    payload["tags"] = ordered
    if retired:
        payload["retired_topics"] = sorted(retired)
    else:
        payload.pop("retired_topics", None)
    workspace_metadata.Vocabulary.model_validate(payload, strict=True)
    store.write_atomic(
        config.tags_path(),
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
    )


def tag_names() -> list[str]:
    return [t["name"] for t in load_tags()]


def add_tag(name: str, description: str = "") -> list[dict[str, Any]]:
    name = store.slugify(name)
    with store.vocab_lock():
        tags = load_tags()
        if name and name not in {t["name"] for t in tags}:
            tags.append({"name": name, "description": description})
            save_tags(tags)
        return tags


class StorageRecoveryError(RuntimeError):
    """An interrupted topic or facet change still needs to finish."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        # The intent file the reader is told to keep.
        self.path = path if path is not None else tag_change_path()


def tag_change_path() -> Path:
    return config.data_dir() / "pending-topic-change.json"


def _recover_tag_change() -> None:
    """Finish a durable intent under the vocabulary lock, including on restart.

    Reapply the topic transformation to current records, never replay old paper
    snapshots: notes and review decisions written since the failure must survive.
    Each step is idempotent. Keep the intent until every replacement is durable.
    """
    path = tag_change_path()
    try:
        try:
            change = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (not isinstance(change, dict) or not {"version", "old", "new", "tags"} <= change.keys()
                or change.get("version") != 1
                or not isinstance(change.get("old"), str)
                or not (change.get("new") is None or isinstance(change.get("new"), str))
                or not isinstance(change.get("tags"), list)
                or any(not isinstance(t, dict) or not isinstance(t.get("name"), str)
                       for t in change["tags"])):
            raise ValueError("invalid pending topic change")
        # A damaged vocabulary must block recovery before any papers move.
        _vocabulary()
        workspace_metadata.Vocabulary.model_validate({"tags": change["tags"]}, strict=True)
        _retag_all(change["old"], change["new"])
        # Publish the vocabulary last. Until then, the durable intent explains
        # any partially moved claims and prevents a competing vocabulary edit.
        save_tags(change["tags"], retire=change["old"])
        path.unlink()
        store._sync_directory(path.parent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StorageRecoveryError(
            f"A topic change is unfinished: {exc}. Doxograph will retry automatically. "
            f"Keep {path} until recovery finishes."
        ) from exc


def recover_storage() -> None:
    """Called before serving library state and before CLI operations."""
    # Ordinary reads do not need write access to a lock file. Only take the
    # lock when there is recovery work; its holder rechecks the intent.
    try:
        if tag_change_path().exists():
            with store.vocab_lock():
                pass
    except OSError as exc:
        raise StorageRecoveryError(
            f"Cannot resume the unfinished topic change: {exc}. "
            f"Keep {tag_change_path()}; Doxograph will retry automatically."
        ) from exc
    try:
        if store.facet_change_path().exists():
            with store.facets_lock():
                pass
    except OSError as exc:
        raise StorageRecoveryError(
            f"Cannot resume the unfinished facet change: {exc}. "
            f"Keep {store.facet_change_path()}; Doxograph will retry automatically.",
            path=store.facet_change_path(),
        ) from exc


def _change_tag(old: str, new: str | None, tags: list[dict[str, Any]]) -> None:
    """Start a recoverable topic change; caller holds the vocabulary lock."""
    # Refuse known damage before starting a multi-file operation. A paper
    # deleted during the scan is fine; an unreadable one must not be skipped.
    for key in store.paper_keys():
        try:
            store.load_paper(key)
        except KeyError:
            continue
    store._read_tensions()
    store._read_agreements()
    store._read_syntheses()
    _retired_topics()
    store.write_json(tag_change_path(), {"version": 1, "old": old, "new": new, "tags": tags})
    _recover_tag_change()


def rename_tag(old: str, new: str) -> None:
    """Rename across the vocabulary and every claim. Merges if `new` exists.

    A name with nothing left once slugged ("!!", "日本語") is refused: renaming
    to "" would take the topic off every claim and drop its synthesis, which
    is a delete, and the caller asked for a rename.
    """
    new = store.slugify(new)
    if not new:
        raise ValueError("a topic name needs at least one letter or digit")
    with store.vocab_lock():
        current = load_tags()
        tags = [t for t in current if t["name"] != old]
        if new not in {t["name"] for t in tags}:
            renamed = next((dict(t) for t in current if t["name"] == old), {"description": ""})
            renamed["name"] = new
            tags.append(renamed)
        _change_tag(old, new, tags)


def _move_pass(data: dict[str, Any], old: str, new: str | None) -> bool:
    """Carry a topic's recorded pass to its new name, or drop it with the topic.

    A deleted topic loses the pairs it was attached to. Recreating the tag over
    the same unchanged claims produces the same signature, so a pass left on
    file would be skipped and the topic never put back on those pairs.

    Where the new name is a topic of its own — a merge rather than a rename —
    its own pass stands. It was recorded against its own claims and its own
    name, and the old topic's signature, which carries the old name, could
    only say the destination had never been asked. If the merge brings claims
    the destination did not have, its signature no longer matches what a pass
    would compute now, and the pass runs again of its own accord.
    """
    passes = data.get("passes") or {}
    if old not in passes:
        return False
    signature = passes.pop(old)
    if new and new not in passes:
        passes[new] = signature
    data["passes"] = passes
    return True


def _retag_all(old: str, new: str | None) -> None:
    """Rewrite or drop a tag across every paper, every tension's topics, and
    the syntheses. Called holding `vocab_lock`; takes `tensions_lock` and then
    `syntheses_lock` inside it, one after the other, which is the only order
    those are ever held in.

    Papers first, tensions last, and `record_tensions` waits on `vocab_lock`
    for the whole of it: between the two writes the claims carry the new name
    while the tensions still carry the old, and a merge landing there would
    read the old name as one the claims dropped and take it off every tension
    before this loop could convert it. A result that lands after sees the new
    tags and attaches only what the claims still carry."""
    for key in store.paper_keys():
        with store.paper_lock(key):
            try:
                paper = store.load_paper(key)
            except KeyError:
                continue
            touched = False
            for claim in paper.get("claims", []):
                if old not in claim.get("tags", []):
                    continue
                kept = {t for t in claim["tags"] if t != old}
                if new:
                    kept.add(new)
                claim["tags"] = sorted(kept)
                touched = True
            if touched:
                store.save_paper(paper)
    with store.tensions_lock():
        data = store._read_tensions()
        touched = _move_pass(data, old, new)
        for tension in data["tensions"]:
            if old not in tension.get("topics", []):
                continue
            kept = {t for t in tension["topics"] if t != old}
            if new:
                kept.add(new)
            tension["topics"] = sorted(kept)   # a tension with no topics left is still a tension
            touched = True
        if touched:
            store._save_tensions(data)
    with store.agreements_lock():
        data = store._read_agreements()
        touched = _move_pass(data, old, new)
        for record in data["agreements"]:
            if old not in record.get("topics", []):
                continue
            kept = {t for t in record["topics"] if t != old}
            if new:
                kept.add(new)
            record["topics"] = sorted(kept)
            touched = True
        if touched:
            store._save_agreements(data)
    with store.syntheses_lock():
        data = store._read_syntheses()
        record = data["syntheses"].pop(old, None)
        if record is not None:
            # A synthesis is about the topic under whichever name; it moves
            # with a rename and goes with a deletion. Merging into a name that
            # already has one would put two answers to one question on file, so
            # the existing record wins there.
            if new and new not in data["syntheses"]:
                record["topic"] = new
                data["syntheses"][new] = record
            store._save_syntheses(data)


def delete_tag(name: str) -> None:
    with store.vocab_lock():
        _change_tag(name, None, [t for t in load_tags() if t["name"] != name])
