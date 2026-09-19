"""Reader-owned research tools. No model code or remote services are used here."""
from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import io
import json
import threading
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict

from . import config, store

router = APIRouter(prefix="/api/notebook")
_lock = threading.RLock()
PaperKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=250)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Reference(Model):
    paper: PaperKey
    claim: str = Field(min_length=1, max_length=300)


class Entry(Model):
    id: str
    kind: Literal["claim", "heading", "note"]
    text: str = ""
    reference: Reference | None = None


class Board(Model):
    id: str
    title: str = Field(min_length=1, max_length=300)
    entries: list[Entry] = Field(default_factory=list)


class SavedSearch(Model):
    id: str
    name: str = Field(min_length=1, max_length=300)
    # Store filter values, never executable query fragments or URLs.
    q: str = ""
    paper: str | None = None
    tag: str | None = None
    label: str | None = None
    kind: str = ""
    unreviewed: bool = False
    unverified: bool = False
    group: bool = True


class Reading(Model):
    paper: PaperKey
    status: Literal["queued", "reading", "finished"] = "queued"
    page: int = Field(default=1, ge=1, le=100000)
    bookmarks: list[int] = Field(default_factory=list)


class Connection(Model):
    id: str
    source: Reference
    target: Reference
    relation: Literal["agrees", "contradicts", "qualifies"]
    note: str = ""


class Notebook(Model):
    revision: int = Field(default=0, ge=0)
    boards: list[Board] = Field(default_factory=list)
    searches: list[SavedSearch] = Field(default_factory=list)
    reading: list[Reading] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)


def load() -> dict:
    path = config.data_dir() / "notebook.json"
    if not path.exists():
        return Notebook().model_dump()
    return Notebook.model_validate_json(path.read_text(encoding="utf-8")).model_dump()


@router.get("")
def read_notebook() -> dict:
    return load()


def references(data: dict):
    for board in data["boards"]:
        for entry in board["entries"]:
            if entry["reference"]:
                yield entry["reference"]
    for connection in data["connections"]:
        yield connection["source"]
        yield connection["target"]


def resolve(ref: dict) -> dict:
    try:
        paper = store.load_paper(ref["paper"])
        claim = next(c for c in paper.get("claims", []) if c["id"] == ref["claim"])
    except (KeyError, StopIteration):
        raise HTTPException(404, "A selected claim no longer exists. Refresh your selection.")
    return dict(claim, paper=paper["key"], paper_title=paper.get("title", ""),
                paper_authors=paper.get("authors", []), paper_year=paper.get("year"),
                paper_notes=paper.get("notes", ""), source=paper.get("source", {}))


@router.put("")
def save_notebook(body: Notebook) -> dict:
    # The revision check avoids overwriting another window's work. The browser
    # keeps the unsaved form on conflict and offers an explicit reload.
    with _lock, store._reentrant_file_lock("notebook", config.locks_dir() / "notebook.lock"):
        previous = load()
        if body.revision != previous["revision"]:
            raise HTTPException(409, "Research tools changed in another window. Reload the saved version before trying again.")
        data = body.model_dump()
        for collection in (data["boards"], data["searches"], data["connections"]):
            ids = [item["id"] for item in collection]
            if len(ids) != len(set(ids)):
                raise HTTPException(422, "Duplicate item identifiers")
        for board in data["boards"]:
            ids = [item["id"] for item in board["entries"]]
            if len(ids) != len(set(ids)):
                raise HTTPException(422, "Duplicate board entry identifiers")
            if any((e["kind"] == "claim") != bool(e["reference"]) for e in board["entries"]):
                raise HTTPException(422, "A claim entry must have a source; headings and notes must not.")
        known = {(r["paper"], r["claim"]) for r in references(previous)}
        for ref in references(data):
            # Old references remain visible as missing sources, never silently
            # disappear from a person's board when extraction replaces a claim.
            if (ref["paper"], ref["claim"]) not in known:
                resolve(ref)
        for connection in data["connections"]:
            if connection["source"] == connection["target"]:
                raise HTTPException(422, "Choose two different claims")
        old_papers = {r["paper"] for r in previous["reading"]}
        if len({r["paper"] for r in data["reading"]}) != len(data["reading"]):
            raise HTTPException(422, "A paper may appear only once in the reading queue")
        for reading in data["reading"]:
            if any(p < 1 or p > 100000 for p in reading["bookmarks"]):
                raise HTTPException(422, "Bookmarks must be positive page numbers")
            reading["bookmarks"] = sorted(set(reading["bookmarks"]))
            if reading["paper"] not in old_papers:
                try:
                    store.load_paper(reading["paper"])
                except KeyError:
                    raise HTTPException(404, "Paper no longer exists")
        data["revision"] += 1
        store.write_json(config.data_dir() / "notebook.json", data)
        return data


