"""The reading layout gives space to the source without losing library state."""

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
def test_reading_layout_and_library_return(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Understanding model oversight',
                                     authors=['Alex Smith', 'Taylor Jones'], year=2026))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.', 'A second page.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await expect(page.locator('body')).to_have_class('reading-paper' + (' library-collapsed' if width < 800 else ''))
                await expect(page.locator('#continue-reading')).to_be_hidden()
                await expect(page.locator('#reading-claim-tools')).to_be_hidden()
                await expect(page.locator('[data-act="reextract"]')).to_be_hidden()
                reader = await page.locator('#pdf-scroll').bounding_box()
                pane = await page.locator('#reader').bounding_box()
                header = await page.locator('#paperhead').bounding_box()
                toolbar = await page.locator('.reader-toolbar').bounding_box()
                hint = await page.locator('#reader-hint').bounding_box()
                # Font metrics and wrapped controls vary by platform. The PDF
                # must follow its header/toolbar directly and fill the reader,
                # rather than begin before an arbitrary viewport coordinate.
                assert abs(pane['y'] - header['y'] - header['height']) <= 2
                assert abs(reader['y'] - toolbar['y'] - toolbar['height']) <= 2
                assert abs(reader['y'] + reader['height'] - hint['y']) <= 2
                assert reader['height'] > 0
                if width < 800:
                    assert abs(pane['height'] - page.viewport_size['height'] * .7) <= 2
                else:
                    main = await page.locator('#main').bounding_box()
                    assert abs(pane['y'] + pane['height'] - main['y'] - main['height']) <= 2
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')

                # Background refreshes keep an open menu and its keyboard focus.
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Check quotes', exact=True).focus()
                await _poll(page)
                await expect(page.get_by_role('button', name='Check quotes', exact=True)).to_be_focused()
                await page.keyboard.press('Escape')

                # Show/hide the library without replacing the mounted document.
                await page.evaluate('window.originalPage = document.querySelector("#reader .pdf-page")')
                await page.locator('#btn-reader-library').click()
                await page.locator('#btn-reader-library').click()
                assert await page.evaluate('originalPage === document.querySelector("#reader .pdf-page")')

                if width > 800:
                    await page.set_viewport_size({'width': 700, 'height': 1000})
                    await expect(page.locator('#side')).to_be_hidden()
                    await expect(page.locator('#btn-reader-library')).to_have_attribute('aria-expanded', 'false')
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await expect(page.locator('#side')).to_be_visible()

                # Filters are discoverable and do not silently disappear on exit.
                await page.keyboard.press('/')
                await expect(page.locator('#reading-claim-tools')).to_be_visible()
                await expect(page.locator('#btn-reader-filters')).to_have_attribute('aria-expanded', 'true')
                await expect(page.locator('#q')).to_be_focused()
                await page.keyboard.insert_text('Oversight')
                await ready(page)
                await page.locator('#btn-reader-filters').click()
                await expect(page.locator('#btn-reader-filters')).to_have_text('Claim filters · active')
                await page.keyboard.press('/')
                await expect(page.locator('#q')).to_be_focused()
                assert await page.locator('#q').evaluate('(field) => field.selectionStart === 0 && field.selectionEnd === field.value.length')
                await page.locator('#btn-reader-filters').click()
                await page.locator('#content [data-act="edit-note"]').click()
                await page.locator('textarea[data-note="paper"]').fill('My unfinished thought.')
                await page.locator('#btn-back-library').click()
                await expect(page.locator('body')).not_to_have_class('reading-paper')
                await expect(page.locator('#q')).to_have_value('Oversight')
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#content [data-act="edit-note"]').click()
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')

                # Escape dismisses the actions without cancelling the note draft.
                await page.locator('.paper-actions > summary').click()
                await _poll(page)
                await expect(page.locator('.paper-actions')).to_have_attribute('open', '')
                await page.keyboard.press('Escape')
                await expect(page.locator('.paper-actions')).not_to_have_attribute('open', '')
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await expect(page.locator('#reader')).to_be_hidden()
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')
                await page.get_by_role('button', name='Show PDF', exact=True).click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await page.locator('textarea[data-note="paper"]').fill('Saved from the reader.')
                await page.locator('.note [data-act="save-note"]').click()
                await ready(page)
                await expect(page.locator('textarea[data-note="paper"]')).to_have_count(0)
                await expect(page.locator('#content > .note')).to_contain_text('Saved from the reader.')
                assert store.load_paper('paper')['notes'] == 'Saved from the reader.'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_workspace_notes_keep_open_in_reading_layout(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Reading with workspace notes'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['The first page.', 'The second page.']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                editor = page.locator('#workspace-notes')
                await editor.fill('A question to keep beside the paper.')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                await page.locator('#workspace-notes-keep').check()
                await page.set_viewport_size({'width': width, 'height': 1000})
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                panel = page.locator('#workspace-notes-reading #workspace-notes-panel')
                await expect(panel).to_be_visible()
                assert await page.locator('#workspace-notes').count() == 1
                await editor.scroll_into_view_if_needed()
                await expect(editor).to_be_in_viewport()
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                assert await panel.evaluate('(node) => node.scrollWidth <= node.clientWidth')
                await editor.fill('A revised question, saved while reading.')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                await page.reload()
                await expect(panel).to_be_visible()
                await expect(page.locator('#workspace-notes-keep')).to_be_checked()
                await expect(editor).to_have_value('A revised question, saved while reading.')
                assert not errors
    run_browser(scenario())
