"""Evidence judgments are editable beside the claim and distinct from verification."""

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_manual_evidence_assessment(engine):
    config.set_ai_enabled(False)
    _paper('study', 'A study of monitoring')
    store.pdf_path('study').write_bytes(minimal_pdf('A study of monitoring.'))
    store.update_claim('study', 'study-c1', {'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#papers')).to_contain_text('1 to review')
                await page.locator('[data-act="edit"]').first.click()
                await page.locator('.evidence-assessment > summary').click()
                await page.locator('[name="conditions"]').fill('500 samples, one model')
                await page.locator('[name="limitations"]').fill('Same dataset as the earlier paper')
                await page.locator('[name="evidence_origin"]').fill('Evaluation set A')
                await page.locator('[name="evidence_relationship"]').select_option('shared_data')
                await page.locator('form[data-form]').get_by_role('button', name='Save', exact=True).click()
                await expect(page.locator('.evidence-quality').first).to_contain_text('Needs human review')
                await expect(page.locator('.evidence-quality').first).to_contain_text('Uses shared data')
                await expect(page.locator('#content')).to_contain_text('Same dataset as the earlier paper')
                await page.reload()
                await expect(page.locator('#content')).to_contain_text('Evaluation set A')

    run_browser(scenario())
