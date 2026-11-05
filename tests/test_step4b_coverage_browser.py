"""Retained review and recovery behavior after the step4b interface cuts."""
import json

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


@pytest.mark.browser
def test_zero_result_filter_hides_focused_review_without_polling():
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('synthetic', title='Synthetic study'))
    store.add_claim('synthetic', {'text': 'Synthetic retained finding'})

    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + '/#view=claims')
                await page.wait_for_function('libraryInitialized')
                await expect(page.locator('#selection-count')).to_have_text('1 claim')
                await expect(page.locator('#btn-focus-review')).to_be_visible()
                await page.evaluate('poller.stop()')
                await page.locator('#q').fill('no synthetic matching claims whatsoever')
                await expect(page.locator('#selection-count')).to_have_text('0 claims')
                await expect(page.locator('#btn-focus-review')).to_be_hidden()
    run_browser(scenario())


@pytest.mark.browser
def test_previous_version_ui_restores_the_persisted_notebook_notes():
    config.set_ai_enabled(False)
    path = config.data_dir() / 'notebook.json'
    store.write_json(path, {'revision': 1, 'notes': 'Original conclusion'})
    store.write_json(path, {'revision': 2, 'notes': 'Later conclusion'})

    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.wait_for_function('libraryInitialized')
                await page.locator('#workspace-actions > summary').click()
                await page.locator('#btn-workspace-backups').click()
                await expect(page.locator('.backup-version')).to_have_count(1)
                await page.locator('[data-backup-restore]').first.click()
                await page.locator('#ask-ok').click()
                await expect(page.locator('#backups-status')).to_contain_text('Restored.')
                assert json.loads(path.read_text(encoding='utf-8'))['notes'] == 'Original conclusion'
    run_browser(scenario())
