"""Chat distinguishes local input problems from private provider diagnostics."""

import json
import logging
import subprocess
from unittest.mock import Mock

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from doxograph import chat, claude_code, codex_cli, config, quotes, server, store
from doxograph.model_errors import InputTooLargeError, ModelSetupError


def ask():
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        return client.post('/api/papers/paper/chat', json={
            'messages': [{'role': 'user', 'content': 'What did the study find?'}]})


def test_subscription_chat_reports_scanned_pdf_as_input_error(monkeypatch):
    config.set_ai_provider('codex')
    store.save_paper(store.new_paper('paper'))
    store.pdf_path('paper').write_bytes(b'%PDF')
    monkeypatch.setattr(quotes, 'paper_text', lambda *a, **kw: None)
    run = Mock(side_effect=AssertionError('must not call the model'))
    monkeypatch.setattr(subprocess, 'run', run)
    response = ask()
    assert response.status_code == 400
    assert 'no readable text' in response.json()['detail']
    run.assert_not_called()


def test_subscription_chat_reports_large_pdf_as_payload_too_large(monkeypatch):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('paper'))
    store.pdf_path('paper').write_bytes(b'x' * 7_000_001)
    run = Mock(side_effect=AssertionError('must not call the model'))
    monkeypatch.setattr(subprocess, 'run', run)
    response = ask()
    assert response.status_code == 413
    assert 'input limit' in response.json()['detail']
    run.assert_not_called()


@pytest.mark.parametrize('kind', ['http', 'anthropic', 'cli', 'unexpected-value'])
def test_provider_diagnostics_stay_in_server_logs(monkeypatch, caplog, kind):
    request = httpx.Request('POST', 'https://provider.example/SECRET_DIAGNOSTIC/v1/chat/completions')
    response = httpx.Response(503, request=request)
    error = {
        'http': httpx.HTTPStatusError('SECRET_DIAGNOSTIC', request=request, response=response),
        'anthropic': anthropic.BadRequestError('SECRET_DIAGNOSTIC', response=response, body=None),
        'cli': RuntimeError('SECRET_DIAGNOSTIC at /Users/local/private'),
        'unexpected-value': ValueError('SECRET_DIAGNOSTIC at /Users/local/private'),
    }[kind]
    monkeypatch.setattr(chat, 'answer', Mock(side_effect=error))
    caplog.set_level(logging.ERROR, logger='doxograph.server')
    result = ask()
    assert result.status_code == (500 if kind == 'unexpected-value' else 502)
    assert 'SECRET_DIAGNOSTIC' not in result.text and '/Users/' not in result.text
    assert len(result.json()['detail']) < 500
    assert 'SECRET_DIAGNOSTIC' in caplog.text


@pytest.mark.parametrize('provider,size', [('codex', 600_001), ('claude_code', 9_500_001)])
def test_cli_input_limits_raise_a_validation_exception(monkeypatch, provider, size):
    config.set_ai_provider(provider)
    module = codex_cli if provider == 'codex' else claude_code
    monkeypatch.setattr(module, 'executable', lambda: '/test/bin/model')
    auth = ('', 'Logged in using ChatGPT') if provider == 'codex' else (
        json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'}), '')
    run = Mock(return_value=subprocess.CompletedProcess([], 0, *auth))
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(InputTooLargeError):
        module.Client().create(model=config.model(), system='test', messages=[
            {'role': 'user', 'content': 'x' * size}])
    assert run.call_count == 1  # Only the authentication check, never generation.


def test_safe_setup_instructions_remain_actionable(monkeypatch):
    monkeypatch.setattr(chat, 'answer', Mock(side_effect=ModelSetupError('Run codex login in Terminal.')))
    response = ask()
    assert response.status_code == 502
    assert response.json()['detail'] == 'Run codex login in Terminal.'


def test_corrupt_paper_data_is_a_server_error_without_exposing_its_path(caplog):
    path = store.paper_path('paper')
    path.write_text('{broken JSON')
    caplog.set_level(logging.ERROR, logger='doxograph.server')
    response = ask()
    assert response.status_code == 500
    assert str(path) not in response.text
    assert str(path) in caplog.text
    assert 'saved paper or model configuration' in response.json()['detail']
