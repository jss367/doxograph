"""Facets: per-claim questions whose answers are short lists of names.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import yaml

from . import config, storage, store, workspace_metadata


# --- facets ----------------------------------------------------------------
#
# A facet is a question asked of every claim whose answer is a short list of
# names: which models a result was measured on, which datasets, which
# intervention. The reader defines the facets a workspace has, each with a
# description that goes into the extraction prompt, and the values are filled
# in by the model and corrected by hand.
#
# Values are open: a facet has no fixed list, since a corpus about language
# models names a new one with every other paper. What it has instead is
# aliases, written when a reader merges two spellings, so "gpt-4-0613" read
# from the next paper lands on "GPT-4" without anyone asking twice.
#
# Lock order is facets before vocabulary before paper.
#
# Deleting a facet and rewriting its values touch every paper and then the
# definitions, so each is recorded first as an intent, as a topic change is.
# Whoever takes the facets lock next finishes one that was cut short.

def facet_change_path() -> Path:
    return config.data_dir() / "pending-facet-change.json"


@contextlib.contextmanager
def facets_lock() -> Iterator[None]:
    with store.document_lock("facets"):
        _recover_facet_change()
        yield


FACET_CHANGES = ("values", "delete")


def _recover_facet_change() -> None:
    """Finish a recorded facet change; caller holds the facets lock.

    The change is applied to the papers as they are now rather than replayed
    from snapshots, so edits made since survive, and each step is idempotent.
    The definitions are written last and the intent removed after them:
    until then the intent is what explains values the papers already carry.
    """
    path = facet_change_path()
    try:
        try:
            change = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (not isinstance(change, dict) or change.get("version") != 1
                or change.get("kind") not in FACET_CHANGES
                or not isinstance(change.get("facet"), str)
                or not isinstance(change.get("facets"), list)
                or not isinstance(change.get("old"), list)
                or any(not isinstance(key, str) for key in change["old"])
                or not isinstance(change.get("new"), str)):
            raise ValueError("invalid pending facet change")
        # A damaged definitions file must block recovery before papers move.
        _facets_file()
        workspace_metadata.Facets.model_validate({"facets": change["facets"]}, strict=True)
        if change["kind"] == "delete":
            _rewrite_papers(lambda paper: _strip_facet(paper, change["facet"]))
        else:
            old, new = set(change["old"]), change["new"]
            _rewrite_papers(lambda paper: _rewrite_values(paper, change["facet"], old, new))
        _save_facets(change["facets"])
        path.unlink()
        store._sync_directory(path.parent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise store.StorageRecoveryError(
            f"A facet change is unfinished: {exc}. Doxograph will retry automatically. "
            f"Keep {path} until recovery finishes.",
            path=path,
        ) from exc


def _change_facets(kind: str, name: str, facets: list[dict[str, Any]], old: list[str] | None = None,
                   new: str = "") -> None:
    """Record a facet change, then carry it out; caller holds the facets lock."""
    # Refuse known damage before starting: an unreadable paper part way
    # through would leave the change recorded and stuck.
    for key in store.paper_keys():
        try:
            store.load_paper(key)
        except KeyError:
            continue
    workspace_metadata.Facets.model_validate({"facets": facets}, strict=True)
    store.write_json(facet_change_path(), {"version": 1, "kind": kind, "facet": name, "facets": facets,
                                     "old": sorted(old or []), "new": new})
    _recover_facet_change()


def _facets_file() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.facets_path(), workspace_metadata.Facets,
    ).model_dump(exclude_unset=True)


def load_facets() -> list[dict[str, Any]]:
    return _facets_file().get("facets") or []


def _save_facets(facets: list[dict[str, Any]]) -> None:
    """Caller must hold `facets_lock`."""
    payload = _facets_file()
    payload["facets"] = facets
    workspace_metadata.Facets.model_validate(payload, strict=True)
    store.write_atomic(
        config.facets_path(),
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
    )


def facet_basis(facet: dict[str, Any]) -> str:
    """What a facet's values were filled in against. Changing the description
    changes the question, so the values on every paper go stale; aliases do
    not, since they rewrite values already on file as they are added."""
    return hashlib.sha1(json.dumps([facet["name"], facet.get("description", "")]).encode()).hexdigest()[:16]


def facet_value_key(value: str) -> str:
    """What makes two values one: case and spacing, as for labels."""
    return store.label_key(facet_value_text(value))


def facet_spellings(rows: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """For each facet, every value on file by its key, in the spelling the
    corpus uses most. A value read from a new paper takes that spelling, so
    "gpt-4" and "GPT-4" are one value without anyone merging them."""
    counts = facet_counts(rows, facets)
    return {name: {facet_value_key(value): value for value in values} for name, values in counts.items()}


#: What separates the words of a facet value: Python's whitespace and the
#: browser's together, so the page and the store make the same key. The
#: browser counts U+FEFF as whitespace and Python does not; Python counts
#: U+001C to U+001F and U+0085 and the browser does not. The page's
#: `facetValueKey` uses the same set, and commas, which separate values.
FACET_SPACE = re.compile(r"[\s\ufeff,]+")


def facet_value_text(value: str) -> str:
    """A value as it is stored. Commas separate values in the editor, as they
    separate labels, so a value cannot hold one: a comma inside a value read
    from a paper becomes a space, and "Llama 2, 7B" is kept as one value
    rather than split into two by the next save."""
    return FACET_SPACE.sub(" ", value).strip(" ")


def facet_value_keys(counts: dict[str, dict[str, int]]) -> dict[str, str]:
    """Each value on file's key, for the page to use in place of its own, as
    `label_keys` does: its case folding follows the browser's Unicode tables,
    which need not be Python's."""
    return {value: facet_value_key(value) for values in counts.values() for value in values}


