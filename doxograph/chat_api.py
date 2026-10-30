"""Routes for chatting with one paper or with the library agent."""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import tempfile
from pathlib import Path
from collections.abc import Iterator
from typing import Any, Literal

import anthropic
import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from . import agent, ask_context, chat, chats, config, fingerprint, job_queue, store
from .chat_attachments import Attachment, check_attachments
from .model_errors import InputTooLargeError, ModelInputError, ModelSetupError, ProviderResponseError

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=32000)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)


class ChatBody(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    # A saved conversation keeps its own agent; without one, Settings decides.
    agent: Literal["api", "claude_code", "codex"] | None = None

    @field_validator("messages")
    @classmethod
    def conversation(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        check_attachments(messages)
        if any(m.role != ("user" if i % 2 == 0 else "assistant")
               for i, m in enumerate(messages)) or messages[-1].role != "user":
            raise ValueError("Send alternating user and assistant messages, ending with a question.")
        if sum(len(m.content) for m in messages) > 80000:
            raise ValueError("Start a new conversation; this one is too long.")
        return messages


@contextlib.contextmanager
def _chat_errors(label: str) -> Iterator[None]:
    """Map the failures of a model-backed chat to the response the page shows."""
    try:
        yield
    except ProviderResponseError as exc:
        logger.exception(f"{label} received an invalid provider response")
        raise HTTPException(502, str(exc)[:500]) from exc
    except InputTooLargeError as exc:
        raise HTTPException(413, str(exc)[:500]) from None
    except FileNotFoundError:
        raise HTTPException(400, "This paper's PDF is unavailable. Import it again before chatting.") from None
    except ModelInputError as exc:
        raise HTTPException(400, str(exc)[:500]) from None
    except ValueError as exc:
        logger.exception(f"{label} encountered invalid saved data or configuration")
        raise HTTPException(500, f"{label} could not read the saved paper or model configuration. Check your library and provider settings.") from exc
    except ModelSetupError as exc:
        logger.exception(f"{label} model setup failed")
        raise HTTPException(502, str(exc)[:500]) from exc
    except (anthropic.APIError, httpx.HTTPError) as exc:
        logger.exception(f"{label} provider request failed")
        status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            detail = "The model service refused access. Check the selected provider's credentials, then retry."
        elif status == 429:
            detail = "The model service's usage limit was reached. Wait a moment, then retry."
        elif isinstance(exc, (httpx.TimeoutException, anthropic.APITimeoutError)):
            detail = "The model service timed out. Try again."
        else:
            detail = "The model service could not be reached or is unavailable. Check your connection and provider settings, then retry."
        raise HTTPException(502, detail) from exc
    except RuntimeError as exc:
        logger.exception(f"{label} model request failed")
        raise HTTPException(502, "The model could not complete this request. Check its login and usage limits, then retry.") from exc


def _chat_agent(agent: str | None) -> contextlib.AbstractContextManager[None]:
    return config.use_ai_provider(agent) if agent else contextlib.nullcontext()


@router.post("/api/papers/{key}/chat")
def paper_chat(key: str, body: ChatBody) -> dict[str, Any]:
    job_queue._require_analysis()
    with _chat_errors("Paper chat"), _chat_agent(body.agent):
        try:
            return chat.answer(key, [message.model_dump(exclude={"attachments"} if not message.attachments else set()) for message in body.messages])
        except KeyError:
            raise HTTPException(404, f"no paper {key}") from None


def _reachable(url: str) -> bool:
    """Whether this very process answers at `url`, not another service or another Doxograph on that port."""
    try:
        response = httpx.get(f"{url}/api/health", timeout=3, trust_env=False, follow_redirects=False)
        health = response.json() if response.is_success else {}
    except (httpx.HTTPError, ValueError):
        return False
    return isinstance(health, dict) and health.get("app") == "doxograph" and health.get("instance") == fingerprint.INSTANCE


def _callback_url(request: Request) -> str:
    """Where the agent's tools reach this server: a loopback address that answers.

    The MCP adapter talks only to loopback, so it never gets a LAN address. Prefer the loopback
    address this request arrived on (127.0.0.2, ::1); otherwise try 127.0.0.1, which a wildcard
    bind answers. A server bound to one non-loopback address answers on neither, so the check
    fails with setup guidance rather than handing the agent a URL it cannot reach.
    """
    host, port = (request.scope.get("server") or ("", 8765))[:2]
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost"
    host = host if loopback else "127.0.0.1"
    url = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    if not _reachable(url):
        raise ModelSetupError(
            f"Ask the library needs Doxograph to answer on a loopback address, and {url} did not respond. "
            "Start the app with `doxograph serve --host 127.0.0.1` (the default) or `--host 0.0.0.0`.")
    return url


class AgentBody(ChatBody):
    paper: str | None = Field(default=None, max_length=200)


@router.post("/api/agent/chat")
def agent_chat(body: AgentBody, request: Request) -> dict[str, Any]:
    job_queue._require_analysis()
    with _chat_errors("Library agent"), _chat_agent(body.agent):
        return agent.answer([message.model_dump(exclude={"attachments"} if not message.attachments else set()) for message in body.messages],
                            lambda: _callback_url(request), body.paper)


class AskBody(ChatBody):
    scope: ask_context.Scope
    conversation_id: chats.ChatId | None = None


@router.post("/api/ask")
def ask(body: AskBody, request: Request) -> dict[str, Any]:
    """One Ask interface, with scope fixed before either evidence path runs."""
    job_queue._require_analysis()
    with _chat_errors("Ask"), _chat_agent(body.agent), config.use_model_access():
        scope = ask_context.resolve_scope(body.scope)
        prior: list[dict[str, Any]] = []
        saved = chats._load(body.conversation_id) if body.conversation_id else None
        if len(body.messages) > 1:
            if not saved or [(m["role"], m["content"], m.get("attachments", [])) for m in saved["messages"]] != [
                    (m.role, m.content, [a.model_dump() for a in m.attachments]) for m in body.messages[:-1]]:
                raise HTTPException(409, "Save or reopen this conversation before asking a follow-up.")
            previous = saved["messages"][-2].get("scope") or saved.get("scope") or {"mode": "paper" if saved["paper"] else "library",
                                              "papers": [saved["paper"]] if saved["paper"] else scope.papers}
            if ((previous["mode"] == "library" and scope.mode != "library")
                    or not set(previous["papers"]) <= set(scope.papers)):
                raise HTTPException(409, "Start a new conversation when removing sources.")
            unique = {}
            for message in saved["messages"]:
                for item in message.get("evidence") or []:
                    if item["paper"] in scope.papers and set(item.get("sources", [])) <= set(scope.papers) and item.get("text"):
                        unique[item["ref"]] = {**item, "carried": True}
            prior = list(unique.values())
            # Never silently drop historical evidence to squeeze it into the prompt.
            if len(str(prior)) > 100_000:
                raise HTTPException(413, "This conversation has too much evidence. Start a new conversation.")
        if scope.mode != "paper" and config.ai_provider() not in ("claude_code", "codex"):
            raise HTTPException(422, "Selected-source and library retrieval require Claude or ChatGPT subscription. Choose a supported agent.")
        with tempfile.TemporaryDirectory(prefix="doxograph-evidence-") as directory:
            capture = ask_context.Capture(scope, Path(directory) / "evidence.jsonl", prior)
            for focus in scope.focus:
                if focus.claim:
                    paper = store.load_paper(focus.paper)
                    claim = next(c for c in paper["claims"] if c["id"] == focus.claim)
                    capture.direct.extend(ask_context.collect({"items": [{**claim, "paper": focus.paper}]}))
            if capture.remaining() < (1 if scope.mode == "paper" else 0):
                raise HTTPException(413, "Too much evidence for another answer. Start a new conversation or narrow the sources.")
            token = ask_context.active.set(capture)
            try:
                messages = [message.model_dump() for message in body.messages]
                result = (chat.answer(scope.papers[0], messages) if scope.mode == "paper"
                          else agent.answer(messages, lambda: _callback_url(request)))
                current = capture.direct + ask_context.read_log(capture.log)
            finally:
                ask_context.active.reset(token)
        turn = (len(body.messages) + 1) // 2
        evidence = {e["ref"]: e for e in prior}
        evidence.update({e["ref"]: {**e, "turn": turn, "carried": False} for e in current})
        if len(evidence) > 500:
            raise HTTPException(413, "Too much evidence for one answer. Narrow the sources or question.")
        entries = list(evidence.values())
        return {**result, "scope": scope.model_dump(), "evidence": entries,
                "citations": ask_context.citations(result["answer"], entries, scope)}
