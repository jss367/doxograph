"""Resume reading and review from the library without a model call."""
import asyncio

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _polled, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_continue_reading_survives_reload_and_opens_the_saved_page(engine):
    config.set_ai_enabled(False)
    for key, title in [('one', 'A study of recovery'), ('two', 'A different paper')]:
        store.save_paper(store.new_paper(key, title=title))
        store.pdf_path(key).write_bytes(minimal_pdf(['First page.', 'Second page.']))
    store.add_claim('one', {'text': 'Recovery depends on the experimental conditions.', 'reviewed': False})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                card = page.locator('#continue-reading')
                await page.locator('#papers li[data-paper="one"]').click()
                await expect(card).to_contain_text('A study of recovery')
                await card.get_by_role('button', name='Resume at page 1').click()
                await ready(page)
                await page.get_by_role('button', name='Next page', exact=True).click()
                await ready(page)
                await page.get_by_role('button', name='Close research tools').click()
                await expect(card.get_by_role('button', name='Resume at page 2')).to_be_visible()

                # Reading a queued paper can differ from the library underneath it.
                await page.locator('#papers li[data-paper="two"]').click()
                await page.locator('[data-tool="reading"]').click()
                await ready(page)
                await page.locator('#research-body').get_by_role('button', name='Resume at page 2').click()
                await ready(page)
                await page.get_by_role('button', name='Close research tools').click()
                await page.reload()
                await expect(card).to_contain_text('A study of recovery')
                await card.get_by_role('button', name='Resume at page 2').click()
                await ready(page)
                await expect(page.locator('#reader-page')).to_have_value('2')
                await expect(page.locator('#research-body h3').first).to_have_text('A study of recovery')
                await page.get_by_role('button', name='Close research tools').click()

                # A fresh library URL still offers the last paper in this browser.
                await page.goto(url)
                await card.get_by_role('button', name='Open paper', exact=True).click()
                await page.wait_for_function("V.paper === 'one'")
                assert httpx.get(url + '/api/notebook').json()['reading'][0]['page'] == 2
                await page.screenshot(path='.context/continue-reading.png')
                assert not errors
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_continue_review_clears_filters_and_handles_missing_sources(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('one', title='A study without a PDF'))
    claims = [store.add_claim('one', {'text': text, 'reviewed': False})
              for text in ['First finding.', 'Second finding.']]
    store.save_paper(store.new_paper('two', title='Another source'))
    other = store.add_claim('two', {'text': 'A remaining finding.', 'reviewed': False})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="one"]').click()
                card = page.locator('#continue-reading')
                await expect(card).to_contain_text('No PDF available')
                assert not await card.locator('[data-tool="reader"]').count()
                await page.locator('#q').fill('nothing matches')
                await page.locator('#only-unverified').check()
                await card.get_by_role('button', name='Next unreviewed claim').click()
                await expect(page.locator(f'.claim.sel[data-claim="{claims[0]["id"]}"]')).to_be_visible()
                await expect(page.locator('#q')).to_have_value('')
                await expect(page.locator('#only-unverified')).not_to_be_checked()
                # Leaving the research form through the shortcut keeps its draft.
                await page.locator('#research-nav [data-view="research"]').click()
                await page.locator('#research-form [name="context"]').fill('A question to keep.')
                await card.get_by_role('button', name='Next unreviewed claim').click()
                await page.locator('#research-nav [data-view="research"]').click()
                await expect(page.locator('#research-form [name="context"]')).to_have_value('A question to keep.')
                await page.locator('#content [data-act="cancel-research"]').click()
                # Restore the first cursor so the reload can check the next one.
                await page.locator(f'.claim[data-claim="{claims[0]["id"]}"] .ctext').click()
                await page.goto(url)
                await expect(card).to_contain_text('Second finding.')
                await card.get_by_role('button', name='Next unreviewed claim').click()
                await expect(page.locator(f'.claim.sel[data-claim="{claims[1]["id"]}"]')).to_be_visible()

                # A removed cursor is harmless; reviewing all of this paper falls
                # through to another paper regardless of the current selection.
                store.delete_claim('one', claims[1]['id'])
                store.update_claim('one', claims[0]['id'], {'reviewed': True})
                await _polled(page)
                await expect(card).to_contain_text('A remaining finding.')
                await card.get_by_role('button', name='Next unreviewed claim').click()
                await expect(page.locator(f'.claim.sel[data-claim="{other["id"]}"]')).to_be_visible()
                store.update_claim('two', other['id'], {'reviewed': True})
                await _polled(page)
                await expect(card).to_contain_text('All claims reviewed.')
                assert not await card.locator('[data-continue="review"]').count()
                store.delete_paper('two')
                await _polled(page)
                await expect(card).to_be_hidden()
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
def test_continue_shortcuts_are_scoped_to_each_workspace():
    store.save_paper(store.new_paper('same', title='Default research'))
    other = config.create_workspace('Other research')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('same', title='Other workspace research'))

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                card = page.locator('#continue-reading')
                await page.locator('#papers li[data-paper="same"]').click()
                await expect(card).to_contain_text('Default research')
                await page.locator('#workspace').select_option(other['id'])
                await page.locator('#papers').get_by_text('Other workspace research').wait_for()
                await expect(card).to_be_hidden()
                await page.locator('#papers li[data-paper="same"]').click()
                await expect(card).to_contain_text('Other workspace research')
                await page.locator('#workspace').select_option('default')
                await expect(card).to_contain_text('Default research')
                await page.reload()
                await expect(card).to_contain_text('Default research')
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_fresh_paper_links_replace_saved_visits_and_clear_the_old_cursor(engine):
    other = config.create_workspace('Other research')
    workspace_ids = ['default', other['id']]
    cursors = {}
    for workspace in workspace_ids:
        with config.use_workspace(workspace):
            for key, title in [('one', 'Previously visited paper'), ('two', 'Freshly linked paper')]:
                store.save_paper(store.new_paper(key, title=f'{workspace}: {title}'))
            store.add_claim('one', {'text': 'First old finding.', 'reviewed': False})
            cursors[workspace] = store.add_claim('one', {'text': 'Second old finding.', 'reviewed': False})['id']
            store.add_claim('two', {'text': 'New finding.', 'reviewed': False})

    async def saved(page, workspace):
        return await page.evaluate('(workspace) => JSON.parse(localStorage.getItem(`doxograph-continue:${workspace}`))', workspace)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            context = await browser.new_context()
            with _server() as url:
                # Both workspaces start with an actual saved paper and cursor.
                for workspace in workspace_ids:
                    page = await context.new_page()
                    await page.goto(f'{url}/#ws={workspace}&paper=one')
                    review = page.locator('#continue-reading [data-continue="review"]')
                    await review.click()
                    await review.click()
                    assert await saved(page, workspace) == {'paper': 'one', 'claim': cursors[workspace]}
                    await page.close()

                for workspace in workspace_ids:
                    # A new tab is a new document, unlike a same-page hash change.
                    page = await context.new_page()
                    await page.goto(f'{url}/#ws={workspace}&paper=two')
                    title = page.locator('#continue-reading .continue-item').first.locator('.continue-text')
                    await expect(title).to_have_text(f'{workspace}: Freshly linked paper')
                    assert await saved(page, workspace) == {'paper': 'two', 'claim': None}
                    if workspace == 'default':
                        assert await saved(page, other['id']) == {'paper': 'one', 'claim': cursors[other['id']]}
                    await page.reload()
                    await expect(title).to_have_text(f'{workspace}: Freshly linked paper')
                    await page.goto(f'{url}/#ws={workspace}')
                    await expect(title).to_have_text(f'{workspace}: Freshly linked paper')
                    await page.close()
            await browser.close()
    asyncio.run(scenario())
