"""Unsubmitted references and acknowledged unknown lines have different lifecycles."""
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models
from test_quit_page_imports_browser import begin_quit, initialize


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('input_kind', ['typed', 'pasted', 'dom-only'])
def test_never_submitted_references_block_quit_until_explicitly_cleared(engine, input_kind):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                resume, freeze = await initialize(page, url)
                if input_kind == 'typed':
                    await page.locator('#refs').fill('Never submitted writing')
                else:
                    await page.evaluate('pasted => { const field = document.querySelector("#refs"); field.value = "Never submitted writing"; if (pasted) field.dispatchEvent(new InputEvent("input", {bubbles:true,inputType:"insertFromPaste"})); }', input_kind == 'pasted')
                await begin_quit(page, freeze)
                await page.wait_for_function('window.quitReady !== null')
                assert await page.evaluate('window.quitReady') is False
                await expect(page.locator('#refs')).to_have_value('Never submitted writing')
                await page.evaluate('() => {' + resume + '}')
                await page.locator('#refs').fill('')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.locator('#refs').fill('Never submitted writing')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('change', ['none', 'replace', 'clear-retype', 'clear', 'edit-revert'])
def test_acknowledged_unknown_lines_are_safe_until_the_next_input_edit(engine, change):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            requests = []

            async def ingest(route):
                requests.append(route.request.post_data_json['text'])
                await route.fulfill(status=200, json={'queued':0, 'known':[], 'unknown':['Unknown line']})

            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/ingest', ingest)
                await page.locator('#refs').fill('Unknown line')
                await page.evaluate('addReferences()')
                await expect(page.locator('#refs')).to_have_value('Unknown line')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                if change == 'replace':
                    await page.locator('#refs').fill('New line')
                elif change in ['clear-retype', 'clear']:
                    await page.locator('#refs').fill('')
                    if change == 'clear-retype':
                        await page.locator('#refs').fill('Unknown line')
                elif change == 'edit-revert':
                    await page.locator('#refs').fill('New line')
                    await page.locator('#refs').fill('Unknown line')
                unsafe = change not in ['none', 'clear']
                before = await page.locator('#refs').input_value()
                await begin_quit(page, freeze)
                await page.wait_for_function('window.quitReady !== null')
                assert await page.evaluate('window.quitReady') is not unsafe
                await expect(page.locator('#refs')).to_have_value(before)
                assert requests == ['Unknown line']
                if unsafe:
                    await page.evaluate('() => {' + resume + '}')
                    await page.evaluate('addReferences()')
                    assert requests[-1] == before
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(scenario())
