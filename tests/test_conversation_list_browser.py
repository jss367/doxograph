"""Conversation rows switch independently; archiving preserves saved evidence."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_browser import _server

# A tall window shows the conversation list without scrolling the docked Ask
# pane. When the pane must scroll, Linux WebKit moves it as a click takes focus
# off the composer, so the button slides out from under the pointer.
VIEWPORT = {'width': 1100, 'height': 1100}


def seed_conversations(url):
    config.set_ai_provider('codex')
    for letter, question, answer in [
        ('a', 'Which findings have the strongest evidence?', 'The studies agree on improved recovery, with different sample sizes.'),
        ('b', 'Where do the papers disagree?', 'The reported effect varies with the intervention and measurement.'),
        ('c', 'What should I read next?', 'Start with the comparison study, then inspect the supporting experiments.'),
    ]:
        response = httpx.put(f'{url}/api/chats/{letter * 32}', json={
            'agent': 'codex', 'revision': 0, 'operation': letter * 32,
            'messages': [{'role': 'user', 'content': question,
                          'attachments': [{'name': 'context.txt', 'media_type': 'text/plain', 'data': 'Q29udGV4dA=='}]},
                         {'role': 'assistant', 'content': answer, 'assistant': 'ChatGPT'}],
        })
        assert response.status_code == 200


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversations_list_archive_restore_reload_and_keep_drafts(engine):
    async def scenario(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport=VIEWPORT)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            rows = page.locator('#agent-thread [data-thread-open]')
            await expect(rows).to_have_count(3)
            assert await page.locator('select#agent-thread').count() == 0
            positions = await rows.evaluate_all('(rows) => rows.map(r => r.getBoundingClientRect().top)')
            assert positions == sorted(set(positions))
            first = page.locator(f'#agent-thread [data-thread-open="{"a" * 32}"]')
            second = page.locator(f'#agent-thread [data-thread-open="{"b" * 32}"]')
            await first.focus()
            await page.keyboard.press('Enter')
            await expect(first).to_have_attribute('aria-current', 'true')
            await page.locator('#agent-question').fill('Keep this draft with the evidence question')
            await page.locator('#agent-clear').click()
            await page.locator('#agent-question').fill('An independent question')
            draft_id = await page.locator('#agent-thread [aria-current="true"]').get_attribute('data-thread-open')
            await second.click()
            await page.locator('#agent-question').fill('Compare the measurement conditions')
            await first.click()
            await expect(page.locator('#agent-question')).to_have_value('Keep this draft with the evidence question')

            # Archive another row without changing the current draft or transcript.
            archive_third = page.locator(f'[data-thread-archive="{"c" * 32}"]')
            await archive_third.click()
            await expect(rows).to_have_count(3)
            await expect(page.locator('#agent-question')).to_have_value('Keep this draft with the evidence question')
            await page.locator('#agent-archive summary').click()
            await expect(page.locator('#agent-archived [data-thread-open]')).to_have_count(1)
            if engine == 'chromium':
                Path('.context').mkdir(exist_ok=True)
                await page.screenshot(path='.context/conversation-list.png')

            await page.locator(f'[data-thread-archive="{"a" * 32}"]').click()
            await expect(first).not_to_be_visible()
            await expect(page.locator('#agent-question')).to_have_value('An independent question')
            await second.click()
            await expect(page.locator('#agent-question')).to_have_value('Compare the measurement conditions')
            archived_first = page.locator(f'#agent-archived [data-thread-open="{"a" * 32}"]')
            await archived_first.click()
            await expect(page.locator('#agent-messages')).to_contain_text('improved recovery')
            await expect(page.locator('#agent-send')).to_be_disabled()
            await page.get_by_role('button', name='Restore Which findings have the strongest evidence?', exact=True).click()
            await expect(first).to_have_attribute('aria-current', 'true')
            await expect(page.locator('#agent-question')).to_have_value('Keep this draft with the evidence question')
            await page.locator(f'[data-thread-open="{draft_id}"]').click()
            await expect(page.locator('#agent-question')).to_have_value('An independent question')

            await page.reload()
            await page.locator('#btn-agent:enabled').click()
            await expect(rows).to_have_count(2)
            await expect(page.locator('#agent-archive summary')).to_have_text('Archived conversations (1)')
            await first.click()
            await expect(page.locator('#agent-messages a[data-chat-download]')).to_have_text('context.txt')
            # When all saved conversations are archived, a fresh one stays usable.
            for letter in ['a', 'b']:
                await page.locator(f'[data-thread-archive="{letter * 32}"]').click()
                await expect(page.locator(f'#agent-thread [data-thread-open="{letter * 32}"]')).to_have_count(0)
            await expect(rows).to_have_text(['New conversation'])
            await page.reload()
            await page.locator('#btn-agent:enabled').click()
            await expect(rows).to_have_text(['New conversation'])
            await expect(page.locator('#agent-question')).to_be_enabled()
            assert not errors

    with _server() as url:
        seed_conversations(url)
        run_browser(scenario(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_archive_failure_and_lost_response_keep_saved_conversations(engine):
    async def scenario(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport=VIEWPORT)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            archive = page.locator(f'[data-thread-archive="{"a" * 32}"]')
            original = httpx.get(url + '/api/chats').json()['chats'][-1]

            async def fail_archive(route):
                if route.request.method == 'PATCH':
                    await route.fulfill(status=507, json={'detail': 'Disk full'})
                else:
                    await route.continue_()

            await page.route('**/api/chats/*', fail_archive)
            await archive.click()
            await expect(page.locator('.chat-thread-error')).to_contain_text('Disk full')
            await expect(page.locator('#agent-thread [data-thread-open]')).to_have_count(3)
            assert httpx.get(url + '/api/chats').json()['chats'][-1] == original
            await page.unroute('**/api/chats/*', fail_archive)

            async def lose_response(route):
                if route.request.method == 'PATCH':
                    await route.fetch()
                    await route.abort()
                else:
                    await route.continue_()

            await page.route('**/api/chats/*', lose_response)
            await archive.click()
            await expect(page.locator('#agent-thread [data-thread-open]')).to_have_count(2)
            saved = next(c for c in httpx.get(url + '/api/chats').json()['chats'] if c['id'] == 'a' * 32)
            assert saved['archived'] and saved['messages'] == original['messages']
            await page.unroute('**/api/chats/*', lose_response)
            await page.locator('#agent-archive summary').click()
            await page.get_by_role('button', name='Restore Which findings have the strongest evidence?', exact=True).click()
            await expect(page.locator('#agent-thread [data-thread-open]')).to_have_count(3)

    with _server() as url:
        seed_conversations(url)
        run_browser(scenario(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('other_active', [True, False], ids=['other-active', 'last-active'])
def test_lost_active_archive_response_selects_a_usable_conversation(engine, other_active):
    async def scenario(url, original):
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport=VIEWPORT)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator(f'#agent-thread [data-thread-open="{"a" * 32}"]').click()
            await page.locator('#agent-question').fill('Keep the archived conversation draft')

            async def lose_response(route):
                if route.request.method == 'PATCH':
                    await route.fetch()
                    await route.abort()
                else:
                    await route.continue_()

            await page.route('**/api/chats/*', lose_response)
            await page.locator(f'[data-thread-archive="{"a" * 32}"]').click()
            active = page.locator('#agent-thread [aria-current="true"]')
            await expect(active).to_have_count(1)
            await expect(active).not_to_have_attribute('data-thread-open', 'a' * 32)
            await expect(page.locator('#agent-question')).to_be_enabled()
            if other_active:
                await expect(page.locator('#agent-thread [data-thread-open]')).to_have_count(2)
            else:
                await expect(active).to_have_text('New conversation')
                await expect(page.locator('#agent-messages')).to_be_empty()
            saved = httpx.get(url + '/api/chats').json()['chats']
            assert len(saved) == 3
            first = next(chat for chat in saved if chat['id'] == 'a' * 32)
            assert first['archived'] and first['messages'] == original['messages']
            assert await page.locator('.chat-thread-error').count() == 0
            # Recovery also selects a restored row after its response is lost.
            await page.locator('#agent-archive summary').click()
            await page.locator(f'[data-thread-archive="{"a" * 32}"]').click()
            await expect(active).to_have_attribute('data-thread-open', 'a' * 32)
            await expect(page.locator('#agent-question')).to_have_value('Keep the archived conversation draft')
            await expect(page.locator('#agent-question')).to_be_enabled()
            assert await page.locator('.chat-thread-error').count() == 0
            assert not errors

    with _server() as url:
        seed_conversations(url)
        if not other_active:
            for letter in ['b', 'c']:
                assert httpx.patch(f'{url}/api/chats/{letter * 32}', json={'revision': 1, 'archived': True}).status_code == 200
        original = next(chat for chat in httpx.get(url + '/api/chats').json()['chats'] if chat['id'] == 'a' * 32)
        run_browser(scenario(url, original))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('action', ['save', 'missing-note'])
def test_note_actions_wait_for_archive_and_restore_responses(engine, action):
    async def scenario(url, original):
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport=VIEWPORT)
            errors, writes, note_reads = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: writes.append(request.url)
                    if request.method in ('POST', 'PUT') and '/api/' in request.url else None)
            page.on('request', lambda request: note_reads.append(request.url)
                    if request.method == 'GET' and '/api/research-notes' in request.url else None)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator(f'#agent-thread [data-thread-open="{"a" * 32}"]').click()
            await page.locator('#agent-question').fill('Keep this draft with its conversation')
            button = page.locator('[data-agent-action]')
            expected_action = 'open' if action == 'missing-note' else 'save'
            for archived in (True, False):
                held, release = asyncio.Event(), asyncio.Event()

                async def hold_response(route, _request, held=held, release=release):
                    if route.request.method != 'PATCH':
                        await route.continue_()
                        return
                    response = await route.fetch()
                    held.set()
                    await release.wait()
                    await route.fulfill(response=response)

                await page.route('**/api/chats/*', hold_response)
                await page.locator(f'[data-thread-archive="{"a" * 32}"]').click()
                await asyncio.wait_for(held.wait(), timeout=10)
                try:
                    await expect(button).to_have_attribute('data-agent-action', expected_action)
                    await expect(button).to_be_disabled()
                    writes.clear()
                    note_reads.clear()
                    # Exercise the delegated handler even if a stale/synthetic click
                    # bypasses the disabled button while the PATCH reply is delayed.
                    await button.dispatch_event('click')
                    await page.evaluate('Promise.resolve()')
                    assert not await page.evaluate('Boolean(activeAgentChat().messages[1].saving)')
                    assert writes == [] and note_reads == []
                    saved = httpx.get(url + '/api/chats').json()['chats']
                    assert len(saved) == 3
                    first = next(chat for chat in saved if chat['id'] == 'a' * 32)
                    assert first['archived'] is archived
                    assert first['messages'] == original['messages']
                finally:
                    release.set()
                await page.wait_for_function('!activeAgentChat().archiving')
                await page.unroute('**/api/chats/*', hold_response)
                if archived:
                    await page.locator('#agent-archive summary').click()
                    await page.locator(f'#agent-archived [data-thread-open="{"a" * 32}"]').click()
                await expect(button).to_be_enabled()
                await expect(page.locator('#agent-question')).to_have_value('Keep this draft with its conversation')
            await expect(page.locator('#agent-thread [aria-current="true"]')).to_have_attribute('data-thread-open', 'a' * 32)
            # Both actions work again after the archive transition finishes.
            await button.click()
            if action == 'missing-note':
                await expect(page.locator('#agent-messages')).to_contain_text('The saved note was deleted')
                await expect(button).to_have_attribute('data-agent-action', 'save')
                await button.click()
            await expect(button).to_have_attribute('data-agent-action', 'open')
            notes = httpx.get(url + '/api/research-notes').json()['notes']
            assert len(notes) == 1
            assert 'Stored evidence' in notes[0]['body'] and 'Recovery improved.' in notes[0]['body']
            saved = httpx.get(url + '/api/chats').json()['chats']
            assert len(saved) == 3
            first = next(chat for chat in saved if chat['id'] == 'a' * 32)
            assert first['messages'][1]['evidence'] == original['messages'][1]['evidence']
            assert not errors

    with _server() as url:
        seed_conversations(url)
        original = next(chat for chat in httpx.get(url + '/api/chats').json()['chats'] if chat['id'] == 'a' * 32)
        original['messages'][1]['evidence'] = [{'ref': 'f' * 20, 'kind': 'passage', 'paper': 'study',
                                               'title': 'Stored evidence', 'text': 'Recovery improved.', 'page': 1}]
        if action == 'missing-note':
            original['messages'][1].update(saved_note='d' * 32, note_operation='e' * 32)
        response = httpx.put(f'{url}/api/chats/{"a" * 32}', json={
            'agent': original['agent'], 'messages': original['messages'],
            'revision': original['revision'], 'operation': 'f' * 32,
        })
        assert response.status_code == 200
        run_browser(scenario(url, response.json()))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('other_active', [True, False], ids=['other-active', 'last-active'])
def test_reopening_reconciles_another_windows_archive_and_keeps_explicit_archive_views(engine, other_active):
    async def scenario(url):
        async with browser_session(engine) as browser:
            reader, archiver = await browser.new_page(viewport=VIEWPORT), await browser.new_page(viewport=VIEWPORT)
            errors = []
            reader.on('pageerror', lambda error: errors.append(str(error)))
            archiver.on('pageerror', lambda error: errors.append(str(error)))
            for page in [reader, archiver]:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator(f'#agent-thread [data-thread-open="{"a" * 32}"]').click()
            await reader.locator('#agent-question').fill('Keep this draft after another window archives it')
            await reader.locator('#agent-close').click()
            for letter in ['a', 'b', 'c'] if not other_active else ['a']:
                await archiver.locator(f'[data-thread-archive="{letter * 32}"]').click()
                await expect(archiver.locator(f'#agent-thread [data-thread-open="{letter * 32}"]')).to_have_count(0)
            await reader.locator('#btn-agent').click()
            active = reader.locator('#agent-thread [aria-current="true"]')
            await expect(active).to_have_count(1)
            await expect(active).not_to_have_attribute('data-thread-open', 'a' * 32)
            await expect(reader.locator('#agent-question')).to_be_enabled()
            if not other_active:
                await expect(active).to_have_text('New conversation')
                await expect(reader.locator('#agent-messages')).to_be_empty()
            # Explicit archive browsing remains stable when the dialog is reopened.
            await reader.locator('#agent-archive summary').click()
            await reader.locator(f'#agent-archived [data-thread-open="{"a" * 32}"]').click()
            await expect(reader.locator('#agent-question')).to_be_disabled()
            await expect(reader.locator('#agent-question')).to_have_value('Keep this draft after another window archives it')
            await reader.locator('#agent-close').click()
            await reader.locator('#btn-agent').click()
            await expect(reader.locator('#agent-archived [aria-current="true"]')).to_have_attribute('data-thread-open', 'a' * 32)
            await expect(reader.locator('#agent-messages')).to_contain_text('improved recovery')
            # Restoration in the second window returns the original draft to use.
            await reader.locator('#agent-close').click()
            await archiver.locator('#agent-archive summary').click()
            await archiver.locator(f'[data-thread-archive="{"a" * 32}"]').click()
            await expect(archiver.locator('#agent-thread [aria-current="true"]')).to_have_attribute('data-thread-open', 'a' * 32)
            await reader.locator('#btn-agent').click()
            await expect(active).to_have_attribute('data-thread-open', 'a' * 32)
            await expect(reader.locator('#agent-question')).to_be_enabled()
            await expect(reader.locator('#agent-question')).to_have_value('Keep this draft after another window archives it')
            assert len(httpx.get(url + '/api/chats').json()['chats']) == 3
            assert not errors

    with _server() as url:
        seed_conversations(url)
        run_browser(scenario(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_lost_archive_reconciliation_keeps_note_actions_blocked_when_reopened(engine):
    async def scenario(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport=VIEWPORT)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator(f'#agent-thread [data-thread-open="{"a" * 32}"]').click()
            held, release = asyncio.Event(), asyncio.Event()
            writes = []
            page.on('request', lambda request: writes.append(request.url)
                    if request.method in ('POST', 'PUT') and '/api/' in request.url else None)

            async def lose_patch(route):
                if route.request.method == 'PATCH':
                    await route.fetch()
                    await route.abort()
                else:
                    await route.continue_()

            async def hold_lists(route):
                held.set()
                await release.wait()
                await route.continue_()

            await page.route('**/api/chats/*', lose_patch)
            await page.route('**/api/chats?all_scopes=true', hold_lists)
            await page.locator(f'[data-thread-archive="{"a" * 32}"]').click()
            await asyncio.wait_for(held.wait(), timeout=10)
            try:
                # Reopening redraws controls while recovery is still waiting on its list.
                await page.locator('#agent-close').click()
                await page.locator('#btn-agent').click()
                button = page.locator('[data-agent-action]')
                await expect(button).to_be_disabled()
                await button.dispatch_event('click')
                assert writes == []
                assert await page.evaluate('Boolean(activeAgentChat().archiving)')
            finally:
                release.set()
            await expect(page.locator('#agent-thread [aria-current="true"]')).not_to_have_attribute('data-thread-open', 'a' * 32)
            await expect(page.locator('#agent-question')).to_be_enabled()
            assert len(httpx.get(url + '/api/chats').json()['chats']) == 3

    with _server() as url:
        seed_conversations(url)
        run_browser(scenario(url))
