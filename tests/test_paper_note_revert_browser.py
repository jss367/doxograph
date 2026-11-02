"""Reverted paper-note drafts retire only after outstanding writes settle."""
import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from doxograph import agent, chat, claude_code, codex_cli, extract

    def blocked(*args, **kwargs):
        pytest.fail('Paper-note tests must never call providers')

    for module, name in ((agent, 'answer'), (chat, 'answer'), (claude_code, '_run'),
                         (codex_cli, '_run'), (extract, 'client')):
        monkeypatch.setattr(module, name, blocked)


def seed():
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Synthetic paper', notes='Stored note'))
    store.save_paper(store.new_paper('other', title='Other paper'))


async def open_note(page, url):
    await page.goto(url + '/#paper=paper')
    await page.locator('[data-act="edit-note"][data-paper="paper"]').click()
    await expect(page.locator('textarea[data-note="paper"]')).to_have_value('Stored note')


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('leave', ['done', 'park'])
def test_reverted_note_does_not_reopen_or_block_workspace(engine, leave):
    seed()
    other_workspace = config.create_workspace('Synthetic second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            patches = []

            async def record(route):
                if route.request.method == 'PATCH':
                    patches.append(route.request.post_data_json)
                await route.continue_()

            with _server() as url:
                await open_note(page, url)
                await page.route('**/api/papers/paper', record)
                # Both input events run in one task, before the debounce can fire.
                await page.locator('textarea[data-note="paper"]').evaluate('''field => {
                  for (const value of ['Temporary edit', 'Stored note']) {
                    field.value = value;
                    field.dispatchEvent(new Event('input', {bubbles:true}));
                  }
                }''')
                if leave == 'done':
                    await page.locator('[data-act="done-note"][data-paper="paper"]').click()
                    await expect(page.locator('textarea[data-note="paper"]')).to_have_count(0)
                else:
                    await page.locator('#papers [data-paper="other"]').click()
                assert await page.evaluate('Object.keys(V.noteDrafts)') == []
                await page.evaluate('''id => {
                  window.noteSwitchFinished = false;
                  void switchWorkspace(id).then(() => { window.noteSwitchFinished = true; });
                }''', other_workspace)
                await page.wait_for_function('window.noteSwitchFinished || document.querySelector("#ask[open]")')
                await expect(page.locator('#ask')).not_to_be_visible()
                await page.wait_for_function('window.noteSwitchFinished')
                assert await page.evaluate('workspaceClient.workspaceId') == other_workspace
                assert patches == []

    run_browser(scenario())
    assert store.load_paper('paper')['notes'] == 'Stored note'


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('latest', ['Stored note', 'Newest edit'], ids=['revert-in-flight', 'newer-in-flight'])
def test_inflight_note_keeps_reversion_or_newer_edit_until_written(engine, latest):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            patches = []

            async def hold(route):
                if route.request.method != 'PATCH':
                    await route.continue_()
                    return
                index = len(patches)
                patches.append(route.request.post_data_json)
                assert index < 2
                started[index].set()
                await release[index].wait()
                await route.continue_()

            with _server() as url:
                await open_note(page, url)
                await page.route('**/api/papers/paper', hold)
                field = page.locator('textarea[data-note="paper"]')
                await field.fill('Pending write')
                await page.evaluate('void noteAutosave("paper").flush()')
                try:
                    await asyncio.wait_for(started[0].wait(), 5)
                    await field.fill(latest)
                    assert await page.evaluate('V.noteDrafts.paper') == latest
                    await page.evaluate('void closeNoteEditor()')
                    await expect(field).to_be_visible()
                    release[0].set()
                    await asyncio.wait_for(started[1].wait(), 5)
                    assert patches[1]['notes'] == latest
                    assert store.load_paper('paper')['notes'] == 'Pending write'
                    assert await page.evaluate('V.noteDrafts.paper') == latest
                    release[1].set()
                    await page.wait_for_function('!paperNotesSaving() && !Object.keys(V.noteDrafts).length')
                    await expect(field).to_have_count(0)
                    assert store.load_paper('paper')['notes'] == latest
                    assert [p['notes'] for p in patches] == ['Pending write', latest]
                finally:
                    for event in release:
                        event.set()

    run_browser(scenario())
