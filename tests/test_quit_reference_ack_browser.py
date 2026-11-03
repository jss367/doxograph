"""A definitive retry acknowledges retained unknown lines, not newer input."""
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
@pytest.mark.parametrize('boundary', ['unknown-only', 'partial', 'repeat-failure', 'edited-in-flight',
                                    'reverted-in-flight', 'removed-in-flight', 'edited-before-retry'])
def test_acknowledged_retry_and_retained_reference_input(engine, boundary):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            attempts = []

            async def ingest(route):
                attempts.append(route.request.post_data_json['text'])
                if len(attempts) == 1:
                    await route.fulfill(status=507, json={'detail':'Synthetic initial failure'})
                elif len(attempts) == 2:
                    started.set()
                    await release.wait()
                    if boundary == 'repeat-failure':
                        await route.fulfill(status=503, json={'detail':'Synthetic retry failure'})
                    else:
                        await route.fulfill(status=200, json={'queued':1 if boundary == 'partial' else 0,
                                                             'known':[], 'unknown':['Unrecognized line']})
                else:
                    await route.fulfill(status=200, json={'queued':1, 'known':[], 'unknown':[]})

            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/ingest', ingest)
                await page.locator('#refs').fill('Recognized line\nUnrecognized line')
                await page.evaluate('addReferences()')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                if boundary == 'edited-before-retry':
                    await page.locator('#refs').fill('Edited before retry')
                submitted = await page.locator('#refs').input_value()
                await page.evaluate('void addReferences()')
                await asyncio.wait_for(started.wait(), 5)
                try:
                    if boundary in ['edited-in-flight', 'reverted-in-flight']:
                        await page.locator('#refs').fill('New unsubmitted writing')
                        if boundary == 'reverted-in-flight':
                            await page.locator('#refs').fill(submitted)
                    elif boundary == 'removed-in-flight':
                        await page.locator('#refs').fill('')
                    await begin_quit(page, freeze)
                    assert await page.evaluate('window.quitReady') is None
                    release.set()
                    await page.wait_for_function('window.quitReady !== null')
                    unsafe = boundary in ['repeat-failure', 'edited-in-flight']
                    assert await page.evaluate('window.quitReady') is not unsafe
                    expected = submitted if boundary == 'repeat-failure' else 'New unsubmitted writing' if boundary == 'edited-in-flight' else '' if boundary == 'removed-in-flight' else 'Unrecognized line'
                    await expect(page.locator('#refs')).to_have_value(expected)
                    assert len(attempts) == 2
                    assert await page.evaluate('window.nativeQuitSave(true)') is not unsafe
                    if unsafe:
                        await page.evaluate('() => {' + resume + '}')
                        await page.evaluate('addReferences()')
                        assert attempts[2] == expected
                        await expect(page.locator('#refs')).to_have_value('')
                        assert await page.evaluate('window.nativeQuitSave(true)') is True
                finally:
                    release.set()

    run_browser(scenario())
