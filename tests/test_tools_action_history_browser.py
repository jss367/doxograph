"""Async creation and action destinations respect history transitions."""
import asyncio

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server
from test_research_tools_browser import ready


@pytest.fixture(autouse=True)
def inert_tools(monkeypatch):
    from doxograph import agent, chat, claude_code, codex_cli, extract
    config.set_ai_enabled(False)

    def blocked(*args, **kwargs):
        pytest.fail('Unexpected provider in tools history controls')

    for module, name in ((agent, 'answer'), (chat, 'answer'), (claude_code, '_run'),
                         (codex_cli, '_run'), (extract, 'client')):
        monkeypatch.setattr(module, name, blocked)


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('away', ['library', 'reading'])
def test_away_and_back_during_creation_keeps_the_newer_origin_entry(engine, away):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'PUT':
                    started.set()
                    await release.wait()
                await route.continue_()

            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['boards'] = [{'id': 'b1', 'title': 'Original board', 'entries': []}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url + '/#view=tools&tool=boards&board=b1')
                await ready(page)
                await page.route('**/api/notebook', hold)
                await page.get_by_role('button', name='New board', exact=True).click()
                await page.locator('#ask-input').fill('Delayed new board')
                await page.locator('#ask').get_by_role('button', name='Create board', exact=True).click()
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await page.evaluate('away => openPageView(away === "library" ? "library" : "tools", away === "reading" ? {tool:"reading",board:null} : {})', away)
                    await page.go_back()
                    await page.wait_for_function('V.view === "tools" && V.tool === "boards" && V.board === "b1"')
                    history = await page.evaluate('history.length')
                    release.set()
                    await ready(page)
                    assert await page.evaluate('V.board') == 'b1'
                    assert await page.evaluate('history.length') == history
                    assert len(httpx.get(url + '/api/notebook').json()['boards']) == 2
                finally:
                    release.set()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('action,destination', [('add-board', 'boards'), ('queue-selection', 'reading'), ('save-search', 'searches')])
def test_action_destination_pushes_history_and_back_returns_to_compare(engine, action, destination):
    store.save_paper(store.new_paper('p', title='Synthetic selected paper'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-select-paper="p"]').first.check()
                await page.get_by_role('button', name='Compare & organize', exact=True).click()
                await ready(page)
                history = await page.evaluate('history.length')
                if action == 'save-search':
                    await page.locator('[data-tool="save-search"]').first.evaluate('button => button.click()')
                    await page.locator('#ask-input').fill('Synthetic saved filters')
                    await page.locator('#ask').get_by_role('button', name='Save search', exact=True).click()
                else:
                    await page.locator('#research-tools [data-tool="' + action + '"]').click()
                await ready(page)
                assert await page.evaluate('V.tool') == destination
                assert await page.evaluate('history.length') == history + 1
                await page.go_back()
                await ready(page)
                assert await page.evaluate('V.tool') == 'compare'

    run_browser(scenario())
