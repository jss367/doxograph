"""The shared Ask panel keeps source changes explicit and answers verifiable."""
import asyncio
import re
import socket
import threading
import time
from contextlib import contextmanager

import uvicorn
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import agent, ask_context, config, mcp_server, server, store
from pdfs import minimal_pdf


@pytest.fixture(autouse=True)
def block_live_providers(monkeypatch):
    from doxograph import claude_code, codex_cli, extract
    def blocked(*args, **kwargs):
        pytest.fail('Ask browser tests must mock every provider entry point')
    monkeypatch.setattr(claude_code, '_run', blocked)
    monkeypatch.setattr(codex_cli, '_run', blocked)
    monkeypatch.setattr(extract, 'client', blocked)


@contextmanager
def _server():
    # An in-process server keeps the model stubs active; no live provider is used.
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen()
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(server.app, log_level='warning', lifespan='off'))
    worker = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not http.started:
            if time.monotonic() > deadline:
                raise RuntimeError('Test server failed to start')
            time.sleep(.01)
        yield f'http://127.0.0.1:{port}'
    finally:
        http.should_exit = True
        worker.join(timeout=10)


def seed():
    for key, title in [('a', 'Memory and recovery'), ('b', 'Recovery under intervention'), ('c', 'A separate study')]:
        paper = store.new_paper(key, title=title, authors=['Alex Smith'], year=2026)
        claim = store.new_claim(paper, text=f'{title} reports improvement.', quote='Recovery improved.', tags=['recovery'])
        paper['claims'].append(claim)
        store.save_paper(paper)
        store.pdf_path(key).write_bytes(minimal_pdf(['Recovery improved.', 'The sample was small.']))
    config.set_ai_provider('codex')