def facet_aliases(facet: dict[str, Any]) -> dict[str, str]:
    """A facet's aliases keyed and valued as values are stored, whatever a
    hand edit of the file wrote: a key in any case, a target with a comma."""
    return {facet_value_key(key): facet_value_text(target)
            for key, target in (facet.get("aliases") or {}).items()}


def resolve_facet_value(value: str, aliases: dict[str, str]) -> str:
    """Follow aliases to the value they end on, or "" for a removed one. A
    chain from renames made one after another resolves to its end, and a
    cycle, which only a hand edit can make, stops where it would repeat."""
    seen = set()
    while (key := facet_value_key(value)) in aliases and key not in seen:
        seen.add(key)
        value = aliases[key]
        if not value:
            return ""
    return value


def clean_facet_values(values: Any, facet: dict[str, Any], known: dict[str, str] | None = None) -> list[str]:
    """A facet's values as they are stored: commas and whitespace collapsed,
    aliases applied, one spelling per value, sorted."""
    aliases = facet_aliases(facet)
    cleaned: dict[str, str] = {}
    for raw in values if isinstance(values, list) else []:
        if not isinstance(raw, str) or not (value := facet_value_text(raw)):
            continue
        if not (value := resolve_facet_value(value, aliases)):
            continue
        key = facet_value_key(value)
        cleaned.setdefault(key, known.get(key, value) if known else value)
    return sorted(cleaned.values(), key=lambda value: (facet_value_key(value), value))


def clean_claim_facets(raw: Any, facets: list[dict[str, Any]],
                       known: dict[str, dict[str, str]] | None = None) -> dict[str, list[str]]:
    """The values for each defined facet; a name no facet has is dropped, and
    so is an empty list, which says no more than a missing one."""
    raw = raw if isinstance(raw, dict) else {}
    cleaned = {}
    for facet in facets:
        values = clean_facet_values(raw.get(facet["name"]), facet, (known or {}).get(facet["name"]))
        if values:
            cleaned[facet["name"]] = values
    return cleaned


