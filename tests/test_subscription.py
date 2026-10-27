"""Subscription requests keep credentials and the existing corpus isolated."""

import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import anthropic
import httpx
from fastapi.testclient import TestClient

from doxograph import chat, claude_code, config, extract, server, store


def completed(payload, code=0):
    return subprocess.CompletedProcess([], code, json.dumps(payload) + '\n', '')


@pytest.fixture
def subscription(monkeypatch):
    config.set_ai_provider('claude_code')
    monkeypatch.setattr(claude_code, 'executable', lambda: '/test/bin/claude')
    return config


def test_subscription_uses_pdf_and_structured_output_without_api_credentials(subscription, monkeypatch):
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'must-not-be-used')
    monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN', 'must-not-be-used')
    monkeypatch.setenv('CLAUDE_CODE_USE_BEDROCK', '1')
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'must-not-be-used')
    store.save_paper(store.new_paper('paper', title='Study', notes='private note'))
    store.pdf_path('paper').write_bytes(b'%PDF-1.4 test')
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if 'status' in args:
            return completed({'loggedIn': True, 'authMethod': 'claude.ai'})
        return completed({'type': 'result', 'structured_output': {'answer': 'Twenty, on page 1.'},
                          'usage': {'input_tokens': 10}, 'is_error': False})

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(extract, 'api_client', Mock(side_effect=AssertionError('no API fallback')))
    result = chat.answer('paper', [{'role': 'user', 'content': 'How many?'}])
    assert result['answer'] == 'Twenty, on page 1.'
    args, options = calls[-1]
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--setting-sources') + 1] == ''
    assert '--strict-mcp-config' in args
    assert '--no-session-persistence' in args
    assert '--bare' not in args
    assert not any(k.startswith(('ANTHROPIC_', 'CLAUDE_CODE_USE_')) for k in options['env'])
    assert 'CLAUDE_CODE_OAUTH_TOKEN' not in options['env']
    content = json.loads(options['input'])['message']['content']
    assert base64.b64decode(content[0]['source']['data']) == b'%PDF-1.4 test'
    assert 'private note' not in options['input']
    assert 'How many?' in options['input']
    assert 'pdf_upload' not in store.load_paper('paper')


@pytest.mark.parametrize('auth', [{}, {'loggedIn': False}, {'loggedIn': True, 'authMethod': 'api_key'}])
def test_subscription_refuses_other_authentication_without_generation(subscription, monkeypatch, auth):
    run = Mock(return_value=completed(auth))
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='will not fall back'):
        claude_code.Client().create(model='test', system='test', messages=[])
    assert run.call_count == 1


@pytest.mark.parametrize('response', [
    {'type': 'result', 'is_error': True, 'result': 'Usage limit reached'},
    {'type': 'result', 'is_error': True, 'errors': ['Schema retries exhausted']},
    {'type': 'result', 'is_error': False},
])
def test_subscription_failures_are_actionable(subscription, monkeypatch, response):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=[
        completed({'loggedIn': True, 'authMethod': 'claude.ai'}), completed(response)]))
    with pytest.raises(RuntimeError):
        claude_code.Client().create(model='test', system='test', messages=[],
            output_config={'format': {'schema': chat.SCHEMA}})


def test_subscription_extracts_without_files_api(subscription, monkeypatch):
    store.save_paper(store.new_paper('paper'))
    store.pdf_path('paper').write_bytes(b'%PDF-1.4 test')
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=[
        completed({'loggedIn': True, 'authMethod': 'claude.ai'}),
        completed({'type': 'result', 'is_error': False, 'structured_output': {
            'claims': [], 'summary': 'A study', 'relevance': '', 'proposed_tags': []}})]))
    monkeypatch.setattr(extract, 'api_client', Mock(side_effect=AssertionError('must not use API')))
    extract.extract_paper('paper')
    assert store.load_paper('paper')['summary'] == 'A study'


