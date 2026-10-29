"""Exercise model access selection and paper chat in the real page."""

import asyncio
import json

import pytest
from playwright.async_api import async_playwright

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize('provider,assistant', [('claude_code', 'Claude'), ('codex', 'ChatGPT')])
def test_subscription_selection_and_paper_chat_keep_followups_and_failed_drafts(provider, assistant):
    config.save_analysis_settings('ollama', 'my-local-model', 'http://localhost:11434')
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
            await page.route('**/api/settings/codex/models', lambda route: route.fulfill(
                content_type='application/json', body='{"models":[]}'))
            await page.goto(url)
            await page.locator('#btn-settings').click()
            await page.locator('#ai-access').select_option(provider)
            await page.get_by_text('Model access saved.', exact=True).wait_for()
            assert config.ai_provider() == provider
            assert await page.locator('#ai-provider-form').is_hidden()
            await page.locator('#btn-close-settings').click()
            await page.locator('#papers [data-paper="study"]').click()
            await page.get_by_role('button', name='Ask about this paper', exact=True).click()
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
            assert await page.locator('[data-paper-chat]').is_disabled()
            await page.locator('#btn-settings').click()
            await page.locator('#ai-access').select_option('api')
            await page.get_by_text('Model access saved.', exact=True).wait_for()
            assert await page.locator('#ai-provider-form').is_visible()
            assert await page.locator('#ai-provider').input_value() == 'ollama'
            assert await page.locator('#ai-model').input_value() == 'my-local-model'
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
            await page.get_by_role('button', name='Ask about this paper', exact=True).click()
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