def model_stub(monkeypatch):
    from doxograph import chat
    monkeypatch.setattr(chat, 'answer', lambda *a, **kw: pytest.fail('Unexpected direct-paper provider call'))
    calls = []

    def answer(messages, callback):
        capture = ask_context.active.get()
        calls.append((capture.scope.model_dump(), messages))
        async def retrieve():
            mcp = mcp_server.create_server(workspace=config.workspace_id(), papers=capture.scope.papers,
                evidence_log=str(capture.log), without_notes=True, transport=httpx.ASGITransport(app=server.app))
            return (await mcp.call_tool('search_claims', {'workspace': config.workspace_id()})).structured_content
        data = asyncio.run(retrieve())
        ref = data['evidence'][0]['ref']
        return {'answer': f'The selected studies report improvement [[e:{ref}]]. Check the conditions [[e:unknown]]. [Outside source](/pdf/c).',
                'assistant':'ChatGPT', 'model':'test', 'activity':['Searched selected claims']}
    monkeypatch.setattr(agent, 'answer', answer)
    return calls


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_selection_citations_notes_and_narrowing(engine, monkeypatch):
    seed()
    calls = model_stub(monkeypatch)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-select-paper="a"]').first.check()
                await page.locator('[data-select-paper="b"]').first.check()
                await page.locator('#ask-selection').click()
                await expect(page.locator('#agent-scope')).to_contain_text('Memory and recovery')
                await expect(page.locator('#agent-scope')).to_contain_text('Recovery under intervention')
                await page.locator('#agent-question').fill('Compare the findings')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
                assert calls[0][0]['papers'] == ['a', 'b']
                await expect(page.locator('.citation-warning')).to_contain_text('unverified reference')
                assert await page.locator('#agent-messages a[href="/pdf/c"]').count() == 0
                await expect(page.locator('#agent-messages')).to_contain_text('Unverified link:')
                await page.locator('.citation-link').click()
                await expect(page.locator('#citation-body')).to_contain_text('Memory and recovery reports improvement.')
                await expect(page.locator('#citation-body a')).to_have_attribute('href', re.compile(r'.*sel='))
                await page.locator('#citation-close').click()
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                notes = httpx.get(url + '/api/research-notes').json()['notes']
                assert len(notes) == 1 and 'Memory and recovery reports improvement.' in notes[0]['body']
                # Add a third paper through the keyboard-accessible picker.
                await page.locator('#agent-add').click()
                await page.locator('#source-search').fill('separate')
                await page.locator('[data-source-paper="c"]').focus()
                await page.keyboard.press('Space')
                await page.locator('#source-apply').click()
                await page.locator('#agent-question').fill('What about the third paper?')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
                assert calls[1][0]['papers'] == ['a', 'b', 'c']
                assert len(calls[1][1]) == 3
                await expect(page.locator('#agent-messages')).to_contain_text('Added A separate study')
                # Removing evidence starts fresh and retains only the unsent draft.
                await page.locator('#agent-question').fill('Keep this draft')
                await page.locator('[data-remove-source="c"]').click()
                await page.get_by_role('button', name='Start new conversation', exact=True).click()
                await expect(page.locator('#agent-messages')).to_be_empty()
                await expect(page.locator('#agent-question')).to_have_value('Keep this draft')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
                assert len(calls[-1][1]) == 1 and calls[-1][0]['papers'] == ['a', 'b']
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/ask-selected-sources.png')
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_claim_focus_expansion_provider_choice_and_empty_selection(engine, monkeypatch):
    seed()
    config.set_ai_provider('api')
    calls = model_stub(monkeypatch)
    claim = store.load_paper('a')['claims'][0]

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1000, 'height':1000})
            with _server() as url:
                await page.goto(url + '/#paper=a')
                await page.locator(f'[data-ask-claim="{claim["id"]}"]').first.click()
                await expect(page.locator('#agent-scope')).to_contain_text(claim['text'])
                await page.locator('#agent-question').fill('What supports this claim?')
                await page.locator('#agent-expand').click()
                await expect(page.locator('#agent-agent')).to_have_value('api')
                await expect(page.locator('#agent-send')).to_be_disabled()
                await expect(page.locator('#agent-status')).to_contain_text('Choose a supported agent')
                await page.locator('#agent-agent').select_option('codex')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
                assert calls[0][0]['mode'] == 'library'
                assert calls[0][0]['focus'][0]['claim'] == claim['id']
                await page.locator('#agent-close').click()
                # Contextual entry always starts fresh, even when related chats exist.
                await page.locator(f'[data-ask-claim="{claim["id"]}"]').first.click()
                await expect(page.locator('#agent-messages')).to_be_empty()
                await page.locator('[data-remove-source="a"]').click()
                await expect(page.locator('#agent-send')).to_be_disabled()
                await expect(page.locator('#agent-scope')).not_to_contain_text('entire library')
                assert len(calls) == 1
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_full_paper_answers_save_notes_on_a_narrow_window(engine, monkeypatch):
    from doxograph import extract
    from doxograph.providers import ModelResponse
    import json
    seed()
    config.set_ai_provider('api')
    monkeypatch.setattr(extract, 'client', lambda: object())
    monkeypatch.setattr(extract, 'paper_document', lambda key, api: {'type':'text', 'text':'[Page 1] Recovery improved.\n[Page 2] The sample was small.'})
    def respond(api, **kwargs):
        document = next(e for e in ask_context.active.get().direct if e['kind'] == 'document')
        return ModelResponse.from_text(json.dumps({'answer':f"The sample was small [[e:{document['ref']}:2]]."}))
    monkeypatch.setattr(extract, '_create', respond)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':390, 'height':844})
            with _server() as url:
                await page.goto(url + '/#paper=a')
                await page.get_by_role('button', name='Ask about this paper', exact=True).click()
                await page.locator('#agent-question').fill('What are the limitations?')
                await page.locator('#agent-send').click()
                await page.locator('.citation-link').click()
                await expect(page.locator('#citation-body')).to_contain_text('The sample was small.')
                await expect(page.locator('#citation-body a')).to_have_attribute('href', re.compile(r'/pdf/a\?workspace=default#page=2'))
                await page.locator('#citation-close').click()
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                notes = httpx.get(url + '/api/research-notes').json()['notes']
                assert len(notes) == 1 and 'The sample was small.' in notes[0]['body']
                assert await page.locator('#library-agent').evaluate('el => el.scrollWidth <= el.clientWidth')
                if engine == 'chromium':
                    await page.screenshot(path='.context/ask-paper-narrow.png')
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_scope_edits_during_a_request_apply_only_to_the_next_question(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release = asyncio.Event(), asyncio.Event()
            requests = []
            async def respond(route):
                body = route.request.post_data_json
                requests.append(body)
                if len(requests) == 1:
                    held.set()
                    await release.wait()
                await route.fulfill(json={'answer':'Answer from submitted sources.', 'assistant':'ChatGPT',
                    'scope':body['scope'], 'evidence':[]})
            await page.route('**/api/ask', respond)
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-select-paper="a"]').first.check()
                await page.locator('[data-select-paper="b"]').first.check()
                await page.locator('#ask-selection').click()
                await page.locator('#agent-question').fill('First question')
                await page.locator('#agent-send').click()
                await asyncio.wait_for(held.wait(), timeout=5)
                await page.locator('#agent-add').click()
                await page.locator('#source-search').fill('separate')
                await page.locator('[data-source-paper="c"]').check()
                await page.locator('#source-apply').click()
                release.set()
                await page.locator('#agent-question:enabled').wait_for()
                assert requests[0]['scope']['papers'] == ['a', 'b']
                saved = httpx.get(url + '/api/chats?all_scopes=true').json()['chats'][0]
                assert saved['messages'][0]['scope']['papers'] == ['a', 'b']
                assert saved['scope']['papers'] == ['a', 'b', 'c']
                await page.locator('#agent-question').fill('Follow up')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
                assert requests[1]['scope']['papers'] == ['a', 'b', 'c']
                # A narrowing fork retains an explicitly chosen provider even if Settings differs.
                config.set_ai_provider('api')
                await page.evaluate('pollOnce()')
                await page.locator('[data-remove-source="c"]').click()
                await page.get_by_role('button', name='Start new conversation', exact=True).click()
                await expect(page.locator('#agent-agent')).to_have_value('codex')
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_followup_flushes_failed_save_before_calling_model(engine, monkeypatch):
    seed()
    calls = model_stub(monkeypatch)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            writes = []
            async def fail_save(route):
                if route.request.method == 'PUT':
                    writes.append(route.request.post_data_json)
                    await route.fulfill(status=503, json={'detail': 'Storage temporarily unavailable'})
                else:
                    await route.continue_()
            with _server() as url:
                await page.goto(url)
                await page.route('**/api/chats/*', fail_save)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill('First question')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-status')).to_contain_text('not saved yet')
                assert len(calls) == 1 and len(writes) == 1
                await page.locator('#agent-question').fill('Follow-up question')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-question')).to_be_enabled()
                assert len(calls) == 1 and len(writes) == 2
                await expect(page.locator('#agent-question')).to_have_value('Follow-up question')
                await page.unroute('**/api/chats/*', fail_save)
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-question')).to_be_empty()
                await expect(page.locator('#agent-messages .agent-answer')).to_have_count(2)
                assert len(calls) == 2
                saved = httpx.get(url + '/api/chats').json()['chats']
                assert len(saved) == 1 and len(saved[0]['messages']) == 4
                assert saved[0]['messages'][0]['content'] == 'First question'
                assert saved[0]['messages'][2]['content'] == 'Follow-up question'
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_note_keeps_originating_workspace_while_conversation_save_waits(engine, monkeypatch):
    seed()
    model_stub(monkeypatch)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            started, release = asyncio.Event(), asyncio.Event()
            async def delay_save(route):
                if route.request.method == 'PUT':
                    started.set()
                    await release.wait()
                await route.continue_()
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other workspace'}).json()['workspace']['id']
                await page.goto(url)
                await page.locator('[data-select-paper="a"]').first.check()
                await page.locator('[data-select-paper="b"]').first.check()
                await page.locator('#ask-selection').click()
                await page.locator('#agent-question').fill('Explain the evidence')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-question')).to_be_empty()
                await page.route('**/api/chats/*', delay_save)
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await asyncio.wait_for(started.wait(), 5)
                await page.locator('#agent-close').click()
                await page.evaluate('workspace => switchWorkspace(workspace)', other)
                await page.locator('#btn-agent:enabled').click()
                release.set()
                for _ in range(100):
                    notes = httpx.get(url + '/api/research-notes', headers={'X-Doxograph-Workspace':'default'}).json()['notes']
                    if notes:
                        break
                    await asyncio.sleep(.05)
                assert len(notes) == 1
                body = notes[0]['body']
                assert 'ws=default' in body and f'ws={other}' not in body
                assert 'Sources: Memory and recovery' in body
                assert httpx.get(url + '/api/research-notes', headers={'X-Doxograph-Workspace':other}).json()['notes'] == []
    run_browser(scenario())
