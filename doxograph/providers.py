"""Provider adapters for the analysis pipeline's structured requests.

Anthropic retains its native PDF/cache support. Other providers receive text
and a JSON schema; none of their requests use Anthropic uploads or credentials.
"""

from __future__ import annotations

import ipaddress
import json
from typing import Any, cast
from urllib.parse import urlsplit

import anthropic
import httpx
from . import config
from .analysis_types import (JSONSchema, Message, MessageClient, ModelResponse, OutputConfig,
                             TokenUsage, normalize_response, validate_response)
from .model_errors import InputTooLargeError, ModelInputError, ProviderResponseError


def anthropic_client() -> anthropic.Anthropic:
    key = config.api_key("anthropic")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def client() -> MessageClient | TextClient:
    selected = config.analysis_config()
    if selected.provider == "anthropic":
        # The SDK has a larger overloaded request signature. Adapt that one
        # external boundary to the subset shared with the other providers.
        return cast(MessageClient, anthropic_client())
    if not selected.model.strip():
        raise ModelInputError("Choose a model in Settings before running AI analysis")
    key = config.api_key(selected.provider)
    if selected.provider == "openai" and not key:
        raise ModelInputError("Set OPENAI_API_KEY in the environment or credentials file before running analysis")
    return TextClient(selected, key)


def create(api: MessageClient | TextClient, *, model: str, max_tokens: int, system: str,
           messages: list[Message], schema: JSONSchema, effort: str) -> ModelResponse:
    """The analysis layer supplies provider-independent request options."""
    if isinstance(api, TextClient):
        response = api.create(max_tokens=max_tokens, system=system, messages=messages, schema=schema)
    else:
        output: OutputConfig = {"effort": effort, "format": {"type": "json_schema", "schema": schema}}
        response = normalize_response(api.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages,
            thinking={"type": "adaptive"}, output_config=output))
    return validate_response(response, schema)


class TextClient:
    """Translate structured analysis calls to OpenAI chat or Ollama chat."""

    def __init__(self, selected: config.AnalysisConfig, key: str | None) -> None:
        self.selected = selected
        self.key = key

    def create(self, *, max_tokens: int, system: str, messages: list[Message],
               schema: JSONSchema) -> ModelResponse:
        prompt = system + "\n\nReturn only JSON matching this schema:\n" + json.dumps(schema)
        chat: list[dict[str, Any]] = [{"role": "system", "content": prompt}]
        ollama = self.selected.provider == "ollama"
        for message in messages:
            content = message["content"]
            item: dict[str, Any] = {"role": message["role"], "content": content}
            if isinstance(content, list):
                if any(block.get("type") not in ("text", "image") for block in content):
                    raise ModelInputError("This provider accepts extracted paper text only")
                images = [block for block in content if block.get("type") == "image"]
                if not images or ollama:
                    item["content"] = "\n\n".join(block["text"] for block in content if block.get("type") == "text")
                    if images:
                        item["images"] = [block["source"]["data"] for block in images]
                else:
                    item["content"] = [
                        {"type": "image_url", "image_url": {"url":
                            f"data:{block['source']['media_type']};base64,{block['source']['data']}"}}
                        if block.get("type") == "image" else block for block in content]
            chat.append(item)
        body: dict[str, object] = {"model": self.selected.model, "messages": chat, "stream": False}
        if ollama:
            # A byte is a conservative token bound for the supplied text.
            # Reserve space for the answer and chat framing; Ollama otherwise
            # can silently discard the start of an oversized paper.
            if sum(len(m["content"].encode("utf-8")) for m in chat) + max_tokens + 1024 > 65536:
                raise InputTooLargeError("This analysis exceeds the local context limit. Select an endpoint with a larger context window.")
            body.update(format=schema, options={"num_predict": max_tokens, "num_ctx": 65536})
            route = "/api/chat"
        else:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "analysis", "strict": True, "schema": schema}}
            body["max_completion_tokens" if self.selected.provider == "openai" else "max_tokens"] = max_tokens
            route = "/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        # Keep loopback inference off environment proxies. Remote providers
        # still honor HTTPS_PROXY, HTTP_PROXY, NO_PROXY, and certificate settings.
        host = (urlsplit(self.selected.base_url).hostname or "").lower()
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host == "localhost" or host.endswith(".localhost")
        with httpx.Client(timeout=httpx.Timeout(600, connect=10), trust_env=not loopback) as transport:
            result = transport.post(self.selected.base_url.rstrip("/") + route, json=body, headers=headers)
            result.raise_for_status()
            try:
                data = result.json()
            except ValueError as exc:
                raise ProviderResponseError("AI analysis returned invalid JSON; nothing was saved") from exc
        if not isinstance(data, dict):
            raise ProviderResponseError("AI analysis returned an unreadable response; nothing was saved")
        if data.get("error"):
            raise RuntimeError(f"{self.selected.provider} could not complete AI analysis")
        try:
            if ollama:
                message = data["message"]
                reason = data.get("done_reason")
                if data.get("done") is not True:
                    raise RuntimeError("AI analysis returned an incomplete response")
                usage = TokenUsage.from_provider({"input_tokens": data.get("prompt_eval_count"),
                                                  "output_tokens": data.get("eval_count")})
            else:
                choice = data["choices"][0]
                message, reason = choice["message"], choice["finish_reason"]
                counts = data.get("usage")
                counts = counts if isinstance(counts, dict) else {}
                usage = TokenUsage.from_provider({"input_tokens": counts.get("prompt_tokens"),
                                                  "output_tokens": counts.get("completion_tokens")})
            if not isinstance(message, dict):
                raise TypeError("Expected an assistant message")
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI analysis returned an unreadable response") from exc
        if message.get("refusal") or reason == "content_filter":
            raise RuntimeError("AI analysis was refused by the selected model")
        if reason in ("length", "max_tokens"):
            raise RuntimeError("AI analysis was cut off at the token limit; nothing was saved")
        if reason != "stop":
            raise RuntimeError("AI analysis did not finish normally; nothing was saved")
        text = message.get("content")
        if not isinstance(text, str):
            raise ProviderResponseError("AI analysis returned unreadable text; nothing was saved")
        return ModelResponse.from_text(text, usage)
