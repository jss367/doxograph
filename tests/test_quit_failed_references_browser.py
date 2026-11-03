"""A failed reference submission stays recoverable through quit/Stay/retry."""
import asyncio

import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models
from test_quit_page_imports_browser import begin_quit, initialize


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('failure', ['rejected', 'lost-response'])
@pytest.mark.parametrize('timing', ['before-quit', 'during-quit'])
def test_failed_references_block_quit_and_stay_allows_explicit_retry(engine, failure, timing):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            attempts = []

            async def ingest(route):
                attempts.append(route.request.post_data_json)
                if len(attempts) == 1:
                    started.set()
                    await release.wait()
                    if failure == 'rejected':
                        await route.fulfill(status=507, json={'detail':'Synthetic disk full'})
                    else:
                        await route.abort()
                else:
                    await route.fulfill(status=200, json={'queued':1, 'known':[], 'unknown':[]})

            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/ingest', ingest)
                await page.locator('#refs').fill('Keep this reference draft')
                await page.evaluate('void addReferences()')
                await asyncio.wait_for(started.wait(), 5)
                try:
                    if timing == 'before-quit':
                        release.set()
                        await expect(page.locator('#import-status')).to_contain_text('Could not add')
                    await begin_quit(page, freeze)
                    if timing == 'during-quit':
                        assert await page.evaluate('window.quitReady') is None
                        release.set()
                    await page.wait_for_function('window.quitReady !== null')
                    assert await page.evaluate('window.quitReady') is False
                    assert await page.evaluate('window.quitHealth') is None
                    await expect(page.locator('#refs')).to_have_value('Keep this reference draft')
                    # No automatic retry on repeated flush; the same draft remains unsafe.
                    assert await page.evaluate('window.nativeQuitSave(true)') is False
                    assert len(attempts) == 1
                    await page.evaluate('() => {' + resume + '}')
                    await page.evaluate('addReferences()')
                    await expect(page.locator('#refs')).to_have_value('')
                    assert len(attempts) == 2
                    assert attempts[0]['text'] == attempts[1]['text'] == 'Keep this reference draft'
                    assert await page.evaluate('window.nativeQuitSave(true)') is True
                finally:
                    release.set()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_clearing_failed_reference_text_explicitly_allows_quit(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/ingest', lambda route: route.fulfill(status=503, json={'detail':'Synthetic refusal'}))
                await page.locator('#refs').fill('Keep this reference draft')
                await page.evaluate('addReferences()')
                await begin_quit(page, freeze)
                await page.wait_for_function('window.quitReady !== null')
                assert await page.evaluate('window.quitReady') is False
                await page.evaluate('() => {' + resume + '}')
                await page.locator('#refs').fill('')
                assert await page.evaluate('window.nativeQuitSave(true)') is True

    run_browser(scenario())
