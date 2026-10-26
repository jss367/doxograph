"""Provider settings remain usable in the browser and the native WebKit shell."""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect

from doxograph import config
from test_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_choose_local_provider_and_restore_each_profile(engine):
    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await page.get_by_label('Provider', exact=True).select_option('ollama')
                await page.get_by_label('Model', exact=True).fill('my-local-model')
                await expect(page.get_by_label('Base URL', exact=True)).to_have_value('http://localhost:11434')
                await page.get_by_role('button', name='Save provider', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Provider saved')
                assert config.analysis_config().model == 'my-local-model'
                await page.reload()
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await expect(page.get_by_label('Provider', exact=True)).to_have_value('ollama')
                await expect(page.get_by_label('Model', exact=True)).to_have_value('my-local-model')
                await expect(page.locator('#stats')).not_to_contain_text('no API key')
                # Polling must not overwrite an unsaved provider/model draft.
                await page.get_by_label('Model', exact=True).fill('unsaved-local-model')
                await page.get_by_label('Provider', exact=True).select_option('openai-compatible')
                await page.get_by_label('Model', exact=True).fill('studio-model')
                await page.get_by_label('Base URL', exact=True).fill('http://localhost:1234/v1')
                await page.get_by_label('Provider', exact=True).select_option('ollama')
                await expect(page.get_by_label('Model', exact=True)).to_have_value('unsaved-local-model')
                await page.get_by_label('Provider', exact=True).select_option('openai-compatible')
                await expect(page.get_by_label('Model', exact=True)).to_have_value('studio-model')
                await page.get_by_role('button', name='Save provider', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Provider saved')
                await page.get_by_label('Provider', exact=True).select_option('ollama')
                await expect(page.get_by_label('Model', exact=True)).to_have_value('my-local-model')
                await page.get_by_label('Model', exact=True).fill('my-local-model')
                await page.get_by_role('button', name='Save provider', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Provider saved')
                screenshot = Path('.context') / f'ai-provider-settings-{engine}.png'
                screenshot.parent.mkdir(exist_ok=True)
                await page.screenshot(path=str(screenshot))
                assert not errors
            await browser.close()
    asyncio.run(scenario())
