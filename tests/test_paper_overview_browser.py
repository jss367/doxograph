"""Paper overviews keep context, personal notes, and reading progress together."""

from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _poll, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_paper_overview_keeps_context_notes_and_reading_position(engine, width):
    config.set_ai_enabled(True)
    paper = store.new_paper('paper', title='Understanding model oversight',
                            authors=['Alex Smith', 'Taylor Jones'], year=2026)
    paper.update(summary='Tests oversight across <three> evaluation settings.',
                 relevance='Helps explain when oversight succeeds & why it fails.',
                 notes='Compare the evaluation conditions with our study.')
    store.save_paper(paper)
    store.pdf_path('paper').write_bytes(minimal_pdf(['Introduction.', 'Evaluation conditions.', 'Limitations.']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['reading'] = [{'paper': 'paper', 'status': 'reading', 'page': 2, 'bookmarks': []}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url + '/#paper=paper')
                await page.locator('#reader .pdf-page[data-page="2"]:not(.pending)').wait_for()
                overview = page.get_by_role('region', name='Paper overview', exact=True)
                await expect(overview.locator('.paper-summary')).to_contain_text(paper['summary'])
                await expect(overview.locator('.paper-relevance')).to_contain_text(paper['relevance'])
                await expect(overview.locator('.note')).to_contain_text(paper['notes'])
                await expect(overview.locator('[data-paper-progress]')).to_have_text('Reading · Page 2')
                await expect(page.locator('.paper-about')).to_have_count(0)
                actions = page.locator('.reading-actions')
                await expect(actions.get_by_role('button', name='Read', exact=True)).to_be_visible()
                await expect(actions.get_by_role('button', name='Ask about this paper', exact=True)).to_be_enabled()
                await expect(actions.get_by_role('button', name='View reading queue', exact=True)).to_be_visible()
                await expect(actions.get_by_role('button', name='Add claim by hand', exact=True)).to_be_hidden()
                await actions.get_by_role('button', name='Read', exact=True).focus()
                await _poll(page)
                await expect(actions.get_by_role('button', name='Read', exact=True)).to_be_focused()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path=f'.context/paper-overview-{width}.png', full_page=True)

                await actions.get_by_role('button', name='Ask about this paper', exact=True).click()
                await expect(page.locator('#library-agent')).to_be_visible()
                await expect(page.locator('#agent-scope')).to_contain_text(paper['title'])
                await page.locator('#agent-close').click()
                await overview.locator('[data-act="edit-note"]').click()
                note = overview.locator('textarea[data-note="paper"]')
                await note.fill('An unfinished thought about these conditions.')
                await _poll(page)
                await expect(note).to_have_value('An unfinished thought about these conditions.')

                # The overview stays available with the PDF put away. Read
                # restores the saved page without consuming the note draft.
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await expect(page.locator('#reader')).to_be_hidden()
                await expect(overview).to_be_visible()
                await expect(note).to_have_value('An unfinished thought about these conditions.')
                await actions.get_by_role('button', name='Read', exact=True).click()
                await expect(page.locator('#reader-page')).to_have_value('2')
                await expect(note).to_have_value('An unfinished thought about these conditions.')
                await note.focus()
                await page.locator('[data-tool="next-page"]').dispatch_event('click')
                await expect(overview.locator('[data-paper-progress]')).to_have_text('Reading · Page 3')
                await expect(note).to_have_value('An unfinished thought about these conditions.')
                await expect(note).to_be_focused()
                await overview.locator('[data-act="save-note"]').click()
                await ready(page)
                await expect(overview.locator('.note')).to_contain_text('An unfinished thought about these conditions.')
                # Reloading cancels a library read still in flight, which Linux WebKit
                # reports as an access-control page error. Reads come from the poller
                # and also from the refresh after an action such as saving the note
                # or turning the page, so stop the poller and wait out every read.
                await page.evaluate('poller.stop()')
                await page.wait_for_function(
                    'workspaceClient.pullsInFlight === 0 && workspaceClient.pendingMutations === 0')
                await page.reload()
                await expect(page.locator('#reader-page')).to_have_value('3')
                await expect(overview.locator('[data-paper-progress]')).to_have_text('Reading · Page 3')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_paper_overview_without_analysis_or_pdf_can_be_noted_and_queued(engine):
    config.set_ai_enabled(True)
    store.save_paper(store.new_paper('paper', title='A source to read later'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + '/#paper=paper')
                overview = page.get_by_role('region', name='Paper overview', exact=True)
                await expect(overview.locator('[data-paper-progress]')).to_have_text('Not started')
                await expect(overview.locator('.paper-summary')).to_contain_text('No summary yet.')
                await expect(overview.locator('.paper-relevance')).to_have_count(0)
                await expect(page.get_by_role('button', name='Read', exact=True)).to_be_disabled()
                await expect(page.get_by_role('button', name='Ask about this paper', exact=True)).to_be_enabled()
                await overview.locator('textarea[data-note]').fill('A question to return to.')
                await page.get_by_role('button', name='Add to reading queue', exact=True).click()
                await expect(page.locator('#research-tools')).to_be_visible()
                await ready(page)
                await page.locator('[data-tool="close"]').click()
                await expect(page.locator('#research-tools')).to_be_hidden()
                await expect(overview.locator('[data-paper-progress]')).to_have_text('In reading queue')
                await expect(overview.locator('textarea[data-note]')).to_have_value('A question to return to.')
                await page.locator('.reading-actions').get_by_role('button', name='View reading queue', exact=True).click()
                await expect(page.locator('[data-reading="paper"]')).to_have_count(1)
                await ready(page)
                await page.locator('[data-reading-status="paper"]').select_option('finished')
                await ready(page)
                await page.locator('[data-tool="close"]').click()
                await expect(page.locator('#research-tools')).to_be_hidden()
                await expect(overview.locator('[data-paper-progress]')).to_have_text('Finished · Page 1')
                await overview.locator('[data-act="save-note"]').click()
                await expect(overview.locator('.note')).to_contain_text('A question to return to.')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Add claim by hand', exact=True).click()
                await expect(page.locator('form[data-form="__new__"]')).to_be_visible()

    run_browser(scenario())
