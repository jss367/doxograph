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
def test_agent_labels_appear_in_activity_and_sidebar_without_reloading(monkeypatch):
    seed()
    store.set_labels('a', ['Reading list'])

    def answer(messages, callback):
        capture = ask_context.active.get()

        async def classify():
            mcp = mcp_server.create_server(workspace=config.workspace_id(), papers=capture.scope.papers,
                allow_labels=True, evidence_log=str(capture.log), without_notes=True,
                transport=httpx.ASGITransport(app=server.app))
            await mcp.call_tool('search_claims', {'workspace': config.workspace_id(), 'paper': 'a'})
            result = await mcp.call_tool('update_paper_labels', {'workspace': config.workspace_id(),
                'papers': ['a'], 'labels': ['Activation steering']})
            assert result.structured_content['changed'] == 1

        asyncio.run(classify())
        return {'answer': 'Added Activation steering to Memory and recovery.', 'assistant': 'ChatGPT',
                'model': 'test', 'activity': ['Requested label update (add): Activation steering · a']}

    monkeypatch.setattr(agent, 'answer', answer)

    async def scenario():
        async with browser_session('chromium') as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill('Label the memory paper based on its content.')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-messages .agent-answer')).to_contain_text('Added Activation steering')
                summary = page.get_by_text('Library activity (1)', exact=True)
                await expect(summary).to_be_visible()
                await summary.click()
                await expect(page.locator('#agent-messages .activity').filter(has=summary)).to_contain_text('Activation steering · a')
                await page.locator('#agent-close').click()
                await page.evaluate('pollOnce()')
                await expect(page.locator('#labels')).to_contain_text('Activation steering')
                await expect(page.locator('#labels')).to_contain_text('Reading list')
                assert store.load_paper('a')['labels'] == ['Activation steering', 'Reading list']
                assert store.load_paper('b')['labels'] == []
                await page.locator('#btn-agent').click()
                await page.get_by_text('Library activity (1)', exact=True).click()
                await page.screenshot(path='.context/agent-labels.png', full_page=True)

    run_browser(scenario())


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


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_attachment_only_draft_survives_contextual_open_and_refresh(engine):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            with _server() as url:
                await page.goto(url + "/#paper=a")
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-files').set_input_files(
                    {'name': 'draft.txt', 'mimeType': 'text/plain', 'buffer': b'Unsent context'})
                await expect(page.locator('#agent-attachments')).to_contain_text('draft.txt')
                await expect(page.locator('#agent-question')).to_be_empty()
                await page.locator('#agent-close').click()
                await page.locator('[data-paper-chat="a"]').first.click()
                await expect(page.locator('#agent-thread [data-thread-open]')).to_have_count(2)
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(0)
                await page.locator('#agent-thread [data-thread-open]').nth(1).click()
                await expect(page.locator('#agent-attachments')).to_contain_text('draft.txt')
                await page.locator('#agent-close').click()
                await page.locator('#btn-agent:enabled').click()
                await expect(page.locator('#agent-attachments')).to_contain_text('draft.txt')
                assert httpx.get(url + '/api/chats').json()['chats'] == []
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('has_claims', [False, True])
def test_paper_without_pdf_offers_explicit_scoped_retrieval(engine, has_claims, monkeypatch):
    paper = store.new_paper('metadata', title='A study without a PDF', abstract='An abstract about recovery.',
                            summary='A separate summary about study design.')
    if has_claims:
        paper['claims'].append(store.new_claim(paper, text='An extracted finding.'))
    store.save_paper(paper)
    store.save_paper(store.new_paper('outside', title='Unrelated study'))
    config.set_ai_provider('api')
    calls = []
    def answer(messages, callback):
        capture = ask_context.active.get()
        calls.append((capture.scope.model_dump(), messages))
        mcp = mcp_server.create_server(workspace='default', papers=capture.scope.papers,
            evidence_log=str(capture.log), transport=httpx.ASGITransport(app=server.app))
        async def retrieve():
            result = await mcp.call_tool('get_paper', {'workspace':'default', 'key':'metadata'})
            return result.structured_content
        evidence = asyncio.run(retrieve())['evidence'][0]
        return {'answer':f"The abstract discusses recovery [[e:{evidence['ref']}]]. Full text was not available.",
                'assistant':'ChatGPT', 'model':'mock'}
    monkeypatch.setattr(agent, 'answer', answer)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1100})
            with _server() as url:
                await page.goto(url + '/#paper=metadata')
                await page.get_by_role('button', name='Ask about this paper', exact=True).click()
                await page.locator('#agent-question').fill('What does the abstract say?')
                await expect(page.locator('#agent-send')).to_be_disabled()
                label = 'Use extracted claims only' if has_claims else 'Use paper metadata only'
                await page.get_by_role('button', name=label, exact=True).click()
                await expect(page.locator('#agent-agent')).to_have_value('api')
                await expect(page.locator('#agent-send')).to_be_disabled()
                await expect(page.locator('#agent-status')).to_contain_text('Choose a supported agent')
                await expect(page.locator('#agent-question')).to_have_value('What does the abstract say?')
                if not has_claims:
                    await expect(page.locator('#agent-input-format')).to_contain_text('Full text, extracted claims, and page images are unavailable')
                await page.locator('#agent-agent').select_option('codex')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-question')).to_be_empty()
                assert len(calls) == 1 and calls[0][0]['mode'] == 'selected'
                assert calls[0][0]['papers'] == ['metadata']
                await page.locator('.citation-link').click()
                await expect(page.locator('#citation-body')).to_contain_text('An abstract about recovery.')
                await expect(page.locator('#citation-body')).to_contain_text('A separate summary about study design.')
                saved = httpx.get(url + '/api/chats?all_scopes=true').json()['chats']
                assert saved[0]['scope']['papers'] == ['metadata']
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('enabled', [False, True])
def test_ask_waits_for_initial_provider_state(engine, enabled, monkeypatch):
    seed()
    config.set_ai_enabled(enabled)
    calls = model_stub(monkeypatch)
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1100})
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed_state(route):
                response = await route.fetch()
                started.set()
                await release.wait()
                await route.fulfill(response=response)
            await page.route('**/api/state', delayed_state)
            with _server() as url:
                await page.goto(url)
                await asyncio.wait_for(started.wait(), 5)
                await expect(page.locator('#btn-agent')).to_be_disabled()
                release.set()
                await page.locator('#btn-agent:enabled').click()
                await expect(page.locator('#agent-agent')).to_have_value('codex')
                if enabled:
                    await page.locator('#agent-question').fill('What do these papers say?')
                    await page.locator('#agent-send').click()
                    await expect(page.locator('#agent-question')).to_be_empty()
                    assert len(calls) == 1
                else:
                    await expect(page.locator('#agent-send')).to_be_disabled()
                    await expect(page.locator('#agent-settings')).to_be_visible()
                    assert not calls
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_scope_fork_isolates_attachments_from_inflight_messages(engine):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1100})
            started, release = asyncio.Event(), asyncio.Event()
            async def delayed_answer(route):
                started.set()
                await release.wait()
                await route.fulfill(json={'answer':'Original answer', 'scope':route.request.post_data_json['scope'], 'evidence':[]})
            await page.route('**/api/ask', delayed_answer)
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-files').set_input_files({'name':'original.txt', 'mimeType':'text/plain', 'buffer':b'Original'})
                await page.locator('#agent-question').fill('Original question')
                await page.locator('#agent-send').click()
                await asyncio.wait_for(started.wait(), 5)
                await page.evaluate("agentChats.newScope({mode:'selected', papers:['a'], focus:[]})")
                await expect(page.locator('#agent-attachments')).to_contain_text('original.txt')
                await page.locator('#agent-attachments button').click()
                await page.locator('#agent-files').set_input_files({'name':'new.txt', 'mimeType':'text/plain', 'buffer':b'New'})
                release.set()
                await page.wait_for_function("!document.querySelector('#agent-thread').textContent.includes('answering')")
                saved = httpx.get(url + '/api/chats?all_scopes=true').json()['chats'][0]
                assert [a['name'] for a in saved['messages'][0]['attachments']] == ['original.txt']
                await page.locator(f"[data-thread-open='{saved['id']}']").click()
                await page.locator('#agent-files').set_input_files({'name':'old-draft.txt', 'mimeType':'text/plain', 'buffer':b'Old draft'})
                await expect(page.locator('#agent-attachments')).to_contain_text('old-draft.txt')
                # Fork a settled conversation too, then edit both drafts independently.
                await page.evaluate("agentChats.newScope({mode:'selected', papers:['b'], focus:[]})")
                await page.locator('#agent-attachments button').click()
                await page.locator('#agent-files').set_input_files({'name':'fork.txt', 'mimeType':'text/plain', 'buffer':b'Fork'})
                await expect(page.locator('#agent-attachments')).to_contain_text('fork.txt')
                await page.evaluate('window.forkDraft = activeAgentChat()')
                await page.locator(f"[data-thread-open='{saved['id']}']").click()
                await expect(page.locator('#agent-attachments')).to_contain_text('old-draft.txt')
                await page.locator('#agent-attachments button').click()
                await page.locator('#agent-files').set_input_files({'name':'other.txt', 'mimeType':'text/plain', 'buffer':b'Other'})
                assert await page.evaluate('forkDraft.draftAttachments.map(a => a.name)') == ['fork.txt']
                await expect(page.locator('#agent-attachments')).to_contain_text('other.txt')
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_large_source_roster_saves_a_bounded_note(engine):
    seed()
    keys = [f'{i:05d}-' + 'x' * 194 for i in range(10000)]
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1100})
            async def answer(route):
                await route.fulfill(json={'answer':'A short answer.', 'scope':{'mode':'library', 'papers':keys, 'focus':[]}, 'evidence':[]})
            await page.route('**/api/ask', answer)
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill('Summarize')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-question')).to_be_empty()
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                note = httpx.get(url + '/api/research-notes').json()['notes'][0]
                assert 'A short answer.' in note['body']
                assert '9800 additional papers omitted' in note['body']
                assert len(note['body']) < 2_000_000
                saved = httpx.get(url + '/api/chats?all_scopes=true').json()['chats'][0]
                assert saved['messages'][0]['scope']['papers'] == keys
                assert await page.evaluate("boundedNoteText('x'.repeat(2100000), 1900000, 'Note').length") < 2_000_000
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_pane_keeps_workspace_interactive_while_answering(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            held, release = asyncio.Event(), asyncio.Event()
            requests, errors = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            async def respond(route):
                requests.append((route.request.headers['x-doxograph-workspace'], route.request.post_data_json))
                held.set()
                await release.wait()
                await route.fulfill(json={'answer': 'The studies report improved recovery.', 'assistant': 'ChatGPT',
                    'scope': {**route.request.post_data_json['scope'], 'papers': ['a', 'b', 'c']}})

            await page.route('**/api/ask', respond)
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other workspace'}).json()['workspace']['id']
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await expect(page.locator('#btn-agent')).to_have_attribute('aria-expanded', 'true')
                assert not await page.locator('dialog[open]').count()
                pane = await page.locator('#library-agent').bounding_box()
                main = await page.locator('#main').bounding_box()
                assert pane['x'] >= main['x'] + main['width'] - 1
                assert abs(pane['x'] + pane['width'] - 1440) <= 1
                await page.locator('#agent-question').fill('What do these papers show?')
                await page.locator('#agent-send').click()
                await asyncio.wait_for(held.wait(), 5)
                try:
                    # Real clicks must reach navigation while the answer is pending.
                    await page.locator('#papers li[data-paper="a"]').click()
                    await expect(page.locator('#reader')).to_be_visible()
                    await page.locator('#papers li[data-paper="b"]').click()
                    await expect(page.locator('#paperhead')).to_contain_text('Recovery under intervention')
                    await expect(page.locator('#library-agent')).to_be_visible()
                    await expect(page.locator('#agent-status')).to_contain_text('Looking through')
                    await page.locator('#content [data-act="edit"]').first.click()
                    draft = page.locator('form[data-form] textarea[name="note"]')
                    await draft.fill('A note made while the agent answers.')
                    await expect(draft).to_have_value('A note made while the agent answers.')
                    await page.locator('form[data-form] [data-act="cancel"]').click()
                finally:
                    release.set()
                await expect(page.locator('#agent-messages')).to_contain_text('The studies report improved recovery.')
                await page.wait_for_function('workspaceClient.pendingMutations === 0')
                await page.locator('#workspace').select_option(other)
                await expect(page.locator('#workspace')).to_have_value(other)
                await expect(page.locator('#agent-workspace')).not_to_contain_text('Other workspace')
                await page.locator('#agent-question').fill('Keep this follow-up')
                # Pane keyboard input cannot navigate or review claims behind it.
                await page.locator('#agent-close').focus()
                await page.keyboard.press('/')
                await expect(page.locator('#agent-close')).to_be_focused()
                await page.keyboard.press('Escape')
                await expect(page.locator('#library-agent')).to_be_hidden()
                await expect(page.locator('#btn-agent')).to_be_focused()
                await expect(page.locator('#btn-agent')).to_have_attribute('aria-expanded', 'false')
                await page.locator('#workspace').select_option('default')
                await page.locator('#btn-agent').click()
                await expect(page.locator('#agent-question')).to_have_value('Keep this follow-up')
                await expect(page.locator('#agent-messages')).to_contain_text('improved recovery')
                send = await page.locator('#agent-send').bounding_box()
                assert send['y'] + send['height'] <= 1000
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/ask-library-right-pane.png')
                assert requests[0][0] == 'default'
                assert requests[0][1]['scope']['mode'] == 'library'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_ask_docks_before_a_wide_sidebar_squeezes_the_workspace(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 900, 'height': 1000})
            await page.add_init_script("localStorage.setItem('doxograph-side-width', '450')")
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await expect(page.locator('body')).to_have_class(re.compile(r'agent-docked'))
                main = await page.locator('#main').bounding_box()
                pane = await page.locator('#library-agent').bounding_box()
                assert main['width'] >= 449
                assert pane['y'] >= main['y'] + main['height'] - 1
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator('#reader')).to_be_visible()
                await page.locator('#content [data-act="edit"]').first.click()
                draft = page.locator('form[data-form] textarea[name="note"]')
                await draft.fill('Editing beside a docked conversation.')
                # A wider window restores the right pane and keeps both drafts.
                await page.set_viewport_size({'width': 1800, 'height': 1000})
                await expect(page.locator('body')).not_to_have_class(re.compile(r'agent-docked'))
                await expect(draft).to_have_value('Editing beside a docked conversation.')
                main = await page.locator('#main').bounding_box()
                pane = await page.locator('#library-agent').bounding_box()
                assert main['width'] >= 560
                assert pane['x'] >= main['x'] + main['width'] - 1
                assert await page.evaluate("localStorage.getItem('doxograph-side-width')") == '450'
                # Resizing the sidebar can also dock and undock without a window resize.
                await page.set_viewport_size({'width': 1440, 'height': 1000})
                await expect(page.locator('body')).to_have_class(re.compile(r'agent-docked'))
                await page.locator('#side-resize').focus()
                await page.keyboard.press('Home')
                await expect(page.locator('body')).not_to_have_class(re.compile(r'agent-docked'))
                await expect(draft).to_have_value('Editing beside a docked conversation.')
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_ask_owner_name_tracks_registry_updates_without_changing_conversation(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other workspace'}).json()['workspace']['id']
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill('Keep the same conversation')
                await page.locator('#workspace-actions > summary').click()
                await page.locator('#btn-workspace-rename').click()
                await page.locator('#ask-input').fill('Renamed library')
                await page.locator('#ask-ok').click()
                await expect(page.locator('#agent-workspace')).to_have_text('Conversation in Renamed library')
                await page.wait_for_function('workspaceClient.pendingMutations === 0')
                await page.locator('#workspace').select_option(other)
                await expect(page.locator('#workspace')).to_have_value(other)
                await expect(page.locator('#agent-workspace')).to_have_text('Conversation in Renamed library')
                # Refreshing metadata for the originating workspace while viewing another
                # workspace must still label the original conversation, not the selection.
                response = httpx.patch(url + '/api/workspaces/default', json={'name': 'Recovery library'})
                response.raise_for_status()
                await page.evaluate('loadWorkspaces()')
                await expect(page.locator('#agent-workspace')).to_have_text('Conversation in Recovery library')
                await expect(page.locator('#agent-question')).to_have_value('Keep the same conversation')
                assert await page.evaluate('agentChats.workspace') == 'default'
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_job_notifications_do_not_cover_the_ask_composer(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            calls = []

            async def answer(route):
                calls.append(route.request.post_data_json)
                await route.fulfill(json={'answer': 'You can ask while papers import.', 'assistant': 'ChatGPT',
                    'scope': {**calls[-1]['scope'], 'papers': ['a', 'b', 'c']}})

            await page.route('**/api/ask', answer)
            await page.route('**/api/jobs', lambda route: route.fulfill(json={'jobs': [{
                'id': 'running', 'kind': 'import', 'state': 'fetching', 'label': 'An active import',
                'workspace': 'default', 'detail': '',
            }]}))
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.evaluate("""() => toast('A persistent workspace warning', {timeout:0, tone:'warn',
                    actions:[{label:'Undo', onClick:() => { window.toastUndoUsed = true; }}]})""")
                for width in [1440, 900, 390]:
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    if width <= 900:
                        await expect(page.locator('body')).to_have_class(re.compile(r'agent-docked'))
                    else:
                        await expect(page.locator('body')).not_to_have_class(re.compile(r'agent-docked'))
                    await expect(page.locator('#jobs .job')).to_be_visible()
                    jobs = await page.locator('#jobs').bounding_box()
                    pane = await page.locator('#library-agent').bounding_box()
                    notice = await page.locator('#toasts').bounding_box()
                    if width == 1440:
                        assert jobs['x'] + jobs['width'] <= pane['x']
                    else:
                        assert jobs['y'] + jobs['height'] <= pane['y']
                        assert notice['y'] + notice['height'] <= pane['y']
                        assert notice['x'] + notice['width'] <= jobs['x']
                    await page.locator('#agent-question').fill(f'Question at {width}px')
                    # A real click checks stacking and hit targets as well as box geometry.
                    await page.locator('#agent-send').click()
                    await expect(page.locator('#agent-question')).to_be_enabled()
                assert len(calls) == 3
                await page.locator('#toasts').get_by_role('button', name='Undo', exact=True).click()
                assert await page.evaluate('window.toastUndoUsed') is True
                await page.set_viewport_size({'width': 900, 'height': 1000})
                await page.locator('#agent-close').click()
                jobs = await page.locator('#jobs').bounding_box()
                assert jobs['x'] + jobs['width'] == 884
                assert jobs['y'] + jobs['height'] == 984
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_removing_an_ask_source_disables_send_and_explains_the_repair(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other workspace'}).json()['workspace']['id']
                await page.goto(url + '/#paper=a')
                await page.locator('[data-paper-chat="a"]').first.click()
                await page.locator('#agent-question').fill('Keep my question after removing this paper')
                await expect(page.locator('#agent-send')).to_be_enabled()
                await page.locator('#papers li[data-paper="a"] .pmenu').click()
                await page.locator('#ctxmenu [data-act="del-paper"]').click()
                await page.locator('#ask-ok').click()
                await expect(page.locator('#papers li[data-paper="a"]')).to_have_count(0)
                await expect(page.locator('#agent-send')).to_be_disabled()
                await expect(page.locator('#agent-status')).to_have_text('Source unavailable. Repair the selection before asking another question.')
                await expect(page.locator('#agent-question')).to_have_value('Keep my question after removing this paper')
                # Leaving the owner must cache its latest corpus, not revive a removed source.
                await page.wait_for_function('workspaceClient.pendingMutations === 0')
                await page.locator('#workspace').select_option(other)
                await expect(page.locator('#workspace')).to_have_value(other)
                await page.wait_for_function('workspace => S.workspace?.id === workspace', arg=other)
                await expect(page.locator('#agent-send')).to_be_disabled()
                await expect(page.locator('#agent-status')).to_have_text('Source unavailable. Repair the selection before asking another question.')
                assert await page.evaluate('agentChats.workspace') == 'default'
                # The existing repair controls remain usable without reopening Ask.
                await page.locator('[data-remove-source="a"]').click()
                await page.locator('#agent-add').click()
                await page.locator('[data-source-paper="b"]').check()
                await page.locator('#source-apply').click()
                await expect(page.locator('#agent-scope')).to_contain_text('Recovery under intervention')
                await expect(page.locator('#agent-send')).to_be_enabled()
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_closing_contextual_ask_restores_the_opener_or_falls_back(engine):
    seed()
    claim = store.load_paper('a')['claims'][0]

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#paper=a')
                for selector in ['[data-paper-chat="a"]', f'[data-ask-claim="{claim["id"]}"]']:
                    opener = page.locator(selector).first
                    await opener.click()
                    await expect(page.locator('#agent-question')).to_be_focused()
                    await page.keyboard.press('Escape')
                    await expect(opener).to_be_focused()
                    # A saved edit replaces the button's DOM node but keeps its action.
                    await opener.click()
                    await page.locator('#content [data-act="edit"]').first.click()
                    form = page.locator('form[data-form]')
                    await form.locator('textarea[name="note"]').fill('Saved while asking from ' + selector)
                    await form.locator('button[type="submit"]').click()
                    await expect(form).to_have_count(0)
                    await page.locator('#agent-question').focus()
                    await page.keyboard.press('Escape')
                    await expect(opener).to_be_focused()
                await page.locator('#btn-back-library').click()
                await page.locator('[data-select-paper="a"]').first.check()
                await page.locator('#ask-selection').click()
                await page.keyboard.press('Escape')
                await expect(page.locator('#ask-selection')).to_be_focused()
                # Navigation can retire a contextual opener while Ask remains open.
                await page.locator('#papers li[data-paper="a"]').click()
                await page.locator('[data-paper-chat="a"]').first.click()
                await page.locator('#papers li[data-paper="b"]').click()
                await page.locator('#agent-question').focus()
                await page.keyboard.press('Escape')
                await expect(page.locator('#btn-agent')).to_be_focused()
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversation_refresh_during_input_preserves_the_question(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await page.route('**/api/ask', lambda route: route.fulfill(json={'answer': 'The question was kept.', 'assistant': 'ChatGPT'}))
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                # A conversation-list response can render between native input listeners.
                await page.evaluate("""() => document.addEventListener('input', event => {
                    if (event.target.id === 'agent-question') agentChats.sync();
                }, true)""")
                await page.locator('#agent-question').fill('Keep this question through a refresh')
                await expect(page.locator('#agent-question')).to_have_value('Keep this question through a refresh')
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-messages')).to_contain_text('Keep this question through a refresh')
                await expect(page.locator('#agent-messages')).to_contain_text('The question was kept.')
    run_browser(scenario())
