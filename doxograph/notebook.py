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

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import Response
from pydantic import BaseModel, Field, ConfigDict, model_validator

from . import config, storage, store

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
    stance: Literal["supporting", "opposing", "qualifying", "background"] = "background"


def is_question(board: dict) -> bool:
    return (board.get("kind") == "question"
            or any(board.get(field) for field in ("question", "conclusion", "open_questions"))
            or any(entry.get("kind") == "claim" and entry.get("stance", "background") != "background"
                   for entry in board.get("entries", [])))


class Board(Model):
    kind: Literal["board", "question"] = "board"
    id: str
    title: str = Field(min_length=1, max_length=300)
    entries: list[Entry] = Field(default_factory=list)
    question: str = Field(default="", max_length=20000)
    conclusion: str = Field(default="", max_length=100000)
    open_questions: str = Field(default="", max_length=100000)

    @model_validator(mode="after")
    def retain_question_identity(self) -> Board:
        # Migrate earlier questions on read and keep their identity when text or
        # stances are cleared in a later save. Ordinary boards remain boards.
        if is_question(self.model_dump()):
            self.kind = "question"
        return self


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


class Rect(Model):
    # Fractions of the rendered page, so a highlight keeps its place at any zoom.
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(gt=0, le=1)
    h: float = Field(gt=0, le=1)


class Highlight(Model):
    id: str
    paper: PaperKey
    page: int = Field(ge=1, le=100000)
    # A passage is one rectangle per line of selected text; a figure is the one
    # region dragged out on the page, with no text of its own.
    kind: Literal["passage", "figure"] = "passage"
    rects: list[Rect] = Field(min_length=1, max_length=500)
    text: str = Field(default="", max_length=20000)
    color: Literal["yellow", "green", "blue", "pink"] = "yellow"
    note: str = Field(default="", max_length=20000)
    # An open question stays listed under Marks until it is marked answered.
    question: bool = False


class Notebook(Model):
    revision: int = Field(default=0, ge=0)
    notes: str = ""
    boards: list[Board] = Field(default_factory=list)
    searches: list[SavedSearch] = Field(default_factory=list)
    reading: list[Reading] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    highlights: list[Highlight] = Field(default_factory=list)


def load() -> dict:
    return storage.read_model(config.data_dir() / "notebook.json", Notebook).model_dump()


@router.get("")
def read_notebook() -> dict:
    return load()


class NotesUpdate(Model):
    notes: str
    previous: str


@router.put("/notes")
def save_notes(body: NotesUpdate) -> dict:
    # Autosave changes only notes, preserving concurrent board/queue edits.
    # Compare the last saved text so another note writer cannot be overwritten.
    with _lock, store.document_lock("notebook"):
        data = load()
        if data["notes"] == body.notes:
            # Retrying after a lost response is safe if the text reached disk.
            return data
        if data["notes"] != body.previous:
            raise HTTPException(409, "Workspace notes changed in another window. Copy your text before reloading the saved version.")
        data["notes"] = body.notes
        data["revision"] += 1
        store.write_json(config.data_dir() / "notebook.json", data)
        return data


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
                paper_authors=paper.get("authors") or [], paper_year=paper.get("year"),
                paper_notes=paper.get("notes", ""), source=paper.get("source", {}))


@router.put("")
def save_notebook(body: Notebook) -> dict:
    # The revision check avoids overwriting another window's work. The browser
    # keeps the unsaved form on conflict and offers an explicit reload.
    with _lock, store.document_lock("notebook"):
        previous = load()
        if body.revision != previous["revision"]:
            raise HTTPException(409, "Research tools changed in another window. Reload the saved version before trying again.")
        data = body.model_dump()
        for collection in (data["boards"], data["searches"], data["connections"], data["highlights"]):
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
        # Like reading entries, highlights outlive their paper rather than vanish,
        # but a new highlight needs a paper that still exists.
        if any(h["kind"] == "figure" and len(h["rects"]) != 1 for h in data["highlights"]):
            raise HTTPException(422, "A figure mark is a single region of the page")
        old_highlights = {(h["id"], h["paper"]) for h in previous["highlights"]}
        for paper in {h["paper"] for h in data["highlights"] if (h["id"], h["paper"]) not in old_highlights}:
            try:
                store.load_paper(paper)
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
                "note": "", "reviewed": False, "conditions": "", "limitations": "",
                "evidence_origin": "", "evidence_relationship": "unassessed"}
    return {k: copy.deepcopy(claim.get(k, v)) for k, v in defaults.items()}


