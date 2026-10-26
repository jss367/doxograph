"""Workspace-local long-form writing. Never included in model inputs."""
from __future__ import annotations

import hashlib
import io
import re
import uuid
import warnings
from typing import Annotated
from urllib.parse import urljoin

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, RootModel

from . import config, storage, store

router = APIRouter(prefix="/api/research-notes")
NoteId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
AssetId = Annotated[str, Field(pattern=r"^[a-f0-9]{64}\.(png|jpg|gif|webp)$")]
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class NoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=2_000_000)


class NoteUpdate(NoteBody):
    revision: int = Field(ge=1)


class SavedNote(NoteBody):
    model_config = ConfigDict(extra="allow", strict=True)
    id: NoteId
    revision: int = Field(ge=1)
    created: str
    updated: str


class SavedNotes(RootModel[list[SavedNote]]):
    root: list[SavedNote] = Field(default_factory=list)


def load() -> list[dict]:
    return storage.read_model(config.data_dir() / "research-notes.json", SavedNotes).model_dump()


def find(notes: list[dict], note_id: str) -> dict:
    note = next((n for n in notes if n["id"] == note_id), None)
    if note is None:
        raise HTTPException(404, "This research note no longer exists.")
    return note


def check_revision(note: dict, revision: int) -> None:
    if note["revision"] != revision:
        raise HTTPException(409, "This note changed in another window. Your writing is still here. Reload the saved version to compare before saving.")


@router.get("")
def list_notes() -> dict:
    return {"notes": load()}


@router.post("", status_code=201)
def create_note(body: NoteBody) -> dict:
    if not body.title.strip():
        raise HTTPException(422, "Give your note a title.")
    with store.document_lock("research-notes"):
        notes = load()
        note = dict(body.model_dump(), id=uuid.uuid4().hex, revision=1,
                    created=store.now(), updated=store.now())
        notes.append(note)
        store.write_json(config.data_dir() / "research-notes.json", notes)
        return note


@router.put("/{note_id}")
def update_note(note_id: NoteId, body: NoteUpdate) -> dict:
    if not body.title.strip():
        raise HTTPException(422, "Give your note a title.")
    with store.document_lock("research-notes"):
        notes = load()
        note = find(notes, note_id)
        check_revision(note, body.revision)
        note.update(title=body.title, body=body.body, revision=note["revision"] + 1, updated=store.now())
        store.write_json(config.data_dir() / "research-notes.json", notes)
        return note


@router.delete("/{note_id}")
def delete_note(note_id: NoteId, revision: Annotated[int, Query(ge=1)]) -> dict:
    with store.document_lock("research-notes"):
        notes = load()
        check_revision(find(notes, note_id), revision)
        store.write_json(config.data_dir() / "research-notes.json", [n for n in notes if n["id"] != note_id])
        return {"deleted": note_id}


def absolute_research_links(body: str, base_url: str) -> str:
    """Keep Markdown source while making this installation's links portable."""
    destinations = re.compile(
        r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)[\s\S]*?(?<!`)(?P=ticks)(?!`))"
        r"|(?P<prefix>\]\([ \t]*<?|^[ ]{0,3}\[[^\]\n]+\]:[ \t]*<?)"
        r"(?P<url>/(?:api/research-notes/images/|[?#])[^\s)>]*)", re.MULTILINE)

    def replace(match):
        if match.group("code") is not None:
            return match.group()
        return match.group("prefix") + urljoin(base_url, match.group("url"))

    output, prose = [], []
    fence = None
    list_indents = []

    def flush():
        output.append(destinations.sub(replace, "".join(prose)))
        prose.clear()

    for line in body.splitlines(keepends=True):
        # Quote containers do not change whether a fenced example is code.
        normalized = re.sub(r"^(?: {0,3}>[ \t]?)+", "", line)
        indent = len(normalized) - len(normalized.lstrip(" "))
        item = None
        if normalized.strip() and not fence:
            while list_indents and indent < list_indents[-1]:
                list_indents.pop()
            base = list_indents[-1] if list_indents else 0
            item = re.match(r"^ *(?:[-+*]|\d+[.)])[ \t]+", normalized)
            if item and indent - base < 4:
                list_indents.append(item.end())
        if list_indents and indent >= list_indents[-1]:
            normalized = normalized[list_indents[-1]:]
        elif item and list_indents:
            normalized = normalized[item.end():]
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", normalized.rstrip("\r\n"))
        if fence:
            output.append(line)
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
        elif marker:
            flush()
            fence = marker[1]
            output.append(line)
        elif normalized.startswith(("    ", "\t")):
            flush()
            output.append(line)  # Indented code remains a literal example.
        else:
            prose.append(line)
    flush()
    return "".join(output)


def markdown_export(title: str, body: str, request: Request) -> Response:
    filename = (store.slugify(title) or "research-note")[:120] + ".md"
    content = "# " + title + "\n\n" + absolute_research_links(body, str(request.base_url))
    return Response(content, media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/{note_id}/export")
def export_note(note_id: NoteId, request: Request) -> Response:
    note = find(load(), note_id)
    return markdown_export(note["title"], note["body"], request)


@router.post("/export")
def export_draft(body: NoteBody, request: Request) -> Response:
    # Export a conflicting or unsaved draft without changing the stored note.
    return markdown_export(body.title, body.body, request)


@router.post("/images", status_code=201)
async def upload_image(file: UploadFile) -> dict:
    content = await file.read(MAX_IMAGE_BYTES + 1)
    if len(content) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Choose an image smaller than 10 MB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                extension = {"PNG": "png", "JPEG": "jpg", "GIF": "gif", "WEBP": "webp"}.get(image.format)
                if not extension:
                    raise ValueError("Unsupported image format")
                image.verify()
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "Choose a valid PNG, JPEG, GIF, or WebP image.")
    name = hashlib.sha256(content).hexdigest() + "." + extension
    folder = config.data_dir() / "note-images"
    folder.mkdir(parents=True, exist_ok=True)
    # Content-addressed images are immutable and remain when a note is deleted:
    # another note may still embed the same image.
    with store.document_lock("research-notes"):
        path = folder / name
        if not path.exists():
            temporary = folder / (uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(content)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
    return {"url": f"/api/research-notes/images/{name}?workspace={config.workspace_id()}"}


@router.get("/images/{name}")
def read_image(name: AssetId) -> FileResponse:
    path = config.data_dir() / "note-images" / name
    if not path.is_file():
        raise HTTPException(404, "Image no longer exists in this workspace.")
    return FileResponse(path, headers={"X-Content-Type-Options": "nosniff"})
