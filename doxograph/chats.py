"""Saved paper and library conversations, one file each.

The page asks the model, then saves the whole conversation here with the
revision it last saw, so a second window cannot silently overwrite a thread.
Each save carries a fresh operation ID. A save whose response was lost may have
landed, so the page lists those IDs with its next save: a conversation last
written by one of them is still this page's own, not another window's.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config, storage, store
from .ask_context import Evidence, Scope
from .chat_attachments import Attachment, check_attachments

router = APIRouter(prefix="/api/chats")
ChatId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
OperationId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
PaperKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")]
# Library conversations use the read-only MCP tools, which only the subscriptions run.
Agent = Literal["api", "claude_code", "codex"]
LIBRARY_AGENTS = ("claude_code", "codex")


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=200_000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)
    assistant: str = Field(default="", max_length=100)
    model: str = Field(default="", max_length=200)
    activity: list[Annotated[str, Field(max_length=1000)]] = Field(default_factory=list, max_length=500)
    created: str = Field(default="", max_length=40)
    scope: Scope | None = None
    evidence: list[Evidence] | None = Field(default=None, max_length=500)
    citations: list[dict] = Field(default_factory=list, max_length=500)
    saved_note: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None
    # The research note's idempotency key, saved before the note is posted.
    note_operation: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")] | None = None


class Conversation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paper: PaperKey | None = None
    agent: Agent
    scope: Scope | None = None
    messages: list[Message] = Field(min_length=2, max_length=200)

    @field_validator("messages")
    @classmethod
    def answered(cls, messages):
        check_attachments(messages)
        if any(m.role != ("user" if i % 2 == 0 else "assistant") for i, m in enumerate(messages)) \
                or messages[-1].role != "assistant":
            raise ValueError("Save alternating questions and answers, ending with an answer.")
        return messages


class ConversationUpdate(Conversation):
    revision: int = Field(ge=0)
    operation: OperationId
    unconfirmed: list[OperationId] = Field(default_factory=list, max_length=20)


class SavedChat(Conversation):
    model_config = ConfigDict(extra="allow", strict=True)
    id: ChatId
    revision: int = Field(ge=1)
    operation: OperationId
    created: str
    updated: str


def _now() -> str:
    """Microseconds, so saves within one second still list newest first."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def chats_dir():
    return config.data_dir() / "chats"


def _path(chat_id: str):
    return chats_dir() / f"{chat_id}.json"


def _regular(path: Path) -> bool:
    """A link or special file is never read: it could name something outside the
    workspace, or block a read made while holding locks."""
    return path.is_file() and not path.is_symlink()


def files() -> list[Path]:
    return [path for path in sorted(chats_dir().glob("*.json"))
            if re.fullmatch(r"[a-f0-9]{32}\.json", path.name) and _regular(path)]


def _load(chat_id: str) -> dict | None:
    path = _path(chat_id)
    return storage.read_model(path, SavedChat).model_dump() if _regular(path) else None


def _shown(chat: dict) -> dict:
    question = re.sub(r"\s+", " ", chat["messages"][0]["content"]).strip() or next(
        (a["name"] for a in chat["messages"][0].get("attachments", [])), "New conversation")
    return dict(chat, title=question if len(question) <= 80 else question[:79].rstrip() + "…")


@router.get("")
def list_chats(paper: Annotated[str | None, Query(max_length=200)] = None, all_scopes: bool = False) -> dict:
    """A paper's conversations, or the library's when no paper is given, newest first."""
    chats, issues = [], []
    # Deletion holds this lock, so no file listed here vanishes before it is read.
    with store.document_lock("chats"):
        for path in files():
            try:
                chat = storage.read_model(path, SavedChat).model_dump()
            except storage.StorageReadError as exc:
                issues.append(str(exc))
                continue
            if all_scopes or chat["paper"] == paper or (paper and paper in (chat.get("scope") or {}).get("papers", [])):
                chats.append(_shown(chat))
    chats.sort(key=lambda c: (c["updated"], c["id"]), reverse=True)
    return {"chats": chats, "issues": issues}


@router.put("/{chat_id}")
def save_chat(chat_id: ChatId, body: ConversationUpdate) -> dict:
    """Create a conversation at revision 0, or replace the revision the page last saw."""
    if body.scope is None and body.paper is None and body.agent not in LIBRARY_AGENTS:
        raise HTTPException(422, "Ask the library needs Claude subscription or ChatGPT subscription.")
    with store.document_lock("chats"):
        saved = _load(chat_id)
        if saved is None and body.revision:
            raise HTTPException(404, "This conversation was deleted. Start a new conversation.")
        if saved is not None and saved["revision"] != body.revision and saved["operation"] not in body.unconfirmed:
            raise HTTPException(409, "This conversation changed in another window. Reopen the chat to load the latest version.")
        if saved is not None and saved["paper"] != body.paper and body.scope is None:
            raise HTTPException(409, "This conversation belongs to a different chat.")
        if saved is None and body.scope is None and body.paper is not None and not store.paper_path(body.paper).is_file():
            raise HTTPException(404, f"no paper {body.paper}")
        now = _now()
        chat = dict(body.model_dump(exclude={"revision", "unconfirmed"}), id=chat_id,
                    revision=(saved["revision"] if saved else 0) + 1,
                    created=saved["created"] if saved else now, updated=now)
        store.write_json(_path(chat_id), chat)
        return _shown(chat)


@router.delete("/{chat_id}")
def delete_chat(chat_id: ChatId, revision: Annotated[int, Query(ge=1)]) -> dict:
    with store.document_lock("chats"):
        saved = _load(chat_id)
        if saved is None:
            raise HTTPException(404, "This conversation was already deleted.")
        if saved["revision"] != revision:
            raise HTTPException(409, "This conversation changed in another window. Reopen the chat to load the latest version.")
        _path(chat_id).unlink()
        return {"deleted": chat_id}