class Bulk(Model):
    papers: list[PaperKey] = Field(default_factory=list)
    claims: list[Reference] = Field(default_factory=list)
    field: Literal["labels", "tags"]
    mode: Literal["add", "remove", "replace"] = "add"
    values: list[str]


@router.post("/bulk")
def bulk(body: Bulk) -> dict:
    values = store.normalize_labels(body.values)
    refs = [r.model_dump() for r in body.claims]
    keys = sorted(set(body.papers) | ({r["paper"] for r in refs} if body.field == "tags" else set()))
    if not keys or any(not key or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for ch in key) for key in keys):
        raise HTTPException(422, "Select papers or claims first")
    # Vocabulary first, then papers in stable order, matching existing mutators.
    with store.vocab_lock(), contextlib.ExitStack() as stack:
        for key in keys:
            stack.enter_context(store.paper_lock(key))
        try:
            papers = {key: store.load_paper(key) for key in keys}
        except KeyError:
            raise HTTPException(404, "A selected paper no longer exists")
        for ref in refs:
            resolve(ref)  # validate the entire selection before the first write
        if body.field == "tags" and body.mode != "remove":
            known = {t["name"] for t in store.load_tags()}
            if set(values) - known:
                raise HTTPException(422, "Add new topics to the vocabulary before assigning them")
        changed = 0
        for key, paper in papers.items():
            targets = [paper] if body.field == "labels" else [
                c for c in paper.get("claims", []) if key in body.papers or {"paper": key, "claim": c["id"]} in refs]
            for target in targets:
                old = set(target.get(body.field) or [])
                new = set(values) if body.mode == "replace" else old | set(values) if body.mode == "add" else old - set(values)
                if old != new:
                    target[body.field] = sorted(new)
                    changed += 1
            store.save_paper(paper)
        return {"changed": changed}


# History is kept in the same atomic paper write as the edit it records.
# Only editable fields are captured; uploads, credentials and analysis caches
# have no place in the reader's history.
def claim_fields(claim: dict) -> dict:
    defaults = {"text": "", "kind": "finding", "strength": "supporting", "tags": [],
                "evidence": "", "quote": "", "locator": "", "ledger_links": [],
                "note": "", "reviewed": False}
    return {k: copy.deepcopy(claim.get(k, v)) for k, v in defaults.items()}


def record_history(previous: dict, paper: dict) -> None:
    history = copy.deepcopy(previous.get("history", []))
    current = {c["id"]: c for c in paper.get("claims", [])}
    for claim in previous.get("claims", []):
        before = claim_fields(claim)
        after = claim_fields(current[claim["id"]]) if claim["id"] in current else None
        if after is None or {k: v for k, v in before.items() if k != "reviewed"} != {k: v for k, v in after.items() if k != "reviewed"}:
            history.append({"id": uuid.uuid4().hex, "at": store.now(), "claim": claim["id"], "fields": before})
    if previous.get("notes", "") != paper.get("notes", ""):
        history.append({"id": uuid.uuid4().hex, "at": store.now(), "claim": None,
                        "fields": {"notes": previous.get("notes", "")}})
    if history:
        paper["history"] = history


def fingerprint(paper: dict) -> str:
    value = {"claims": paper.get("claims", []), "notes": paper.get("notes", "")}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@router.get("/history/{key}")
def history(key: str) -> dict:
    try:
        paper = store.load_paper(key)
    except KeyError:
        raise HTTPException(404, "Paper no longer exists")
    return {"entries": list(reversed(paper.get("history", []))), "expected": fingerprint(paper),
            "claims": paper.get("claims", []), "notes": paper.get("notes", "")}


class Restore(Model):
    revision: str
    expected: str


