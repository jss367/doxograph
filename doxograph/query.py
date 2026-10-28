"""Paginated, workspace-local reads for agents and other API clients."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from . import config, notebook, research_notes, search, store

router = APIRouter(prefix="/api/query", dependencies=[Depends(store.recover_storage)])
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]
QueryText = Annotated[str, Query(max_length=1000)]
# A client that must not read the user's own writing (the in-app agent) asks for
# the personal fields to be left out; nothing that serves it should match on them either.
OmitNotes = Annotated[bool, Query()]
PERSONAL_CLAIM_FIELDS = ("note", "ledger_links")


def matches(q: str, *values) -> bool:
    """Every query word must occur; case and accents follow PDF search."""
    terms = search.terms(q)
    if not terms:
        return True
    text = search.fold(" ".join(str(value or "") for value in values))
    return all(term in text for term in terms)


def page(rows: list[dict], limit: int, offset: int, **extra) -> dict:
    end = offset + limit
    return {"workspace": config.workspace_id(), "items": rows[offset:end],
            "total": len(rows), "offset": offset,
            "next_offset": end if end < len(rows) else None, **extra}


def papers_and_issues() -> tuple[list[dict], list[dict]]:
    issues = []
    papers = store.all_papers(issues=issues)
    # Stable pagination even when imports share the same timestamp.
    papers.sort(key=lambda p: p["key"])
    return papers, issues


@router.get("/papers")
def papers(q: QueryText = "", topic: str = "", label: str = "",
           limit: Limit = 20, offset: Offset = 0) -> dict:
    saved, issues = papers_and_issues()
    rows = [{key: value for key, value in store.summarize(p).items() if key != "notes"} for p in saved
            if (not topic or any(topic in c.get("tags", []) for c in p.get("claims", [])))
            and (not label or label in p.get("labels", []))
            and matches(q, p["key"], p.get("title"), p.get("authors"), p.get("year"),
                        p.get("summary"), p.get("abstract"), p.get("labels"))]
    return page(rows, limit, offset, storage_issues=issues)


@router.get("/papers/{key}")
def paper(key: str, omit_notes: OmitNotes = False) -> dict:
    try:
        saved = store.load_paper(key)
    except KeyError:
        raise HTTPException(404, f"no paper {key}") from None
    # Claims and editing history can be very large. Read claims by paper key
    # through /claims so that a single paper cannot bypass pagination.
    return {"workspace": config.workspace_id(),
            "paper": {**{k: v for k, v in store.summarize(saved).items() if not (omit_notes and k == "notes")},
                      "abstract": saved.get("abstract", ""),
                      "doi": saved.get("doi"), "source": saved.get("source")}}


@router.get("/claims")
def claims(q: QueryText = "", topic: str = "", paper: str = "",
           reviewed: bool | None = None, limit: Limit = 20, offset: Offset = 0,
           omit_notes: OmitNotes = False) -> dict:
    saved, issues = papers_and_issues()
    rows = [c for c in store.claim_rows(saved)
            if (not topic or topic in c.get("tags", []))
            and (not paper or c["paper"] == paper)
            and (reviewed is None or bool(c.get("reviewed")) == reviewed)
            and matches(q, c.get("text"), c.get("evidence"), c.get("quote"),
                        None if omit_notes else c.get("note"), c.get("tags"), c["paper"], c["paper_title"],
                        c["paper_authors"], c["paper_year"])]
    rows.sort(key=lambda c: (c["paper"], c["id"]))
    if omit_notes:
        rows = [{k: v for k, v in c.items() if k not in PERSONAL_CLAIM_FIELDS} for c in rows]
    return page(rows, limit, offset, storage_issues=issues)


@router.get("/topics")
def topics(q: QueryText = "", limit: Limit = 20, offset: Offset = 0) -> dict:
    saved, issues = papers_and_issues()
    counts = store.tag_counts(store.claim_rows(saved))
    rows = [{**tag, "n_claims": counts.get(tag["name"], 0)} for tag in store.load_tags()
            if matches(q, tag["name"], tag.get("description"))]
    rows.sort(key=lambda t: t["name"])
    return page(rows, limit, offset, storage_issues=issues)


@router.get("/evidence")
def evidence(kind: Literal["agreements", "tensions", "syntheses"], topic: str = "",
             limit: Limit = 20, offset: Offset = 0) -> dict:
    saved, issues = papers_and_issues()
    claims = store.claim_rows(saved)
    readers = {"agreements": store.agreement_rows, "tensions": store.tension_rows,
               "syntheses": store.synthesis_rows}
    rows = readers[kind](claims)
    rows = [r for r in rows if not topic or topic in r.get("topics", []) or r.get("topic") == topic]
    # Agreements can cite arbitrarily many claims. Preserve their references;
    # retrieve the full quoted claims through /claims rather than embedding all.
    for row in rows:
        if kind == "syntheses":
            # The store has already computed staleness from these full-text
            # comparison snapshots. They are not part of an agent's answer.
            for field in ("claims", "tensions", "prompt_basis"):
                row.pop(field, None)
        row.pop("fingerprints", None)
        if isinstance(row.get("claims"), list):
            row["claims"] = [{"paper": c["paper"], "id": c["id"]} for c in row["claims"]]
    return page(rows, limit, offset, kind=kind, storage_issues=issues)


def saved_notes() -> list[dict]:
    return [{"id": "workspace", "title": "Workspace notes", "body": notebook.load()["notes"]},
            *research_notes.load()]


@router.get("/notes")
def notes(q: QueryText = "", limit: Limit = 20, offset: Offset = 0) -> dict:
    rows = [{"id": n["id"], "title": n["title"], "length": len(n["body"]),
             "preview": n["body"][:300]} for n in saved_notes()
            if matches(q, n["title"], n["body"])]
    return page(rows, limit, offset)


@router.get("/notes/{note_id}")
def note(note_id: str, offset: Offset = 0,
         limit: Annotated[int, Query(ge=1, le=20000)] = 8000) -> dict:
    saved = next((n for n in saved_notes() if n["id"] == note_id), None)
    if saved is None:
        raise HTTPException(404, "no such note")
    end = offset + limit
    return {"workspace": config.workspace_id(), "id": note_id, "title": saved["title"],
            "body": saved["body"][offset:end], "length": len(saved["body"]), "offset": offset,
            "next_offset": end if end < len(saved["body"]) else None}
