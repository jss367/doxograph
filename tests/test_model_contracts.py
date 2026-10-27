"""Every model route validates its output before changing saved research."""

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import httpx

from doxograph import claude_code, codex_cli, config, extract, providers, store
from doxograph.analysis_types import ModelResponse, normalize_response
from doxograph.model_errors import ProviderResponseError


def payload():
    return {"summary": "A study.", "relevance": "Relevant.", "claims": [], "proposed_tags": []}


def response(text, **kwargs):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)],
                           stop_reason="end_turn", usage=None, **kwargs)


@pytest.fixture(params=["anthropic", "claude_code", "codex"])
def model_route(request, monkeypatch):
    """Mock only the SDK/CLI boundary, retaining each real adapter."""
    route = request.param

    def configured(answer):
        if route == "anthropic":
            return SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=response(json.dumps(answer)))))
        module = claude_code if route == "claude_code" else codex_cli
        monkeypatch.setattr(module, "executable", lambda: f"/test/{route}")

        def run(args, **kwargs):
            if "status" in args:
                auth = (json.dumps({"loggedIn": True, "authMethod": "claude.ai"})
                        if route == "claude_code" else "Logged in using ChatGPT")
                return subprocess.CompletedProcess(args, 0, auth, "")
            if route == "claude_code":
                final = {"type": "result", "is_error": False, "structured_output": answer,
                         "usage": {"input_tokens": 12, "output_tokens": 3}}
            else:
                Path(args[args.index("--output-last-message") + 1]).write_text(json.dumps(answer))
                final = {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}}
            return subprocess.CompletedProcess(args, 0, json.dumps(final) + "\n", "")

        monkeypatch.setattr(module, "_run", run)
        return module.Client()

    return configured


@pytest.mark.parametrize("bad", [
    {"summary": "Missing claims"},
    {"summary": "Study", "relevance": "", "claims": None, "proposed_tags": []},
    {"summary": "Study", "relevance": "", "claims": [{"text": 42}], "proposed_tags": []},
    {"summary": "Study", "relevance": "", "claims": [], "proposed_tags": [], "reviewed": True},
    ["Not an analysis object"],
])
def test_invalid_analysis_never_replaces_existing_claims_or_writing(model_route, monkeypatch, bad):
    store.save_paper(store.new_paper("study", notes="My writing", labels=["keep"]))
    store.add_claim("study", {"text": "Already reviewed.", "note": "Keep this note", "reviewed": True})
    path = store.paper_path("study")
    before = path.read_bytes()
    monkeypatch.setattr(extract, "client", lambda: model_route(bad))
    monkeypatch.setattr(extract, "paper_document", lambda *args: {"type": "text", "text": "Paper text"})
    with pytest.raises((ProviderResponseError, RuntimeError)):
        extract.extract_paper("study", keep_reviewed=False)
    assert path.read_bytes() == before


def test_all_routes_return_the_shared_response_contract(model_route):
    result = providers.create(model_route(payload()), model="test", max_tokens=100, system="Analyze",
                              messages=[{"role": "user", "content": "Paper text"}],
                              schema=extract.EXTRACTION_SCHEMA, effort="high")
    assert isinstance(result, ModelResponse)
    assert json.loads(result.text) == payload()
    assert result.stop_reason == "end_turn"


def test_refusal_without_sdk_stop_details_is_reported_in_context():
    raw = SimpleNamespace(stop_reason="refusal", content=[], usage=None)
    result = normalize_response(raw)
    with pytest.raises(RuntimeError, match="extraction refused for study"):
        extract._answer(result, "extraction", "study")


@pytest.mark.parametrize("reason", ["tool_use", "pause_turn", "unexpected"])
def test_unfinished_sdk_turns_cannot_be_merged(monkeypatch, reason):
    store.save_paper(store.new_paper("study"))
    before = store.paper_path("study").read_bytes()
    raw = response(json.dumps(payload()))
    raw.stop_reason = reason
    api = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=raw)))
    monkeypatch.setattr(extract, "client", lambda: api)
    monkeypatch.setattr(extract, "paper_document", lambda *args: {"type": "text", "text": "Paper text"})
    with pytest.raises(ProviderResponseError, match="did not finish normally"):
        extract.extract_paper("study")
    assert store.paper_path("study").read_bytes() == before


def test_text_fragments_are_validated_as_one_complete_response():
    raw = SimpleNamespace(stop_reason="end_turn", usage={"input_tokens": 5, "output_tokens": "bad",
        "cached_input_tokens": 2, "cache_creation_input_tokens": True}, content=[
            SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text='{"answer":'),
            SimpleNamespace(type="text", text='"Yes"}')])
    normalized = normalize_response(raw)
    assert extract._answer(normalized, "chat", "study") == {"answer": "Yes"}
    assert normalized.usage.input_tokens == 5
    assert normalized.usage.output_tokens is None
    assert normalized.usage.cache_read_input_tokens == 2
    assert normalized.usage.cache_creation_input_tokens is None


@pytest.mark.parametrize("raw", [None, {}, SimpleNamespace(stop_reason="end_turn", content="bad"),
    SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=None)])])
def test_unreadable_sdk_responses_fail_at_the_boundary(raw):
    with pytest.raises(ProviderResponseError, match="unreadable"):
        normalize_response(raw)


@pytest.mark.parametrize("usage,expected", [
    (None, None), ([], None), ("unavailable", None),
    ({"prompt_tokens": True}, None), ({"prompt_tokens": 12.5}, None), ({"prompt_tokens": 12}, 12),
])
def test_optional_endpoint_usage_cannot_corrupt_or_discard_valid_analysis(monkeypatch, usage, expected):
    real = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": json.dumps(payload())}, "finish_reason": "stop"}],
        "usage": usage,
    }))
    monkeypatch.setattr(providers.httpx, "Client", lambda **kwargs: real(**kwargs, transport=transport))
    api = providers.TextClient(config.AnalysisConfig("openai-compatible", "test", "http://localhost:1234/v1"), None)
    result = providers.create(api, model="test", max_tokens=100, system="Analyze",
                              messages=[{"role": "user", "content": "Paper text"}],
                              schema=extract.EXTRACTION_SCHEMA, effort="high")
    assert json.loads(result.text) == payload()
    assert result.usage.input_tokens == expected