@router.post("/history/{key}/restore")
def restore(key: str, body: Restore) -> dict:
    # Vocabulary precedes the paper lock, as it does during topic renames.
    with store.vocab_lock(), store.paper_lock(key):
        try:
            paper = store.load_paper(key)
            entry = next(e for e in paper.get("history", []) if e["id"] == body.revision)
        except (KeyError, StopIteration):
            raise HTTPException(404, "History entry no longer exists")
        if fingerprint(paper) != body.expected:
            raise HTTPException(409, "This paper changed. Reopen history before restoring.")
        omitted_topics = []
        if entry["claim"] is None:
            paper["notes"] = entry["fields"]["notes"]
        else:
            claim = next((c for c in paper.get("claims", []) if c["id"] == entry["claim"]), None)
            if claim is None:
                claim = {"id": entry["claim"]}
                paper.setdefault("claims", []).append(claim)
            fields = copy.deepcopy(entry["fields"])
            vocabulary = {tag["name"] for tag in store.load_tags()}
            omitted_topics = sorted(set(fields.get("tags", [])) - vocabulary)
            fields["tags"] = sorted(set(fields.get("tags", [])) & vocabulary)
            claim.update(fields)
            store.check_quote(key, claim)
            claim["updated"] = store.now()
            store.refresh_status(paper)
        store.save_paper(paper)
        return {"restored": entry["id"], "omitted_topics": omitted_topics}


class Selection(Model):
    claims: list[Reference] = Field(default_factory=list)
    papers: list[PaperKey] = Field(default_factory=list)
    board: str | None = None
    format: Literal["markdown", "csv"] = "markdown"
    title: str = "Selected research"


def md(text) -> str:
    # User text is literal content, not raw HTML or Markdown links.
    value = str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for char in "\\`*_{}[]()#+!|":
        value = value.replace(char, "\\" + char)
    return value


def spreadsheet(value) -> str:
    value = str(value or "")
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r")) else value


@router.post("/export")
def export_selection(body: Selection) -> Response:
    title = body.title
    entries = []
    if body.board:
        board = next((b for b in load()["boards"] if b["id"] == body.board), None)
        if not board:
            raise HTTPException(404, "Board no longer exists")
        title, entries = board["title"], board["entries"]
    else:
        refs = [r.model_dump() for r in body.claims]
        seen = {(r["paper"], r["claim"]) for r in refs}
        for key in body.papers:
            try:
                paper = store.load_paper(key)
            except KeyError:
                raise HTTPException(404, "Paper no longer exists")
            for c in paper.get("claims", []):
                if (key, c["id"]) not in seen:
                    refs.append({"paper": key, "claim": c["id"]})
                    seen.add((key, c["id"]))
            if not paper.get("claims"):
                entries.append({"kind": "note", "text": f"{paper.get('title', key)}\n{paper.get('notes', '')}"})
        entries.extend({"kind": "claim", "reference": r, "text": ""} for r in refs)
    lines = ["# " + md(title), ""]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Type", "Text", "Paper", "Authors", "Year", "Claim ID", "Quote", "Page or section", "Evidence", "Topics", "Claim note", "Paper note", "Commentary", "Source"])
    for entry in entries:
        kind, text = entry["kind"], entry.get("text", "")
        if kind != "claim":
            lines.extend([("## " if kind == "heading" else "") + md(text), ""])
            writer.writerow([kind, spreadsheet(text)] + [""] * 12)
            continue
        try:
            row = resolve(entry["reference"])
        except HTTPException:
            if not body.board:
                raise
            text = f"Missing source: {entry['reference']['paper']} / {entry['reference']['claim']}\n{text}"
            lines.extend([md(text), ""])
            writer.writerow(["missing", spreadsheet(text)] + [""] * 12)
            continue
        source = row.get("source", {}).get("url", "")
        citation = f"{'; '.join(row['paper_authors'])} ({row['paper_year'] or 'year unknown'}). {row['paper_title']}. {row.get('locator', '')}"
        lines.extend(["### " + md(row.get("text", "")), "", md(citation), "",
                      "> " + md(row.get("quote", "")).replace("\n", "\n> "), ""])
        for label, value in (("Evidence", row.get("evidence")), ("Topics", ", ".join(row.get("tags", []))),
                             ("My note", row.get("note")), ("Paper note", row["paper_notes"]),
                             ("Commentary", text), ("Source", source),
                             ("Reference", row["paper"] + " / " + row["id"])):
            if value:
                lines.extend([label + ": " + md(value), ""])
        writer.writerow([spreadsheet(v) for v in ["claim", row.get("text"), row["paper_title"],
            "; ".join(row["paper_authors"]), row["paper_year"], row["id"], row.get("quote"),
            row.get("locator"), row.get("evidence"), ", ".join(row.get("tags", [])), row.get("note"),
            row["paper_notes"], text, source]])
    is_csv = body.format == "csv"
    filename = (store.slugify(title) or "selected-research") + (".csv" if is_csv else ".md")
    return Response(output.getvalue() if is_csv else "\n".join(lines),
                    media_type="text/csv" if is_csv else "text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
