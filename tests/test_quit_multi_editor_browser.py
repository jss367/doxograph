"""The final native flush must protect all editors until every save finishes."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models, open_note, seed


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_final_quit_does_not_lose_edit_in_an_already_flushed_editor(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'PATCH':
                    started.set()
                    await release.wait()
                await route.continue_()

            with _server() as url:
                await page.goto(url)
                await page.locator('#workspace-notes-keep').check()
                await open_note(page, url)
                workspace = page.locator('#workspace-notes')
                await expect(workspace).to_be_visible()
                await workspace.fill('Saved workspace text')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.clock.install(time=1000000)
                await page.clock.pause_at(1000001)
                await page.route('**/api/papers/paper', hold)
                await page.locator('textarea[data-note="paper"]').fill('Last paper edit')
                await workspace.focus()
                native = Path('native/Sources/WebWindow.swift').read_text()
                body = native.split('webView.callAsyncJavaScript(', 1)[1]
                script = body.split(chr(34) * 3)[1] if body.lstrip().startswith(chr(34) * 3) else body.split(chr(34))[1]
                await page.evaluate('() => { window.finalNativeFlush = async freezeEditing => {' + script + '}; }')
                await page.evaluate('''() => {
                  window.finalQuitReply = null;
                  void window.finalNativeFlush(true).then(result => { window.finalQuitReply = result; });
                }''')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    # WorkspaceNotes.flush already returned while the paper save waits.
                    await page.keyboard.type(' Typed after workspace flush')
                    release.set()
                    await page.wait_for_function('window.finalQuitReply !== null')
                    assert await page.evaluate('window.finalQuitReply') is True
                    saved = httpx.get(url + '/api/notebook').json()['notes']
                    assert saved == await workspace.input_value()
                    assert httpx.get(url + '/api/papers/paper').json()['notes'] == 'Last paper edit'
                finally:
                    release.set()

    run_browser(scenario())