@pytest.mark.browser
@pytest.mark.parametrize('provider,assistant', [('claude_code', 'Claude'), ('codex', 'ChatGPT')])
def test_library_agent_dialog_sends_the_conversation_and_shows_lookups(provider, assistant):
    config.set_ai_provider(provider)

    async def check(url):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            requests = []

            async def respond(route):
                requests.append(route.request.post_data_json['messages'])
                await route.fulfill(content_type='application/json', body=json.dumps({
                    'answer': 'Two papers agree.', 'activity': ['Searched claims steering'], 'assistant': assistant}))

            await page.route('**/api/agent/chat', respond)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator('#agent-question').fill('What agrees on steering?')
            await page.locator('#agent-send').click()
            await page.get_by_text('Two papers agree.', exact=True).wait_for()
            assert await page.locator('#agent-messages .activity li').text_content() == 'Searched claims steering'
            assert await page.locator('#agent-messages .who').all_text_contents() == ['You', assistant]
            assert await page.locator('#agent-codex-guidance').is_visible() == (provider == 'codex')
            await page.locator('#agent-question').fill('And disagrees?')
            await page.locator('#agent-send').click()
            await page.locator('#agent-question:enabled').wait_for()
            assert [m['content'] for m in requests[1]] == ['What agrees on steering?', 'Two papers agree.', 'And disagrees?']
            await page.locator('#agent-clear').click()
            assert await page.locator('#agent-messages').inner_text() == ''
            await browser.close()
            assert errors == []

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
def test_library_agent_cannot_send_outside_subscription_mode():
    config.set_ai_provider('api')

    async def check(url):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            requests = []
            await page.route('**/api/agent/chat', lambda route: requests.append(route))
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator('#agent-question').fill('Anything?')
            assert await page.locator('#agent-send').is_disabled()
            assert 'Claude subscription' in await page.locator('#agent-status').text_content()
            assert requests == []
            await browser.close()

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_agent_updates_provider_without_losing_drafts_or_answer_labels(engine):
    config.set_ai_provider('claude_code')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()

            async def respond(route):
                assistant = 'ChatGPT' if config.ai_provider() == 'codex' else 'Claude'
                await route.fulfill(json={'answer': f'{assistant} answer.', 'assistant': assistant})

            await page.route('**/api/agent/chat', respond)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator('#agent-question').fill('First question')
            await page.locator('#agent-send').click()
            await page.get_by_text('Claude answer.', exact=True).wait_for()
            await page.locator('#agent-question').fill('Follow up')
            config.set_ai_provider('api')
            await page.evaluate('pollOnce()')
            assert await page.locator('#agent-send').is_disabled()
            config.set_ai_provider('codex')
            await page.evaluate('pollOnce()')
            assert await page.locator('#agent-send').is_enabled()
            assert await page.locator('#agent-question').input_value() == 'Follow up'
            assert await page.locator('#agent-codex-guidance').is_visible()
            await page.locator('#agent-send').click()
            await page.get_by_text('ChatGPT answer.', exact=True).wait_for()
            assert await page.locator('#agent-messages .who').all_text_contents() == ['You', 'Claude', 'You', 'ChatGPT']

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('provider', ['api', 'codex', 'claude_code'])
def test_library_agent_explains_disabled_ai_and_opens_settings(engine, provider):
    config.set_ai_enabled(False)
    config.set_ai_provider(provider)

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            requests = []

            async def respond(route):
                requests.append(route.request.post_data_json['messages'])
                await route.fulfill(content_type='application/json', body=json.dumps({
                    'answer': 'Two papers agree.', 'assistant': 'Claude'}))

            await page.route('**/api/agent/chat', respond)
            await page.goto(url)
            await page.locator('#ai-enabled:enabled').wait_for(state='attached')
            assert await page.locator('#btn-agent').is_enabled()
            await page.locator('#btn-agent').click()
            assert await page.locator('#library-agent').is_visible()
            assert 'Enable AI analysis in Settings.' in await page.locator('#agent-status').inner_text()
            if provider == 'api':
                assert 'Choose Claude subscription' in await page.locator('#agent-status').inner_text()
            await page.locator('#agent-question').fill('Keep my question')
            assert await page.locator('#agent-send').is_disabled()
            assert requests == []

            await page.locator('#agent-settings').click()
            assert await page.locator('#library-agent').is_hidden()
            assert await page.locator('#settings-menu').is_visible()
            await page.locator('#ai-enabled').check()
            await page.get_by_text('AI analysis enabled.', exact=True).wait_for()
            if provider == 'api':
                await page.locator('#ai-access').select_option('claude_code')
                await page.get_by_text('Model access saved.', exact=True).wait_for()
            await page.locator('#btn-close-settings').click()
            await page.locator('#btn-agent').click()
            assert await page.locator('#agent-question').input_value() == 'Keep my question'
            assert await page.locator('#agent-send').is_enabled()
            assert await page.locator('#agent-settings').is_hidden()
            await page.locator('#agent-send').click()
            await page.get_by_text('Two papers agree.', exact=True).wait_for()
            assert requests == [[{'role': 'user', 'content': 'Keep my question'}]]

            # Settings changed in another window must also update the open chat,
            # without replacing its draft or starting another model request.
            await page.locator('#agent-question').fill('Another question')
            config.set_ai_enabled(False)
            await page.evaluate('pollOnce()')
            assert await page.locator('#agent-send').is_disabled()
            assert await page.locator('#agent-settings').is_visible()
            assert 'Enable AI analysis' in await page.locator('#agent-status').inner_text()
            assert await page.locator('#agent-question').input_value() == 'Another question'
            assert len(requests) == 1
            assert errors == []

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('source', ['url', 'storage'])
def test_library_agent_waits_for_workspace_selection_before_opening(engine, source):
    config.set_ai_provider('claude_code')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            held = asyncio.Event()
            release = asyncio.Event()
            requests = []

            async def registry(route):
                held.set()
                await release.wait()
                await route.continue_()

            async def respond(route):
                requests.append((route.request.headers['x-doxograph-workspace'],
                                 route.request.post_data_json['messages']))
                await route.fulfill(json={'answer': 'The default library.', 'assistant': 'Claude'})

            if source == 'storage':
                await page.add_init_script("localStorage.setItem('doxograph-workspace', 'missing')")
            await page.route('**/api/workspaces', registry)
            await page.route('**/api/agent/chat', respond)
            await page.goto(url + ('/#ws=missing' if source == 'url' else ''),
                            wait_until='domcontentloaded')
            await asyncio.wait_for(held.wait(), timeout=10)
            try:
                assert await page.locator('#btn-agent').is_disabled()
                await page.locator('#btn-agent').evaluate('button => button.click()')
                assert await page.locator('#library-agent').is_hidden()
            finally:
                release.set()

            await page.locator('#ai-enabled:enabled').wait_for(state='attached')
            await page.locator('#btn-agent').click()
            await page.locator('#agent-question').fill('Which library?')
            await page.locator('#agent-close').click()
            await page.locator('#btn-agent').click()
            assert await page.locator('#agent-question').input_value() == 'Which library?'
            await page.locator('#agent-send').click()
            await page.get_by_text('The default library.', exact=True).wait_for()
            assert requests == [('default', [{'role': 'user', 'content': 'Which library?'}])]

    with _server() as url:
        run_browser(check(url))