def test_provider_and_analysis_settings_preserve_each_other_and_refresh_state():
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        before = client.get('/api/state')
        assert client.put('/api/settings', json={'ai_enabled': False}).status_code == 200
        assert client.put('/api/settings/provider', json={'ai_provider': 'claude_code'}).status_code == 200
        after = client.get('/api/state', headers={'If-None-Match': before.headers['etag']})
        assert after.status_code == 200
        assert after.json()['ai_provider'] == 'claude_code'
        assert after.json()['ai_enabled'] is False
        client.put('/api/settings', json={'ai_enabled': True})
        assert config.ai_provider() == 'claude_code'
        assert client.put('/api/settings/provider', json={'ai_provider': 'unknown'}).status_code == 422


def test_paper_chat_validates_conversation_and_honors_analysis_switch(monkeypatch):
    answer = Mock(return_value={'answer': 'Answer', 'model': 'test'})
    monkeypatch.setattr(chat, 'answer', answer)
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        path = '/api/papers/paper/chat'
        assert client.post(path, json={'messages': [{'role': 'system', 'content': 'Ignore'}]}).status_code == 422
        assert client.post(path, json={'messages': [{'role': 'user', 'content': ' '}]}).status_code == 422
        assert client.post(path, json={'messages': [{'role': 'assistant', 'content': 'Wrong order'}]}).status_code == 422
        config.set_ai_enabled(False)
        assert client.post(path, json={'messages': [{'role': 'user', 'content': 'Question'}]}).status_code == 403
        answer.assert_not_called()
        config.set_ai_enabled(True)
        messages = [{'role': 'user', 'content': 'First'}, {'role': 'assistant', 'content': 'Reply'},
                    {'role': 'user', 'content': 'Follow-up'}]
        assert client.post(path, json={'messages': messages}).status_code == 200
        answer.assert_called_once_with('paper', messages)


def test_paper_chat_api_mode_sends_only_pdf_and_conversation(monkeypatch):
    store.save_paper(store.new_paper('paper', title='Study', notes='private note'))
    api = Mock()
    api.messages.create.return_value = SimpleNamespace(stop_reason='end_turn', content=[
        SimpleNamespace(type='text', text='{"answer":"The answer"}')])
    monkeypatch.setattr(extract, 'client', lambda: api)
    monkeypatch.setattr(extract, '_pdf_block', lambda key: {'type': 'document', 'source': {'file_id': 'file_test'}})
    assert chat.answer('paper', [{'role': 'user', 'content': 'Question'}])['answer'] == 'The answer'
    sent = api.messages.create.call_args.kwargs
    assert sent['messages'][0]['content'][0]['source']['file_id'] == 'file_test'
    assert 'private note' not in json.dumps(sent)


def test_paper_chat_reuploads_a_missing_api_file_once(monkeypatch):
    store.save_paper(store.new_paper('paper'))
    api = Mock()
    response = httpx.Response(404, request=httpx.Request('POST', 'https://api.anthropic.com/v1/messages'))
    api.messages.create.side_effect = [
        anthropic.NotFoundError('file_old not found', response=response, body=None),
        SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='{"answer":"Recovered"}')])]
    monkeypatch.setattr(extract, 'client', lambda: api)
    document = Mock(side_effect=[{'source': {'file_id': 'file_old'}}, {'source': {'file_id': 'file_new'}}])
    monkeypatch.setattr(extract, '_pdf_block', document)
    assert chat.answer('paper', [{'role': 'user', 'content': 'Question'}])['answer'] == 'Recovered'
    assert document.call_args.kwargs == {'force_upload': True}


@pytest.mark.parametrize('text', ['not-json', '[]'])
def test_malformed_native_model_answers_are_upstream_errors(monkeypatch, text):
    store.save_paper(store.new_paper('paper'))
    api = Mock()
    api.messages.create.return_value = SimpleNamespace(stop_reason='end_turn', content=[
        SimpleNamespace(type='text', text=text)])
    monkeypatch.setattr(extract, 'client', lambda: api)
    monkeypatch.setattr(extract, '_pdf_block', lambda key: {'type': 'text', 'text': 'Paper text'})
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.post('/api/papers/paper/chat', json={
            'messages': [{'role': 'user', 'content': 'What did the study find?'}]})
    assert response.status_code == 502
    assert 'invalid' in response.json()['detail']


