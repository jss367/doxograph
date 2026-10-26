"""Exercise model access selection and paper chat in the real page."""

import asyncio
import json

import pytest
from playwright.async_api import async_playwright

from doxograph import config, store
from test_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize('provider,assistant', [('claude_code', 'Claude'), ('codex', 'ChatGPT')])
def test_subscription_selection_and_paper_chat_keep_followups_and_failed_drafts(provider, assistant):
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            requests = []

            async def respond(route):
                messages = route.request.post_data_json['messages']
                requests.append(messages)
                if len(requests) == 2:
                    await route.fulfill(status=502, content_type='application/json',
                                        body=json.dumps({'detail': 'Usage limit reached'}))
                else:
                    await route.fulfill(content_type='application/json',
                                        body=json.dumps({'answer': 'Twenty adults (p. 1).', 'model': 'test', 'assistant': assistant}))

            await page.route('**/api/papers/study/chat', respond)
            await page.goto(url)
            await page.locator('#btn-settings').click()
            await page.locator('#ai-provider').select_option(provider)
            await page.get_by_text('Model access saved.', exact=True).wait_for()
            assert config.ai_provider() == provider
            await page.locator('#btn-close-settings').click()
            await page.locator('#papers [data-paper="study"]').click()
            await page.get_by_role('button', name='Chat about paper', exact=True).click()
            await page.locator('#chat-question').fill('How many adults?')
            await page.locator('#chat-send').click()
            await page.get_by_text('Twenty adults (p. 1).', exact=True).wait_for()
            assert await page.locator('#chat-messages .who').last.text_content() == assistant
            await page.locator('#chat-question').fill('Was this a pilot?')
            await page.locator('#chat-send').click()
            await page.get_by_text('Usage limit reached', exact=True).wait_for()
            assert await page.locator('#chat-question').input_value() == 'Was this a pilot?'
            await page.locator('#chat-send').click()
            await page.locator('#chat-question:enabled').wait_for()
            assert len(requests[2]) == 3
            assert requests[2][-1]['content'] == 'Was this a pilot?'
            await page.locator('#chat-clear').click()
            assert await page.locator('#chat-messages').inner_text() == ''
            await page.locator('#chat-close').click()
            await page.locator('#btn-settings').click()
            await page.locator('#ai-enabled').uncheck()
            await page.get_by_text('AI analysis disabled.', exact=True).wait_for()
            await page.locator('#btn-close-settings').click()
            assert await page.get_by_role('button', name='Chat about paper', exact=True).is_disabled()
            assert not errors
            await browser.close()

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('answer,turns', [('x' * 30000, 3), ('Short answer', 20), ('x' * 32001, 1)],
                         ids=['total-characters', 'message-count', 'single-answer-length'])
def test_long_chat_keeps_draft_and_explains_how_to_start_over(answer, turns):
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            requests = []

            async def respond(route):
                requests.append(route.request.post_data_json['messages'])
                await route.fulfill(content_type='application/json', body=json.dumps({
                    'answer': answer, 'model': 'test', 'assistant': 'Claude'}))

            await page.route('**/api/papers/study/chat', respond)
            await page.goto(url)
            await page.locator('#papers [data-paper="study"]').click()
            await page.get_by_role('button', name='Chat about paper', exact=True).click()
            for _ in range(turns):
                await page.locator('#chat-question').fill('Question')
                await page.locator('#chat-send').click()
                await page.locator('#chat-question:enabled').wait_for()
            await page.locator('#chat-question').fill('Keep this question')
            await page.locator('#chat-send').click()
            await page.get_by_text('Start a new conversation; this one is too long.', exact=True).wait_for()
            assert len(requests) == turns
            assert await page.locator('#chat-question').input_value() == 'Keep this question'
            await page.locator('#chat-clear').click()
            await page.locator('#chat-question').fill('New question')
            await page.locator('#chat-send').click()
            await page.locator('#chat-question:enabled').wait_for()
            assert requests[-1] == [{'role': 'user', 'content': 'New question'}]
            await browser.close()

    with _server() as url:
        asyncio.run(check(url))