def set_claim_facets_by_hand(claim: dict[str, Any], raw: Any) -> None:
    """Apply a person's facet values to a claim. Only the facets named in
    `raw` are set, and an empty list clears one; the rest keep what they
    hold, which may be newer than anything the person was shown. A facet
    whose values they changed is recorded as theirs, and no later pass
    overwrites it."""
    facets = load_facets()
    raw = raw if isinstance(raw, dict) else {}
    before = claim.get("facets") or {}
    given = clean_claim_facets(raw, facets)
    after = {name: values for name, values in before.items()
             if name not in raw and any(facet["name"] == name for facet in facets)}
    after.update(given)
    after = {facet["name"]: after[facet["name"]] for facet in facets if facet["name"] in after}
    hand = set(claim.get("facets_hand") or [])
    for facet in facets:
        name = facet["name"]
        if before.get(name, []) != after.get(name, []):
            hand.add(name)
    claim["facets"] = after
    defined = {facet["name"] for facet in facets}
    claim["facets_hand"] = sorted(hand & defined)
    if not claim["facets_hand"]:
        claim.pop("facets_hand")


def claim_needs_facet(claim: dict[str, Any], facet: dict[str, Any]) -> bool:
    """Whether no pass has given this claim values for `facet` under its
    current description, and no person has set them."""
    if facet["name"] in (claim.get("facets_hand") or []):
        return False
    return (claim.get("facet_basis") or {}).get(facet["name"]) != facet_basis(facet)


def mark_facets_filled(claim: dict[str, Any], facets: list[dict[str, Any]]) -> None:
    """Record that a pass gave this claim values for `facets` as they are."""
    claim["facet_basis"] = {**(claim.get("facet_basis") or {}),
                            **{facet["name"]: facet_basis(facet) for facet in facets}}


def stale_facets(paper: dict[str, Any], facets: list[dict[str, Any]]) -> list[str]:
    """The facets some claim on this paper still needs values for. Tracked
    claim by claim rather than for the paper: a claim a pass left alone, such
    as one edited while the model ran or one a re-read kept, stays stale
    until a pass reaches it."""
    claims = paper.get("claims", [])
    return [facet["name"] for facet in facets if any(claim_needs_facet(c, facet) for c in claims)]


def reconcile_claim_facets(claim: dict[str, Any], facets: list[dict[str, Any]]) -> None:
    """Bring facet data from an older copy of a claim, from history or a
    backup, into line with the facets as they are now: values for a deleted
    facet are dropped, aliases and removals made since are applied, and
    ownership and fill records are kept only for facets still defined."""
    defined = {facet["name"] for facet in facets}
    claim["facets"] = clean_claim_facets(claim.get("facets"), facets)
    hand = [name for name in claim.get("facets_hand") or [] if name in defined]
    if hand:
        claim["facets_hand"] = sorted(set(hand))
    else:
        claim.pop("facets_hand", None)
    basis = {name: value for name, value in (claim.get("facet_basis") or {}).items() if name in defined}
    if basis:
        claim["facet_basis"] = basis
    else:
        claim.pop("facet_basis", None)


