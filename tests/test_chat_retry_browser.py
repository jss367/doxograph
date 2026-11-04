"""Retry waits keep their submitted request and save only the resulting answer."""
import asyncio
import os
from datetime import datetime, timezone

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from browser_support import run_browser
from test_ask_browser import _server, seed


@pytest.fixture(autouse=True)
def block_retry_test_providers(monkeypatch):
    from doxograph import agent, chat, claude_code, codex_cli, extract

    def blocked(*args, **kwargs):
        pytest.fail('Retry browser tests must intercept every model request')

    for module, name in [(agent, 'answer'), (chat, 'answer'), (claude_code, '_run'),
                         (codex_cli, '_run'), (extract, 'client')]:
        monkeypatch.setattr(module, name, blocked)


@pytest.mark.browser
@pytest.mark.parametrize('header,delay', [
    ('Wed, 04 Nov 2026 21:02:00 GMT', 120000), ('120', 120000),
    ('Wed, 04 Nov 2026 20:59:59 GMT', 15000), ('malformed', 15000),
])
@pytest.mark.parametrize('move', [False, True])
def test_retry_wait_preserves_request_and_saves_once(header, delay, move):
    seed()

    async def scenario():
        # A local runtime can select an installed browser; CI uses Playwright's pin.
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=os.environ.get('DOXOGRAPH_TEST_CHROMIUM'))
            try:
                page = await browser.new_page()
                requests, saves, errors = [], [], []
                saved_answer = asyncio.Event()
                page.on('pageerror', lambda error: errors.append(str(error)))

                async def answer(route):
                    requests.append((route.request.headers['x-doxograph-workspace'],
                                     route.request.post_data_json))
                    if len(requests) == 1:
                        await route.fulfill(status=502, headers={
                            'X-Doxograph-Retryable': 'true', 'Retry-After': header},
                            json={'detail': 'Busy'})
                    else:
                        await route.fulfill(json={'answer': 'Recovered answer', 'assistant': 'Mock',
                            'scope': {**route.request.post_data_json['scope'], 'papers': ['a', 'b', 'c']}})

                async def save(route):
                    if route.request.method == 'PUT':
                        saves.append((route.request.headers['x-doxograph-workspace'],
                                      route.request.post_data_json))
                        response = await route.fetch()
                        await route.fulfill(response=response)
                        saved_answer.set()
                    else:
                        await route.continue_()

                await page.route('**/api/ask', answer)
                await page.route('**/api/chats/*', save)
                with _server() as url:
                    other = httpx.post(url + '/api/workspaces', json={'name': 'Other'}).json()['workspace']['id']
                    await page.goto(url)
                    await page.locator('#btn-agent:enabled').click()
                    await page.wait_for_function('agentChats.active && !agentChats.active.reading')
                    now = datetime(2026, 11, 4, 21, tzinfo=timezone.utc)
                    await page.clock.install(time=now)
                    await page.clock.pause_at(now)
                    await page.locator('#agent-question').fill('Original question')
                    await page.locator('#agent-send').click()
                    await expect(page.locator('#agent-status')).to_contain_text(f'Retrying in {delay // 1000} seconds')
                    owner = await page.evaluate('agentChats.active.id')
                    if move:
                        # Move both the selected thread and workspace during the wait.
                        await page.locator('#agent-clear').click()
                        await page.locator('#agent-question').fill('New thread draft')
                        await page.locator('#workspace').select_option(other)
                        await page.wait_for_function('id => workspaceClient.workspaceId === id && !workspaceController.pending', arg=other)
                    await page.clock.fast_forward(delay - 1)
                    assert len(requests) == 1
                    assert not saves
                    await page.clock.fast_forward(1)
                    # A real server read verifies the saved history, not only the outgoing PUT.
                    await asyncio.wait_for(saved_answer.wait(), 5)
                    assert requests[0] == requests[1]
                    assert requests[0][0] == 'default'
                    assert requests[0][1]['conversation_id'] == owner
                    assert requests[0][1]['messages'] == [{'role': 'user', 'content': 'Original question'}]
                    assert len(saves) == 1
                    assert saves[0][0] == 'default'
                    await page.wait_for_function('workspaceClient.pendingMutations === 0')
                    chats = httpx.get(url + '/api/chats?all_scopes=true',
                        headers={'X-Doxograph-Workspace': 'default'}).json()['chats']
                    saved = next(chat for chat in chats if chat['id'] == owner)
                    assert [m['content'] for m in saved['messages']] == ['Original question', 'Recovered answer']
                    if move:
                        await expect(page.locator('#agent-question')).to_have_value('New thread draft')
                        await expect(page.locator('#agent-messages')).not_to_contain_text('Recovered answer')
                    else:
                        await expect(page.locator('#agent-messages')).to_contain_text('Recovered answer')
                    assert not errors
            finally:
                await browser.close()
    run_browser(scenario())
