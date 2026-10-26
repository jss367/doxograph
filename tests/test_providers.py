"""Analysis uses only the selected endpoint, and rejects unsafe partial output."""

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from doxograph import config, extract, providers, quotes, server, store
from pdfs import minimal_pdf


def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def select(provider="ollama", model="local-model", base_url=None):
    config.save_analysis_settings(provider, model, base_url or config.PROVIDERS[provider]["base_url"])


def transport(monkeypatch, answer, *, status=200, reason="stop", during=None):
    requests = []

    def send(request):
        requests.append(request)
        body = json.loads(request.content)
        if during:
            during()
        if status != 200:
            return httpx.Response(status, json={"error": "unavailable"})
        if "/api/chat" in str(request.url):
            response = {"message": {"content": json.dumps(answer)}, "done": True,
                        "done_reason": reason, "prompt_eval_count": 100, "eval_count": 20}
        else:
            response = {"choices": [{"message": {"content": json.dumps(answer)}, "finish_reason": reason}],
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        return httpx.Response(200, json=response)

    real = httpx.Client
    monkeypatch.setattr(providers.httpx, "Client", lambda **kw: real(**kw, transport=httpx.MockTransport(send)))
    monkeypatch.setattr(providers, "anthropic_client", Mock(side_effect=AssertionError("must not contact Anthropic")))
    return requests


def paper(key="study"):
    store.save_paper(store.new_paper(key, title="A study"))
    store.pdf_path(key).write_bytes(minimal_pdf("Steered models recover in half of trials."))
    return key


def payload():
    return {"summary": "A study.", "relevance": "Relevant.", "proposed_tags": [], "claims": [{
        "text": "Steered models recover in half of trials.", "kind": "finding", "strength": "headline",
        "tags": ["recovery"], "evidence": "Half of trials.", "quote": "Steered models recover in half of trials.",
        "locator": "p. 1", "ledger_links": []}]}


@pytest.mark.parametrize("provider", ["ollama", "openai", "openai-compatible"])
def test_extraction_sends_page_text_and_schema_only_to_selected_provider(monkeypatch, provider):
    select(provider)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-test-secret")
    requests = transport(monkeypatch, payload())
    key = paper()
    result = extract.extract_paper(key)
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert body["model"] == "local-model"
    content = body["messages"][1]["content"]
    assert "[Page 1]" in content and "Steered models recover" in content
    assert "thinking" not in body and "output_config" not in body
    assert "anthropic-test-secret" not in str(request.headers)
    assert "api.anthropic.com" not in str(request.url)
    if provider == "ollama":
        assert str(request.url) == "http://localhost:11434/api/chat"
        assert "authorization" not in request.headers
        assert body["format"] == extract.EXTRACTION_SCHEMA
        assert body["stream"] is False
    else:
        assert body["response_format"]["json_schema"]["schema"] == extract.EXTRACTION_SCHEMA
        assert str(request.url).endswith("/v1/chat/completions")
        if provider == "openai":
            assert request.headers["authorization"] == "Bearer openai-test-secret"
            assert body["max_completion_tokens"] == 16000
    assert result["claims"][0]["quote_verified"] is True
    assert "pdf_upload" not in result
    assert result["extraction"]["provider"] == provider
    assert result["extraction"]["model"] == "local-model"
    assert result["extraction"]["usage"]["input_tokens"] == 100


@pytest.mark.parametrize("operation,answer", [
    (extract.retag_paper, {"assignments": []}),
    (extract.find_tensions, {"tensions": []}),
    (extract.find_agreements, {"agreements": []}),
    (extract.synthesize_topic, {"text": "Both papers report recovery."}),
])
def test_all_followup_operations_use_local_provider(monkeypatch, operation, answer):
    select()
    store.add_tag("recovery")
    for key in ("one", "two"):
        store.save_paper(store.new_paper(key))
        store.add_claim(key, {"text": "Recovery happens.", "tags": ["recovery"]})
    requests = transport(monkeypatch, answer)
    operation("one" if operation == extract.retag_paper else "recovery")
    assert len(requests) == 1
    assert str(requests[0].url) == "http://localhost:11434/api/chat"


@pytest.mark.parametrize("answer,reason,error", [
    ({"summary": "Missing claims"}, "stop", "invalid analysis JSON"),
    (payload(), "length", "token limit"),
    (payload(), "content_filter", "refused"),
    (payload(), "tool_calls", "did not finish"),
])
def test_bad_responses_do_not_replace_existing_claims(monkeypatch, answer, reason, error):
    select("openai-compatible")
    key = paper()
    store.add_claim(key, {"text": "Keep this claim."})
    before = store.load_paper(key)
    transport(monkeypatch, answer, reason=reason)
    with pytest.raises((ValueError, RuntimeError), match=error):
        extract.extract_paper(key)
    assert store.load_paper(key) == before


@pytest.mark.parametrize("status", [401, 429, 500])
def test_endpoint_errors_are_not_retried_with_another_provider(monkeypatch, status):
    select()
    key = paper()
    requests = transport(monkeypatch, {}, status=status)
    with pytest.raises(httpx.HTTPStatusError):
        extract.extract_paper(key)
    assert len(requests) == 1
    assert not store.load_paper(key)["claims"]


def test_unreadable_pdf_fails_before_sending_any_text(monkeypatch):
    select()
    key = paper()
    monkeypatch.setattr(quotes, "paper_text", lambda *args, **kwargs: None)
    requests = transport(monkeypatch, payload())
    with pytest.raises(ValueError, match="no readable text"):
        extract.extract_paper(key)
    assert requests == []


def test_long_local_prompt_is_rejected_instead_of_truncated(monkeypatch):
    select()
    key = paper()
    monkeypatch.setattr(quotes, "paper_text", lambda *args, **kwargs: SimpleNamespace(raw="x" * 65536))
    requests = transport(monkeypatch, payload())
    with pytest.raises(ValueError, match="local context limit"):
        extract.extract_paper(key)
    assert not requests


def test_disabled_analysis_never_contacts_local_or_cloud_server(monkeypatch):
    select()
    config.set_ai_enabled(False)
    requests = transport(monkeypatch, payload())
    with pytest.raises(RuntimeError, match="disabled"):
        extract.extract_paper(paper())
    assert not requests


def test_settings_change_during_extraction_keeps_original_provenance(monkeypatch):
    select()
    requests = transport(monkeypatch, payload(), during=lambda: select("openai-compatible", "other"))
    result = extract.extract_paper(paper())
    assert len(requests) == 1
    assert result["extraction"]["provider"] == "ollama"
    assert result["extraction"]["model"] == "local-model"
    assert config.analysis_config().provider == "openai-compatible"


def test_local_deletion_does_not_contact_anthropic_for_old_uploads(monkeypatch):
    select()
    key = paper()
    saved = store.load_paper(key)
    saved["pdf_upload"] = {"file_id": "previous-anthropic-upload"}
    store.save_paper(saved)
    create = Mock(side_effect=AssertionError("must not create cloud client"))
    monkeypatch.setattr(extract, "client", create)
    extract.delete_paper(key)
    create.assert_not_called()
    assert not store.paper_path(key).exists()


def test_provider_settings_persist_and_invalidate_every_workspace_state():
    other = config.create_workspace("Other")
    with client() as c:
        initial = c.get("/api/state")
        changed = c.put("/api/settings", json={"provider": "ollama", "model": "local-model",
                                               "base_url": "http://localhost:11434/"})
        assert changed.status_code == 200
        response = c.get("/api/state", headers={"If-None-Match": initial.headers["etag"]})
        assert response.status_code == 200
        assert response.json()["provider"] == "ollama"
        assert c.get("/api/state", headers={"X-Doxograph-Workspace": other["id"]}).json()["model"] == "local-model"
        c.put("/api/settings", json={"ai_enabled": False})
        assert config.analysis_config().provider == "ollama"
        c.put("/api/settings", json={"provider": "anthropic", "model": "claude-test"})
        assert config.ai_enabled() is False
        assert config.analysis_settings()["providers"]["ollama"]["model"] == "local-model"
    result = subprocess.check_output([sys.executable, "-c", "from doxograph.config import analysis_config; print(analysis_config().model)"], text=True)
    assert result.strip() == "claude-test"


@pytest.mark.parametrize("settings", [
    {"provider": "bogus", "model": "x"},
    {"provider": "ollama", "model": " "},
    {"provider": "ollama", "model": "x", "base_url": "file:///tmp/server"},
    {"provider": "ollama", "model": "x", "base_url": "http://secret:password@localhost"},
    {"provider": "openai", "model": "x", "base_url": "https://other.example/v1"},
])
def test_invalid_settings_leave_previous_selection_unchanged(settings):
    before = config.analysis_config()
    with client() as c:
        assert c.put("/api/settings", json=settings).status_code == 422
    assert config.analysis_config() == before


def test_provider_credentials_are_isolated_and_never_returned_to_browser(monkeypatch, tmp_path):
    credentials = tmp_path / "credentials"
    credentials.write_text('export ANTHROPIC_API_KEY="anthropic-secret"\nOPENAI_API_KEY=openai-secret\nDOXOGRAPH_API_KEY=custom-secret\n')
    monkeypatch.setenv("DOXOGRAPH_CREDENTIALS_FILE", str(credentials))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DOXOGRAPH_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert config.api_key("anthropic") == "anthropic-secret"
    assert config.api_key("openai") == "openai-secret"
    assert config.api_key("openai-compatible") == "custom-secret"
    assert config.api_key("ollama") is None
    select("openai")
    with client() as c:
        state = c.get("/api/state")
        assert state.json()["has_key"] is True
        assert "secret" not in state.text
    assert "secret" not in (config.base_data_dir() / "settings.json").read_text()


def test_provider_and_endpoint_are_in_topic_pass_signatures():
    store.add_tag("recovery")
    first = (extract._pass_extra("recovery", "", ""), store.synthesis_prompt_basis("recovery"))
    select("ollama", config.MODEL)
    second = (extract._pass_extra("recovery", "", ""), store.synthesis_prompt_basis("recovery"))
    assert all(a != b for a, b in zip(first, second))
    select("ollama", config.MODEL, "http://localhost:11435")
    third = (extract._pass_extra("recovery", "", ""), store.synthesis_prompt_basis("recovery"))
    assert all(a != b for a, b in zip(second, third))


@pytest.mark.parametrize("provider", ["ollama", "openai", "openai-compatible"])
def test_environment_can_configure_provider_without_saved_settings(monkeypatch, provider):
    monkeypatch.setenv("DOXOGRAPH_PROVIDER", provider)
    monkeypatch.setenv("DOXOGRAPH_MODEL", "environment-model")
    monkeypatch.setenv("DOXOGRAPH_BASE_URL", "http://localhost:9999/v1")
    assert config.analysis_config() == config.AnalysisConfig(provider, "environment-model", "http://localhost:9999/v1")
    assert config.analysis_settings()["providers"][provider]["model"] == "environment-model"
    select("anthropic", "saved-model")
    assert config.analysis_config() == config.AnalysisConfig("anthropic", "saved-model", "")


def test_openai_missing_key_fails_before_making_a_request(monkeypatch, tmp_path):
    select("openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DOXOGRAPH_CREDENTIALS_FILE", str(tmp_path / "no-credentials"))
    requests = transport(monkeypatch, payload())
    with pytest.raises(ValueError, match="Set OPENAI_API_KEY"):
        extract.extract_paper(paper())
    assert requests == []


@pytest.mark.parametrize("status", [401, 403])
def test_provider_authentication_errors_explain_how_to_fix_analysis(status):
    request = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
    response = httpx.Response(status, request=request)
    job = server._new_job("import")
    job["state"] = "reading"
    server._import_failed(job, httpx.HTTPStatusError("Access refused", request=request, response=response))
    assert "provider's credentials" in job["explanation"]
    assert "upload the PDF" not in job["explanation"]


@pytest.mark.parametrize("entry", ["batch", "server", "cli"])
def test_queued_batch_items_keep_the_original_provider(monkeypatch, entry):
    from doxograph import __main__

    select()
    monkeypatch.setattr(config, "PASS_WORKERS", 1)
    keys = ["first", "second", "third"]
    for key in keys:
        store.save_paper(store.new_paper(key))
        store.add_claim(key, {"text": "Recovery happens."})
    requests = transport(monkeypatch, {"assignments": []},
                         during=lambda: select("openai-compatible", "cloud-model", "https://remote.example/v1"))
    if entry == "server":
        job = server._new_job("retag")
        server._run_retag(job, keys)
        assert job["state"] == "done"
    elif entry == "cli":
        assert __main__.cmd_retag(SimpleNamespace(keys=keys)) == 0
    else:
        results = list(extract.run_concurrently(keys, extract.retag_paper))
        assert all(error is None for _, _, error in results)
    assert len(requests) == len(keys)
    assert all(str(request.url) == "http://localhost:11434/api/chat" for request in requests)
    assert all(json.loads(request.content)["model"] == "local-model" for request in requests)
    # The batch's context does not leak back into the next operation.
    assert config.analysis_config().provider == "openai-compatible"


@pytest.mark.parametrize("provider", ["ollama", "openai", "openai-compatible"])
@pytest.mark.parametrize("endpoint", [
    "http://user:private-secret@localhost:11434",
    "http://localhost:11434?key=private-secret",
    "http://localhost:11434#private-secret",
])
def test_environment_endpoint_secrets_are_rejected_before_exposure(monkeypatch, provider, endpoint):
    monkeypatch.setenv("DOXOGRAPH_PROVIDER", provider)
    monkeypatch.setenv("DOXOGRAPH_MODEL", "model")
    monkeypatch.setenv("DOXOGRAPH_BASE_URL", endpoint)
    requests = transport(monkeypatch, payload())
    key = paper()
    for operation in (config.analysis_config, config.analysis_settings, lambda: extract.extract_paper(key)):
        with pytest.raises(ValueError, match="without credentials, query, or fragment") as caught:
            operation()
        assert "private-secret" not in str(caught.value)
    with TestClient(server.app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as c:
        response = c.get("/api/state")
        assert response.status_code == 500
        assert "private-secret" not in response.text
    assert not requests
    assert store.load_paper(key)["extraction"] is None
    assert "private-secret" not in store.paper_path(key).read_text()


def test_anthropic_ignores_unused_environment_endpoint(monkeypatch):
    monkeypatch.setenv("DOXOGRAPH_BASE_URL", "https://user:private-secret@example.com?key=private-secret")
    settings = config.analysis_settings()
    assert settings["base_url"] == ""
    assert "private-secret" not in json.dumps(settings)
