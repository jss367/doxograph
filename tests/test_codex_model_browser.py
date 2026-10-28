"""Select and save a subscription model in Chromium and the native WebKit engine."""

import asyncio
import json
from pathlib import Path

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_subscription_model_picker_persists_choices_and_preserves_loading_draft(engine):
    config.set_ai_provider('codex')
    config.save_codex_model('')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            requested, release = asyncio.Event(), asyncio.Event()

            async def catalog(route):
                requested.set()
                await release.wait()
                await route.fulfill(content_type='application/json', body=json.dumps({'models': [
                    {'id': 'gpt-6.1-sol', 'name': 'GPT-6.1 Sol'},
                    {'id': 'gpt-6-astra', 'name': 'GPT-6 Astra'},
                ]}))

            await page.route('**/api/settings/codex/models', catalog)
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await requested.wait()
                await page.get_by_label('ChatGPT model', exact=True).select_option('custom')
                await page.get_by_label('Custom model ID').fill('my-model')
                release.set()
                await expect(page.locator('#codex-model-refresh')).to_be_enabled()
                await expect(page.get_by_label('Custom model ID')).to_have_value('my-model')
                await page.get_by_label('ChatGPT model', exact=True).select_option('gpt-6-astra')
                await page.get_by_role('button', name='Save model', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Model saved.')
                assert config.model() == 'gpt-6-astra'
                await page.reload()
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await expect(page.get_by_label('ChatGPT model', exact=True)).to_have_value('gpt-6-astra')
                await expect(page.locator('#codex-current-model')).to_have_text('Current model: gpt-6-astra')
                screenshot = Path('.context') / f'chatgpt-model-settings-{engine}.png'
                await page.screenshot(path=str(screenshot))
                # A custom model survives reload even when absent from the catalog.
                await page.get_by_label('ChatGPT model', exact=True).select_option('custom')
                await page.get_by_label('Custom model ID').fill('custom-model')
                await page.get_by_role('button', name='Save model', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Model saved.')
                await page.reload()
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await expect(page.get_by_label('Custom model ID')).to_have_value('custom-model')
                await page.get_by_label('Model access', exact=True).select_option('api')
                await expect(page.locator('#ai-settings-status')).to_have_text('Model access saved.')
                await expect(page.locator('#codex-model-form')).to_be_hidden()
                await page.get_by_label('Model access', exact=True).select_option('codex')
                await expect(page.locator('#ai-settings-status')).to_have_text('Model access saved.')
                await expect(page.get_by_label('Custom model ID')).to_have_value('custom-model')
                await page.get_by_label('ChatGPT model', exact=True).select_option('')
                await page.get_by_role('button', name='Save model', exact=True).click()
                await expect(page.locator('#codex-current-model')).to_contain_text('Codex default (automatic)')
                assert config.model() == 'Codex default'
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
def test_model_picker_still_saves_custom_choice_when_discovery_fails():
    config.set_ai_provider('codex')
    config.save_codex_model('existing-model')

    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            await page.route('**/api/settings/codex/models', lambda route: route.fulfill(
                status=503, content_type='application/json', body='{"detail":"Codex is unavailable."}'))
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#ai-enabled')).to_be_enabled()
                await page.get_by_role('button', name='Settings', exact=True).click()
                await expect(page.locator('#codex-models-status')).to_contain_text('Codex is unavailable.')
                await expect(page.get_by_label('Custom model ID')).to_have_value('existing-model')
                await page.get_by_label('Custom model ID').fill('replacement-model')
                await page.get_by_role('button', name='Save model', exact=True).click()
                await expect(page.locator('#ai-settings-status')).to_contain_text('Model saved.')
                assert config.model() == 'replacement-model'

    run_browser(scenario())
