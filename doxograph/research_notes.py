"""Workspace-local long-form writing. Never included in model inputs."""
from __future__ import annotations

import hashlib
import io
import json
import uuid
import warnings
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field

from . import config, store

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


def load() -> list[dict]:
    path = config.data_dir() / "research-notes.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


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
    with store._reentrant_file_lock("research-notes", config.locks_dir() / "research-notes.lock"):
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
    with store._reentrant_file_lock("research-notes", config.locks_dir() / "research-notes.lock"):
        notes = load()
        note = find(notes, note_id)
        check_revision(note, body.revision)
        note.update(title=body.title, body=body.body, revision=note["revision"] + 1, updated=store.now())
        store.write_json(config.data_dir() / "research-notes.json", notes)
        return note


@router.delete("/{note_id}")
def delete_note(note_id: NoteId, revision: Annotated[int, Query(ge=1)]) -> dict:
    with store._reentrant_file_lock("research-notes", config.locks_dir() / "research-notes.lock"):
        notes = load()
        check_revision(find(notes, note_id), revision)
        store.write_json(config.data_dir() / "research-notes.json", [n for n in notes if n["id"] != note_id])
        return {"deleted": note_id}


@router.get("/{note_id}/export")
def export_note(note_id: NoteId) -> Response:
    note = find(load(), note_id)
    filename = (store.slugify(note["title"]) or "research-note")[:120] + ".md"
    return Response("# " + note["title"] + "\n\n" + note["body"], media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
    with store._reentrant_file_lock("research-notes", config.locks_dir() / "research-notes.lock"):
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
