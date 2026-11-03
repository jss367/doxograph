"""Claims within a paper: their ids, quotes, edits, and review.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

from . import config, quotes, store


def ensure_claim_seq(paper: dict[str, Any]) -> int:
    """Set and return the paper's claim high-water mark.

    Corpora written before `claim_seq` existed derive it from the ids they
    already hold, including deleted claims retained in edit history. Call this
    on the *whole* paper before filtering its claims:
    deriving it from a subset lets an id belonging to a filtered-out claim be
    issued again, and ids travel in flight.
    """
    seq = paper.get("claim_seq")
    if seq is None:
        used = []
        identifiers = [claim.get("id", "") for claim in paper.get("claims", [])]
        identifiers.extend(entry.get("claim") or "" for entry in paper.get("history", []))
        for identifier in identifiers:
            match = re.search(r"-c(\d+)$", identifier)
            if match:
                used.append(int(match.group(1)))
        seq = max(used, default=0)
        paper["claim_seq"] = seq
    return seq


def next_claim_id(paper: dict[str, Any]) -> str:
    """Allocate a claim id that has never been used on this paper.

    Counting the current claims recycles the id of a deleted one, and ids travel
    in flight: a retag response or a PATCH issued before the deletion is matched
    by id alone and would land on the unrelated replacement.
    """
    seq = ensure_claim_seq(paper) + 1
    paper["claim_seq"] = seq
    return f"{paper['key']}-c{seq}"


def new_claim(paper: dict[str, Any], **fields: Any) -> dict[str, Any]:
    claim = {
        "id": next_claim_id(paper),
        "text": "",
        "kind": "finding",
        "strength": "supporting",
        "tags": [],
        "evidence": "",
        "quote": "",
        "locator": "",
        "ledger_links": [],
        # The reader's own note on this claim: what they make of it, what the
        # extraction got wrong, what to check. Never sent to a model, and not
        # part of `claim_fingerprint`, so writing one stales nothing and
        # cannot put a topic back in the queue for a pass.
        "note": "",
        "reviewed": False,
        # True when the quote was found in the PDF, False when it was not, None
        # when there was nothing to check (no quote, or no readable PDF).
        "quote_verified": None,
        # The page the quote was found on, counted from the front of the file,
        # which is what a reviewer needs to check a locator against.
        "quote_page": None,
        "added": store.now(),
    }
    claim.update(fields)
    return claim


def check_quote(key: str, claim: dict[str, Any]) -> bool | None:
    """Set and return `quote_verified` for one claim against the paper's PDF.

    The search knows where it found the quote, so the page comes along for
    free and is recorded too.
    """
    found = quotes.locate(store.pdf_path(key), claim.get("quote") or "", store.text_path(key),
                          guard=lambda: store.paper_lock(key))
    verified: bool | None = found["found"] if found else None
    claim["quote_verified"] = verified
    claim["quote_page"] = found["page"] if found else None
    return verified


def quote_context(key: str, claim_id: str) -> dict[str, Any]:
    """Where a claim's quote sits in the paper, in the paper's own words.

    The answer to "this quote was not found — then what does the paper say?",
    which is otherwise a hunt through the PDF. It is drawn from the same
    alignment that checks the quote, so the passage it shows is the one the
    check scored.
    """
    paper = store.load_paper(key)
    claim = next((c for c in paper.get("claims", []) if c.get("id") == claim_id), None)
    if claim is None:
        raise KeyError(claim_id)
    quote = claim.get("quote") or ""
    locator = claim.get("locator") or ""
    context: dict[str, Any] = {
        "claim": claim_id, "quote": quote, "locator": locator,
        "locator_page": quotes.locator_page(locator), "diff": [],
    }
    if not quote.strip():
        return {**context, "available": False, "reason": "This claim has no quote."}
    found = quotes.locate(store.pdf_path(key), quote, store.text_path(key), guard=lambda: store.paper_lock(key))
    if found is None:
        reason = ("This paper has no PDF to check against."
                  if not store.pdf_path(key).exists()
                  else "No text could be read from this PDF; it may be a scan.")
        return {**context, "available": False, "reason": reason}
    if found["suggestion"] and found["suggestion"] != quote:
        context["diff"] = quotes.word_diff(quote, found["suggestion"])
    return {**context, **found, "available": True, "reason": ""}


@store._locked
def verify_quotes(key: str) -> dict[str, Any]:
    """Re-check every quote on a paper. For corpora extracted before quotes
    were checked, and for a PDF that arrived after its claims did."""
    paper = store.load_paper(key)
    for claim in paper.get("claims", []):
        check_quote(key, claim)
    store.save_paper(paper)
    return paper


def clean_ledger_links(links: Any) -> list[dict[str, Any]]:
    """Keep only links that name a claim currently in the ledger.

    A model can invent an ID that looks right, and `ledger.yaml` can change
    during a long extraction. Either way the corpus would record a relationship
    to a claim that does not exist and the UI would show the bare ID.
    """
    known = {c["id"] for c in store.load_ledger()}
    cleaned = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        claim = (link.get("claim") or "").strip()
        if claim not in known:
            continue
        cleaned.append({
            "claim": claim,
            "relation": link.get("relation") or config.LEDGER_RELATIONS[-1],
            "note": (link.get("note") or "").strip(),
        })
    return cleaned


CLAIM_FIELDS = {
    "text", "kind", "strength", "tags", "facets", "evidence", "quote", "locator",
    "ledger_links", "note", "reviewed", "conditions", "limitations", "evidence_origin", "evidence_relationship",
}


class TopicConflictError(ValueError):
    """A manual edit still names a topic that was renamed or deleted."""


class EditConflictError(ValueError):
    """The document changed after a guarded editor read it."""


def paper_revision(paper: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(paper, sort_keys=True).encode()).hexdigest()


def check_paper_revision(paper: dict[str, Any], expected_revision: str | None) -> None:
    # Call inside the same paper lock as the update, including claim edits.
    if expected_revision is not None and paper_revision(paper) != expected_revision:
        raise EditConflictError("This paper changed in another window. Refresh it before trying again.")


@contextlib.contextmanager
def _claim_edit_lock(key: str, patch: dict[str, Any]) -> Iterator[None]:
    # Topic edits share the vocabulary lock with renames, before taking the
    # paper lock. Notes and review decisions can still land during recovery.
    # Facet edits take the facets lock first, as a facet's deletion does.
    with (store.facets_lock() if "facets" in patch else contextlib.nullcontext(),
          store.vocab_lock() if "tags" in patch else contextlib.nullcontext()):
        if "tags" in patch:
            try:
                retired = store._retired_topics()
            except (OSError, ValueError) as exc:
                raise store.StorageRecoveryError(f"Cannot validate the selected topics: {exc}") from exc
            stale = set(patch["tags"]) & retired
            if stale:
                raise TopicConflictError(
                    f"Topics were renamed or deleted: {', '.join(sorted(stale))}. "
                    "Update the topic list in your draft and save again."
                )
        with store.paper_lock(key):
            yield


def update_claim(key: str, claim_id: str, patch: dict[str, Any], *,
                 expected_revision: str | None = None) -> dict[str, Any]:
    with _claim_edit_lock(key, patch):
        paper = store.load_paper(key)
        check_paper_revision(paper, expected_revision)
        claim: dict[str, Any]
        for claim in paper.get("claims", []):
            if claim.get("id") == claim_id:
                for field, value in patch.items():
                    if field == "facets":
                        store.set_claim_facets_by_hand(claim, value)
                    elif field in CLAIM_FIELDS:
                        claim[field] = clean_ledger_links(value) if field == "ledger_links" else value
                if "quote" in patch:
                    check_quote(key, claim)
                claim["updated"] = store.now()
                refresh_status(paper)
                store.save_paper(paper)
                return claim
        raise KeyError(claim_id)


def add_claim(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _claim_edit_lock(key, patch):
        paper = store.load_paper(key)
        fields = {k: v for k, v in patch.items() if k in CLAIM_FIELDS and k != "facets"}
        if "ledger_links" in fields:
            fields["ledger_links"] = clean_ledger_links(fields["ledger_links"])
        claim = new_claim(paper, **fields)
        if "facets" in patch:
            store.set_claim_facets_by_hand(claim, patch["facets"])
        # A hand-written claim needs no review, but a blank one is not a claim yet.
        # An explicit `reviewed` in the patch still wins.
        if "reviewed" not in patch:
            claim["reviewed"] = bool(claim["text"].strip())
        if claim.get("quote"):
            check_quote(key, claim)
        paper.setdefault("claims", []).append(claim)
        refresh_status(paper)
        store.save_paper(paper)
        return claim


@store._locked
def review_claims(key: str, reviewed: bool, claim_ids: list[str] | None = None) -> list[str]:
    """Mark a paper's claims reviewed, or unreviewed, in one write.

    Returns the ids it actually changed, which is what an undo needs: setting
    them back must not also unreview a claim the person had already reviewed
    before. `claim_ids` is that undo's other half — a list restricts the change
    to those claims, and one not on the paper is ignored rather than refused,
    since the alternative is failing a bulk action over a claim deleted
    meanwhile.
    """
    paper = store.load_paper(key)
    wanted = set(claim_ids) if claim_ids is not None else None
    changed = []
    for claim in paper.get("claims", []):
        if wanted is not None and claim.get("id") not in wanted:
            continue
        if bool(claim.get("reviewed")) == reviewed:
            continue
        claim["reviewed"] = reviewed
        claim["updated"] = store.now()
        changed.append(claim["id"])
    if changed:
        refresh_status(paper)
        store.save_paper(paper)
    return changed


@store._locked
def delete_claim(key: str, claim_id: str) -> None:
    paper = store.load_paper(key)
    before = len(paper.get("claims", []))
    paper["claims"] = [c for c in paper.get("claims", []) if c.get("id") != claim_id]
    if len(paper["claims"]) == before:
        raise KeyError(claim_id)
    refresh_status(paper)
    store.save_paper(paper)


def refresh_status(paper: dict[str, Any]) -> None:
    claims = paper.get("claims", [])
    if not claims:
        paper["status"] = "extracted" if paper.get("extraction") else "fetched"
    elif all(c.get("reviewed") for c in claims):
        paper["status"] = "reviewed"
    else:
        paper["status"] = "extracted"