def facet_counts(rows: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """How many claims carry each value of each facet, commonest first.
    Spellings that differ only in case count as one value, shown as the
    spelling most claims use."""
    result = {}
    for facet in facets:
        name = facet["name"]
        spellings: dict[str, dict[str, int]] = {}
        for row in rows:
            for value in (row.get("facets") or {}).get(name, []):
                seen = spellings.setdefault(facet_value_key(value), {})
                seen[value] = seen.get(value, 0) + 1
        counts = {min(seen, key=lambda value: (-seen[value], value)): sum(seen.values())
                  for seen in spellings.values()}
        result[name] = dict[str, Any](sorted(counts.items(), key=lambda kv: (-kv[1], facet_value_key(kv[0]), kv[0])))
    return result


def stale_facet_papers(papers: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, int]:
    """For each facet, how many papers have claims it has not been filled on."""
    counts = {facet["name"]: 0 for facet in facets}
    for paper in papers:
        for name in stale_facets(paper, facets):
            counts[name] += 1
    return counts


def add_facet(name: str, description: str = "") -> list[dict[str, Any]]:
    name = store.slugify(name)
    if not name:
        raise ValueError("Name the facet with at least one letter or digit.")
    with facets_lock():
        facets = load_facets()
        if any(facet["name"] == name for facet in facets):
            raise ValueError(f"There is already a facet named {name}.")
        facets.append({"name": name, "description": " ".join(description.split())})
        _save_facets(facets)
        return facets


def describe_facet(name: str, description: str) -> list[dict[str, Any]]:
    """Change what a facet asks for. Every paper's values for it go stale."""
    with facets_lock():
        facets = load_facets()
        facet = next((facet for facet in facets if facet["name"] == name), None)
        if facet is None:
            raise KeyError(name)
        facet["description"] = " ".join(description.split())
        _save_facets(facets)
        return facets


def _rewrite_papers(rewrite: Callable[[dict[str, Any]], bool]) -> None:
    """Apply `rewrite` to every paper under its lock, saving the ones it
    changed. Caller holds `facets_lock`."""
    for key in store.paper_keys():
        with store.paper_lock(key):
            try:
                paper = store.load_paper(key)
            except KeyError:
                continue
            if rewrite(paper):
                store.save_paper(paper)


def delete_facet(name: str) -> list[dict[str, Any]]:
    """Remove a facet and its values from every claim.

    The values go first and the definition last, so a delete cut short
    leaves the facet defined with fewer values, which a fill puts right,
    rather than values for a facet nobody can see."""
    with facets_lock():
        if not any(facet["name"] == name for facet in load_facets()):
            raise KeyError(name)
        _change_facets("delete", name, [facet for facet in load_facets() if facet["name"] != name])
        return load_facets()


def _strip_facet(paper: dict[str, Any], name: str) -> bool:
    changed = False
    for claim in paper.get("claims", []):
        changed |= (claim.get("facets") or {}).pop(name, None) is not None
        changed |= (claim.get("facet_basis") or {}).pop(name, None) is not None
        hand = claim.get("facets_hand") or []
        if name in hand:
            claim["facets_hand"] = [n for n in hand if n != name]
            changed = True
    return changed


def _rewrite_values(paper: dict[str, Any], name: str, old_keys: set[str], new: str) -> bool:
    """Put `new` in place of the values `old_keys` names, or drop them when
    `new` is empty. Running it twice changes nothing more."""
    changed = False
    for claim in paper.get("claims", []):
        values = (claim.get("facets") or {}).get(name)
        if not values or not any(facet_value_key(v) in old_keys for v in values):
            continue
        kept = [v for v in values if facet_value_key(v) not in old_keys] + ([new] if new else [])
        cleaned = clean_facet_values(kept, {"name": name})
        if cleaned:
            claim["facets"][name] = cleaned
        else:
            claim["facets"].pop(name)
        changed = True
    return changed


def rewrite_facet_values(name: str, old: list[str], new: str) -> list[dict[str, Any]]:
    """Rename one value, merge several into one, or with an empty `new`
    remove them, on every claim. The old spellings become aliases, so a later
    read writes the new value in their place, or drops them."""
    new = facet_value_text(new)
    with facets_lock():
        facets = load_facets()
        facet = next((facet for facet in facets if facet["name"] == name), None)
        if facet is None:
            raise KeyError(name)
        old_keys = {facet_value_key(value) for value in old if value.strip()}
        aliases = facet_aliases(facet)
        # Merging into a spelling an earlier rename replaced means merging
        # into what it became. Not when that is one of the values being
        # renamed: then this is the rename being undone, and `new` is meant.
        if new:
            resolved = resolve_facet_value(new, aliases)
            if resolved and facet_value_key(resolved) not in old_keys:
                new = resolved
        new_key = facet_value_key(new) if new else None
        old_keys.discard(new_key)
        if not old_keys:
            return facets
        # An alias that pointed at a value being renamed follows it.
        for key, target in aliases.items():
            if target and facet_value_key(target) in old_keys:
                aliases[key] = new
        for key in old_keys:
            aliases[key] = new
        if new_key is not None:
            aliases.pop(new_key, None)
        facet["aliases"] = dict(sorted(aliases.items()))
        _change_facets("values", name, facets, sorted(old_keys), new)
        return load_facets()
