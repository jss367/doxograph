"""Provider adapters for the analysis pipeline's structured requests.

Anthropic retains its native PDF/cache support. Other providers receive text
and a JSON schema; none of their requests use Anthropic uploads or credentials.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import anthropic
import httpx
from jsonschema import Draft202012Validator, ValidationError

from . import config


def anthropic_client():
    key = config.api_key("anthropic")
    return anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()


def client():
    selected = config.analysis_config()
    if selected.provider == "anthropic":
        return anthropic_client()
    if not selected.model.strip():
        raise ValueError("Choose a model in Settings before running AI analysis")
    key = config.api_key(selected.provider)
    if selected.provider == "openai" and not key:
        raise ValueError("Set OPENAI_API_KEY in the environment or credentials file before running analysis")
    return TextClient(selected, key)


def create(api, *, model, max_tokens, system, messages, schema, effort):
    """The analysis layer supplies provider-independent request options."""
    if isinstance(api, TextClient):
        return api.create(max_tokens=max_tokens, system=system, messages=messages, schema=schema)
    return api.messages.create(
        model=model, max_tokens=max_tokens, system=system, messages=messages,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )


class TextClient:
    """Translate structured analysis calls to OpenAI chat or Ollama chat."""

    def __init__(self, selected: config.AnalysisConfig, key: str | None):
        self.selected = selected
        self.key = key

    def create(self, *, max_tokens, system, messages, schema):
        prompt = system + "\n\nReturn only JSON matching this schema:\n" + json.dumps(schema)
        chat = [{"role": "system", "content": prompt}]
        for message in messages:
            content = message["content"]
            if isinstance(content, list):
                if any(block.get("type") != "text" for block in content):
                    raise ValueError("This provider accepts extracted paper text only")
                content = "\n\n".join(block["text"] for block in content)
            chat.append({"role": message["role"], "content": content})
        body = {"model": self.selected.model, "messages": chat, "stream": False}
        ollama = self.selected.provider == "ollama"
        if ollama:
            # A byte is a conservative token bound for the supplied text.
            # Reserve space for the answer and chat framing; Ollama otherwise
            # can silently discard the start of an oversized paper.
            if sum(len(m["content"].encode("utf-8")) for m in chat) + max_tokens + 1024 > 65536:
                raise ValueError("This analysis exceeds the local context limit. Select an endpoint with a larger context window.")
            body.update(format=schema, options={"num_predict": max_tokens, "num_ctx": 65536})
            route = "/api/chat"
        else:
            body["response_format"] = {"type": "json_schema", "json_schema": {
                "name": "analysis", "strict": True, "schema": schema}}
            body["max_completion_tokens" if self.selected.provider == "openai" else "max_tokens"] = max_tokens
            route = "/chat/completions"
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        # Local inference can be slow. Do not follow redirects to another host
        # or inherit an HTTP proxy for localhost analysis.
        with httpx.Client(timeout=httpx.Timeout(600, connect=10), trust_env=False) as transport:
            result = transport.post(self.selected.base_url.rstrip("/") + route, json=body, headers=headers)
            result.raise_for_status()
            try:
                data = result.json()
            except ValueError as exc:
                raise ValueError("AI analysis returned invalid JSON; nothing was saved") from exc
        if not isinstance(data, dict):
            raise ValueError("AI analysis returned an unreadable response; nothing was saved")
        if data.get("error"):
            raise RuntimeError(f"{self.selected.provider} could not complete AI analysis")
        try:
            if ollama:
                message = data["message"]
                reason = data.get("done_reason")
                if data.get("done") is not True:
                    raise RuntimeError("AI analysis returned an incomplete response")
                usage = SimpleNamespace(input_tokens=data.get("prompt_eval_count"), output_tokens=data.get("eval_count"))
            else:
                choice = data["choices"][0]
                message, reason = choice["message"], choice["finish_reason"]
                counts = data.get("usage") or {}
                usage = SimpleNamespace(input_tokens=counts.get("prompt_tokens"), output_tokens=counts.get("completion_tokens"))
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
        try:
            payload = json.loads(message.get("content") or "")
            Draft202012Validator(schema).validate(payload)
        except (ValueError, TypeError, ValidationError) as exc:
            raise ValueError("The selected model returned invalid analysis JSON; nothing was saved. "
                             "Use a model and endpoint that support structured JSON output.") from exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(payload))],
                               stop_reason="end_turn", usage=usage)
