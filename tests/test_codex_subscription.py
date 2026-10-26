"""ChatGPT authentication and text transport for paper work through Codex."""

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from doxograph import chat, codex_cli, config, extract, quotes, store


@pytest.fixture
def codex(monkeypatch):
    config.set_ai_provider('codex')
    monkeypatch.delenv('DOXOGRAPH_CODEX_MODEL', raising=False)
    monkeypatch.setattr(codex_cli, 'executable', lambda: '/test/bin/codex')


def test_codex_sends_page_labeled_text_with_chatgpt_auth_and_no_api_fallback(codex, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'never-use-this')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://wrong.example')
    monkeypatch.setenv('CODEX_API_KEY', 'never-use-this')
    monkeypatch.setenv('CODEX_ACCESS_TOKEN', 'never-use-this')
    store.save_paper(store.new_paper('paper', title='Study', notes='private note'))
    store.pdf_path('paper').write_bytes(b'%PDF')
    monkeypatch.setattr(quotes, 'paper_text', lambda *a, **kw: quotes.build('Page one\fTwenty participants'))
    monkeypatch.setattr(extract, 'api_client', Mock(side_effect=AssertionError('no API fallback')))
    calls = []

    def run(args, **options):
        calls.append((args, options))
        if 'status' in args:
            return subprocess.CompletedProcess(args, 0, '', 'Logged in using ChatGPT\n')
        assert json.loads(Path(args[args.index('--output-schema') + 1]).read_text()) == chat.SCHEMA
        assert 'private note' not in options['input']
        Path(args[args.index('--output-last-message') + 1]).write_text('{"answer":"Twenty (p. 2)."}')
        return subprocess.CompletedProcess(args, 0, json.dumps({
            'type': 'turn.completed', 'usage': {'input_tokens': 10, 'output_tokens': 5}}), '')

    monkeypatch.setattr(subprocess, 'run', run)
    result = chat.answer('paper', [{'role': 'user', 'content': 'How many?'}])
    assert result == {'answer': 'Twenty (p. 2).', 'model': 'Codex default', 'assistant': 'ChatGPT'}
    args, options = calls[-1]
    assert '[PDF page 1]\nPage one' in options['input']
    assert '[PDF page 2]\nTwenty participants' in options['input']
    assert '--ignore-user-config' in args
    assert '--ephemeral' in args
    assert args[args.index('--sandbox') + 1] == 'read-only'
    assert 'forced_login_method="chatgpt"' in args
    assert 'shell_tool' in args and 'multi_agent' in args
    assert '--model' not in args
    assert not any(k.startswith('OPENAI_') for k in options['env'])
    assert 'CODEX_API_KEY' not in options['env'] and 'CODEX_ACCESS_TOKEN' not in options['env']
    assert 'pdf_upload' not in store.load_paper('paper')


@pytest.mark.parametrize('status', ['Not logged in', 'Logged in using an API key'])
def test_codex_rejects_api_or_missing_auth_without_starting_a_turn(codex, monkeypatch, status):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, '', status))
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='will not fall back'):
        codex_cli.Client().create()
    assert run.call_count == 1
    assert 'forced_login_method="chatgpt"' not in run.call_args.args[0]


@pytest.mark.parametrize('event', [
    {'type': 'turn.failed', 'error': {'message': 'Usage limit reached'}},
    {'type': 'error', 'message': 'Session expired'},
    {'type': 'turn.started'},
    {'type': 'turn.completed'},  # A successful turn without a final answer is not enough.
])
def test_codex_reports_failed_or_incomplete_turns(codex, monkeypatch, event):
    monkeypatch.setattr(subprocess, 'run', Mock(side_effect=[
        subprocess.CompletedProcess([], 0, '', 'Logged in using ChatGPT'),
        subprocess.CompletedProcess([], 0, json.dumps(event), '')]))
    with pytest.raises(RuntimeError, match='Codex'):
        codex_cli.Client().create(model='Codex default', system='test', messages=[])


def test_codex_does_not_send_a_scanned_paper_as_empty_text(codex, monkeypatch):
    store.save_paper(store.new_paper('scan'))
    store.pdf_path('scan').write_bytes(b'%PDF')
    monkeypatch.setattr(quotes, 'paper_text', lambda *a, **kw: None)
    run = Mock(side_effect=AssertionError('must not call model'))
    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='no readable text'):
        chat.answer('scan', [{'role': 'user', 'content': 'Question'}])
    run.assert_not_called()


def test_model_changes_with_provider_and_respects_codex_override(monkeypatch):
    monkeypatch.setattr(config, 'MODEL', 'claude-test')
    monkeypatch.setenv('DOXOGRAPH_CODEX_MODEL', 'codex-test')
    assert config.model() == 'claude-test'
    config.set_ai_provider('codex')
    assert config.model() == 'codex-test'
    config.set_ai_enabled(False)
    assert config.ai_provider() == 'codex'
    config.set_ai_provider('claude_code')
    assert config.model() == 'claude-test'
    assert config.ai_enabled() is False


def test_provider_switch_invalidates_passes_and_preserves_inflight_access(codex):
    before = extract._pass_extra('topic', 'description', 'context')

    @config.bind_model_access
    def running():
        config.set_ai_provider('claude_code')
        assert config.ai_provider() == 'codex'
        assert config.model() == 'Codex default'

    running()
    assert config.ai_provider() == 'claude_code'
    assert extract._pass_extra('topic', 'description', 'context') != before


def test_provider_binding_resets_after_failure(codex):
    @config.bind_model_access
    def running():
        config.set_ai_provider('api')
        raise RuntimeError('failed')

    with pytest.raises(RuntimeError, match='failed'):
        running()
    assert config.ai_provider() == 'api'