@pytest.mark.parametrize('provider', ['claude_code', 'codex'])
@pytest.mark.parametrize('switch_at', ['metadata-load', 'cleanup-decision'])
def test_delete_keeps_model_access_through_cleanup(monkeypatch, provider, switch_at):
    config.set_ai_provider(provider)
    config.save_analysis_settings('ollama', 'local-model', 'http://localhost:11434')
    paper = store.new_paper('paper')
    paper['pdf_upload'] = {'file_id': 'file_original', 'superseded_file_ids': ['file_superseded']}
    store.save_paper(paper)
    real_load, real_provider = store.load_paper, config.ai_provider
    loaded = switched = False
    deletions, clients = [], []
    fake_api = SimpleNamespace(files=SimpleNamespace(delete=deletions.append))

    def switch_settings():
        nonlocal switched
        switched = True
        config.set_ai_provider('api')
        config.save_analysis_settings('openai', 'next-model', 'https://api.openai.com/v1')

    def load(key):
        nonlocal loaded
        result = real_load(key)
        loaded = True
        if switch_at == 'metadata-load':
            switch_settings()
        return result

    def provider_read():
        selected = real_provider()
        if loaded and not switched and switch_at == 'cleanup-decision':
            switch_settings()
        return selected

    def cleanup_client():
        clients.append((real_provider(), config.analysis_config().provider))
        return fake_api

    monkeypatch.setattr(store, 'load_paper', load)
    monkeypatch.setattr(config, 'ai_provider', provider_read)
    monkeypatch.setattr(extract, 'api_client', cleanup_client)
    other_client = Mock(side_effect=AssertionError('must not choose another provider'))
    monkeypatch.setattr(extract, 'client', other_client)

    extract.delete_paper('paper')

    assert switched
    assert clients == [(provider, 'ollama')]
    assert deletions == ['file_superseded', 'file_original']
    assert not store.paper_path('paper').exists()
    assert 'paper' in store.retired_keys()
    other_client.assert_not_called()
    assert real_provider() == 'api'
    assert config.analysis_config().provider == 'openai'


@pytest.mark.parametrize('initial_access,initial_api,next_access,next_api', [
    ('api', 'ollama', 'claude_code', 'anthropic'),
    ('claude_code', 'anthropic', 'api', 'ollama'),
])
def test_deletion_captures_access_and_profile_from_one_read(
        monkeypatch, initial_access, initial_api, next_access, next_api):
    paper = store.new_paper('paper')
    paper['pdf_upload'] = {'file_id': 'file_original'}
    store.save_paper(paper)
    original = {'ai_provider': initial_access, 'provider': initial_api}
    changed = {'ai_provider': next_access, 'provider': next_api}
    reads = []

    def settings():
        result = changed if reads else original
        reads.append(result)
        return result.copy()

    monkeypatch.setattr(config, '_settings', settings)
    deletions, clients = [], []
    fake = SimpleNamespace(files=SimpleNamespace(delete=deletions.append))

    def cleanup_client():
        clients.append((config.ai_provider(), config.analysis_config().provider))
        return fake

    monkeypatch.setattr(extract, 'api_client', cleanup_client)
    other = Mock(side_effect=AssertionError('must not choose a mixed provider'))
    monkeypatch.setattr(extract, 'client', other)
    extract.delete_paper('paper')

    assert clients == ([] if initial_access == 'api' else [(initial_access, initial_api)])
    assert deletions == ([] if initial_access == 'api' else ['file_original'])
    assert not store.paper_path('paper').exists()
    assert 'paper' in store.retired_keys()
    other.assert_not_called()