def record_history(previous: dict, paper: dict) -> None:
    history = copy.deepcopy(previous.get("history", []))
    current = {c["id"]: c for c in paper.get("claims", [])}
    for claim in previous.get("claims", []):
        before = claim_fields(claim)
        after = claim_fields(current[claim["id"]]) if claim["id"] in current else None
        if after is None or {k: v for k, v in before.items() if k != "reviewed"} != {k: v for k, v in after.items() if k != "reviewed"}:
            history.append({"id": uuid.uuid4().hex, "at": store.now(), "claim": claim["id"], "fields": before})
    # Reintroducing an identity from history is a restoration, not a new
    # claim. Keep its previous absence so that restoration can itself be undone.
    prior_ids = {claim["id"] for claim in previous.get("claims", [])}
    historical_ids = {entry["claim"] for entry in previous.get("history", [])}
    for claim_id in current.keys() - prior_ids:
        if claim_id in historical_ids:
            history.append({"id": uuid.uuid4().hex, "at": store.now(),
                            "claim": claim_id, "fields": None})
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
        deleted = entry["claim"] is not None and entry["fields"] is None
        if entry["claim"] is None:
            paper["notes"] = entry["fields"]["notes"]
        elif deleted:
            paper["claims"] = [c for c in paper.get("claims", []) if c["id"] != entry["claim"]]
            store.refresh_status(paper)
        else:
            claim = next((c for c in paper.get("claims", []) if c["id"] == entry["claim"]), None)
            current_review = bool(claim.get("reviewed")) if claim is not None else None
            if claim is None:
                claim = {"id": entry["claim"]}
                paper.setdefault("claims", []).append(claim)
            fields = copy.deepcopy(entry["fields"])
            vocabulary = {tag["name"] for tag in store.load_tags()}
            omitted_topics = sorted(set(fields.get("tags", [])) - vocabulary)
            fields["tags"] = sorted(set(fields.get("tags", [])) & vocabulary)
            if current_review is not None:
                fields["reviewed"] = current_review
            claim.update(fields)
            store.check_quote(key, claim)
            claim["updated"] = store.now()
            store.refresh_status(paper)
        store.save_paper(paper)
        return {"restored": entry["id"], "omitted_topics": omitted_topics, "deleted": deleted}


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
        if is_question(board):
            entries = [
                {"kind": "heading", "text": "Research question"},
                {"kind": "note", "text": board["question"]},
                {"kind": "heading", "text": "Working conclusion"},
                {"kind": "note", "text": board["conclusion"]},
                {"kind": "heading", "text": "Open questions"},
                {"kind": "note", "text": board["open_questions"]},
                *entries,
            ]
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
    header = ["Type", "Text", "Paper", "Authors", "Year", "Claim ID", "Quote", "Page or section", "Evidence", "Topics", "Claim note", "Paper note", "Commentary", "Source", "Conditions", "Limitations", "Evidence origin", "Replication assessment", "Bearing on question"]
    writer.writerow(header)
    for entry in entries:
        kind, text = entry["kind"], entry.get("text", "")
        if kind != "claim":
            lines.extend([("## " if kind == "heading" else "") + md(text), ""])
            writer.writerow([kind, spreadsheet(text)] + [""] * (len(header) - 2))
            continue
        try:
            row = resolve(entry["reference"])
        except HTTPException:
            if not body.board:
                raise
            text = f"Missing source: {entry['reference']['paper']} / {entry['reference']['claim']}\n{text}"
            lines.extend([md(text), ""])
            writer.writerow(["missing", spreadsheet(text)] + [""] * (len(header) - 2))
            continue
        source = row.get("source", {}).get("url", "")
        citation = f"{'; '.join(row['paper_authors'])} ({row['paper_year'] or 'year unknown'}). {row['paper_title']}. {row.get('locator', '')}"
        lines.extend(["### " + md(row.get("text", "")), "", md(citation), "",
                      "> " + md(row.get("quote", "")).replace("\n", "\n> "), ""])
        for label, value in (("Evidence", row.get("evidence")), ("Topics", ", ".join(row.get("tags", []))),
                             ("Conditions", row.get("conditions")), ("Limitations", row.get("limitations")),
                             ("Evidence origin", row.get("evidence_origin")),
                             ("Replication assessment", row.get("evidence_relationship")),
                             ("Bearing on question", entry.get("stance") if body.board and is_question(board) else ""),
                             ("My note", row.get("note")), ("Paper note", row["paper_notes"]),
                             ("Commentary", text), ("Source", source),
                             ("Reference", row["paper"] + " / " + row["id"])):
            if value:
                lines.extend([label + ": " + md(value), ""])
        writer.writerow([spreadsheet(v) for v in ["claim", row.get("text"), row["paper_title"],
            "; ".join(row["paper_authors"]), row["paper_year"], row["id"], row.get("quote"),
            row.get("locator"), row.get("evidence"), ", ".join(row.get("tags", [])), row.get("note"),
            row["paper_notes"], text, source, row.get("conditions"), row.get("limitations"),
            row.get("evidence_origin"), row.get("evidence_relationship"),
            entry.get("stance") if body.board and is_question(board) else ""]])
    is_csv = body.format == "csv"
    filename = (store.slugify(title) or "selected-research") + (".csv" if is_csv else ".md")
    return Response(output.getvalue() if is_csv else "\n".join(lines),
                    media_type="text/csv" if is_csv else "text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


MARK_COLORS = {"yellow": "ffe200", "green": "46d264", "blue": "50a0ff", "pink": "ff64aa"}


def page_box(page, rect: dict) -> tuple[float, float, float, float]:
    """A rectangle in fractions of the page as the reader drew it, in PDF user space.

    The reader draws what PDF.js shows: the crop box, clipped to the media box,
    turned by the page's rotation. The fractions are undone in that order.
    """
    crop, media = page.cropbox, page.mediabox
    left, right = max(float(crop.left), float(media.left)), min(float(crop.right), float(media.right))
    bottom, top = max(float(crop.bottom), float(media.bottom)), min(float(crop.top), float(media.top))
    corners = []
    for u, v in ((rect["x"], rect["y"]), (rect["x"] + rect["w"], rect["y"] + rect["h"])):
        # (a, b) run rightward and downward across the unrotated page.
        a, b = {0: (u, v), 90: (v, 1 - u), 180: (1 - u, 1 - v), 270: (1 - v, u)}[page.rotation % 360]
        corners.append((left + a * (right - left), top - b * (top - bottom)))
    (x1, y1), (x2, y2) = corners
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


@router.get("/annotated/{key}")
def annotated_pdf(key: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]+$", max_length=250)]) -> Response:
    """A copy of the paper's PDF carrying its marks as ordinary PDF annotations.

    Passages become highlights and figures become rectangles; notes and questions
    travel as each annotation's text. The stored PDF is never changed.
    """
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import Highlight as HighlightAnnotation, Rectangle
    from pypdf.constants import AnnotationFlag
    from pypdf.errors import PdfReadError
    from pypdf.generic import ArrayObject, FloatObject, NameObject, TextStringObject

    # Beside the import, as in ingest: pypdf may first load here, after an upgrade.
    # The module is imported here because `fingerprint` below names a paper's digest.
    from . import fingerprint
    fingerprint.snapshot()

    path = store.pdf_path(key)
    if not path.exists():
        raise HTTPException(404, "This paper has no PDF")
    marks = [h for h in load()["highlights"] if h["paper"] == key]
    if not marks:
        raise HTTPException(404, "This paper has no marks to export")
    try:
        writer = PdfWriter(clone_from=PdfReader(path))
    except (PdfReadError, ValueError, OSError):
        raise HTTPException(422, "This PDF could not be read to add annotations")
    skipped = 0
    for mark in sorted(marks, key=lambda h: (h["page"], h["rects"][0]["y"], h["rects"][0]["x"])):
        if mark["page"] > len(writer.pages):
            skipped += 1
            continue
        page = writer.pages[mark["page"] - 1]
        boxes = [page_box(page, r) for r in mark["rects"]]
        bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
        color = MARK_COLORS[mark["color"]]
        if mark["kind"] == "figure":
            annotation = Rectangle(bounds)
            annotation.flags = AnnotationFlag.PRINT
            annotation[NameObject("/C")] = ArrayObject(FloatObject(int(color[i:i + 2], 16) / 255) for i in (0, 2, 4))
        else:
            quads = ArrayObject(FloatObject(n) for x1, y1, x2, y2 in boxes for n in (x1, y2, x2, y2, x1, y1, x2, y1))
            annotation = HighlightAnnotation(rect=bounds, quad_points=quads, highlight_color=color, printing=True)
        contents = ("Question: " if mark["question"] else "") + mark["note"]
        if contents:
            annotation[NameObject("/Contents")] = TextStringObject(contents)
        annotation[NameObject("/T")] = TextStringObject("Doxograph")
        writer.add_annotation(page_number=mark["page"] - 1, annotation=annotation)
    output = io.BytesIO()
    writer.write(output)
    headers = {"Content-Disposition": f'attachment; filename="{key}-annotated.pdf"'}
    if skipped:
        # A mark past the last page belongs to a PDF that has since been replaced.
        headers["X-Doxograph-Skipped-Marks"] = str(skipped)
    return Response(output.getvalue(), media_type="application/pdf", headers=headers)
