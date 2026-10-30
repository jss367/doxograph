"""Exercise model access selection and paper chat in the real page."""

import asyncio
import json

import httpx
import pytest
from playwright.async_api import async_playwright

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _answer, _server


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

            await page.route('**/api/ask', respond)
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
            await page.locator('#agent-question').fill('How many adults?')
            await page.locator('#agent-question').press('Shift+Enter')
            assert await page.locator('#agent-question').input_value() == 'How many adults?\n'
            await page.locator('#agent-question').evaluate('''field => {
                const enter = (init, keyCode) => {
                    const event = new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true, ...init});
                    if (keyCode) Object.defineProperty(event, 'keyCode', {value: keyCode});
                    field.dispatchEvent(event);
                };
                enter({isComposing: true});
                enter({}, 229);
                enter({repeat: true});
            }''')
            await page.wait_for_timeout(200)
            assert requests == []
            await page.locator('#agent-question').press('Enter')
            await page.get_by_text('Twenty adults (p. 1).', exact=True).wait_for()
            assert await page.locator('#agent-messages .who').last.text_content() == assistant
            await page.locator('#agent-question').fill('Was this a pilot?')
            await page.locator('#agent-send').click()
            await page.get_by_text('Usage limit reached', exact=True).wait_for()
            assert await page.locator('#agent-question').input_value() == 'Was this a pilot?'
            await page.locator('#agent-send').click()
            await page.locator('#agent-question:enabled').wait_for()
            assert len(requests[2]) == 3
            assert requests[2][-1]['content'] == 'Was this a pilot?'
            await page.locator('#agent-clear').click()
            assert await page.locator('#agent-messages').inner_text() == ''
            await page.locator('#agent-close').click()
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

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await page.locator('#papers [data-paper="study"]').click()
            await page.get_by_role('button', name='Ask about this paper', exact=True).click()
            for _ in range(turns):
                await page.locator('#agent-question').fill('Question')
                await page.locator('#agent-send').click()
                await page.locator('#agent-question:enabled').wait_for()
            await page.locator('#agent-question').fill('Keep this question')
            await page.locator('#agent-send').click()
            await page.get_by_text('Start a new conversation; this one is too long.', exact=True).wait_for()
            assert len(requests) == turns
            assert await page.locator('#agent-question').input_value() == 'Keep this question'
            await page.locator('#agent-clear').click()
            await page.locator('#agent-question').fill('New question')
            await page.locator('#agent-send').click()
            await page.locator('#agent-question:enabled').wait_for()
            assert requests[-1] == [{'role': 'user', 'content': 'New question'}]
            await browser.close()

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_paper_conversations_run_side_by_side_on_their_own_agents_and_survive_reload(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            held, release = asyncio.Event(), asyncio.Event()

            async def respond(route):
                body = route.request.post_data_json
                requests.append((body['agent'], [m['content'] for m in body['messages']]))
                if body['messages'][0]['content'] == 'First question' and len(body['messages']) == 1:
                    held.set()
                    await release.wait()
                assistant = {'codex': 'ChatGPT', 'api': 'Anthropic'}.get(body['agent'], 'Claude')
                await route.fulfill(json={'answer': f'{assistant} answer {len(requests)}.', 'model': 'test',
                                          'assistant': assistant})

            async def open_chat():
                if _saved_chats():
                    await page.locator('#btn-agent').click()
                    return
                await page.locator('#papers [data-paper="study"]').click()
                await page.get_by_role('button', name='Ask about this paper', exact=True).click()

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await open_chat()
            assert await page.locator('#agent-agent').input_value() == 'claude_code'
            await page.locator('#agent-question').fill('First question')
            await page.locator('#agent-send').click()
            await asyncio.wait_for(held.wait(), timeout=10)
            # Start another conversation, on another agent, while the first is still answering.
            await page.locator('#agent-clear').click()
            await page.locator('#agent-agent').select_option('codex')
            await page.locator('#agent-question').fill('Second question')
            await page.locator('#agent-send').click()
            await page.get_by_text('ChatGPT answer 2.', exact=True).wait_for()
            release.set()
            await page.locator('#agent-thread option', has_text='First question').filter(
                has_not_text='answering').wait_for(state='attached')
            assert requests == [('claude_code', ['First question']), ('codex', ['Second question'])]

            await page.reload()
            await open_chat()
            # The most recently answered conversation reopens; the held first answer may have landed last.
            await page.locator('#agent-thread option').nth(1).wait_for(state='attached')
            assert sorted(await page.locator('#agent-thread option').all_text_contents()) == ['First question', 'Second question']
            await page.locator('#agent-thread').select_option(label='Second question')
            await page.get_by_text('ChatGPT answer 2.', exact=True).wait_for()
            assert await page.locator('#agent-agent').input_value() == 'codex'
            await page.locator('#agent-thread').select_option(label='First question')
            assert await page.locator('#agent-agent').input_value() == 'claude_code'
            await page.get_by_text('Claude answer 2.', exact=True).wait_for()
            await page.locator('#agent-question').fill('Follow up')
            await page.locator('#agent-send').click()
            await page.get_by_text('Claude answer 3.', exact=True).wait_for()
            assert requests[-1] == ('claude_code', ['First question', 'Claude answer 2.', 'Follow up'])

            await page.locator('#agent-thread').select_option(label='Second question')
            await page.locator('#agent-delete').click()
            await _answer(page, 'Delete conversation')
            await page.locator('#agent-thread option', has_text='Second question').wait_for(state='detached')
            assert await page.locator('#agent-thread option').all_text_contents() == ['First question']
            await page.get_by_text('Claude answer 3.', exact=True).wait_for()
            [saved] = store.config.data_dir().joinpath('chats').glob('*.json')
            saved = json.loads(saved.read_text())
            assert saved['scope']['papers'] == ['study'] and saved['agent'] == 'claude_code'
            assert [m['assistant'] for m in saved['messages'][1::2]] == ['Claude', 'Claude']
            assert not errors

    with _server() as url:
        run_browser(check(url))


def _saved_chats():
    return sorted((json.loads(path.read_text()) for path in config.data_dir().joinpath('chats').glob('*.json')),
                  key=lambda chat: [m['content'] for m in chat['messages']])


async def _saved_contents(count):
    for _ in range(200):
        if len(chats := _saved_chats()) >= count:
            return [[m['content'] for m in chat['messages']] for chat in chats]
        await asyncio.sleep(0.05)
    raise AssertionError(f'expected {count} saved conversations, found {len(_saved_chats())}')


def _their_follow_up(saved):
    """Another window's save of a follow-up to the conversation it last loaded."""
    return {'paper': saved['paper'], 'scope': saved.get('scope'), 'agent': saved['agent'], 'revision': saved['revision'], 'operation': 'f' * 32,
            'messages': saved['messages'] + [{'role': 'user', 'content': 'Theirs'},
                                             {'role': 'assistant', 'content': 'Their answer.'}]}


async def _paper_chat_with_answers(page):
    answers = []

    async def respond(route):
        answers.append(route.request.post_data_json['messages'][-1]['content'])
        await route.fulfill(json={'answer': f'Answer {len(answers)}.', 'model': 'test', 'assistant': 'Claude'})

    await page.route('**/api/ask', respond)

    async def ask(question):
        expected = f'Answer {len(answers) + 1}.'
        await page.locator('#agent-question').fill(question)
        await page.locator('#agent-send').click()
        await page.get_by_text(expected, exact=True).wait_for()

    async def open_chat():
        if _saved_chats():
            await page.locator('#btn-agent').click()
            return
        await page.locator('#papers [data-paper="study"]').click()
        await page.get_by_role('button', name='Ask about this paper', exact=True).click()

    return ask, open_chat


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_unsaved_answer_survives_another_window_saving_the_same_conversation(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            await page.goto(url)
            await open_chat()
            await ask('First')
            [first] = _saved_chats()
            failing = True

            async def disk_full(route):
                if failing and route.request.method == 'PUT':
                    await route.fulfill(status=503, json={'detail': 'Disk unavailable.'})
                else:
                    await route.continue_()

            await page.route('**/api/chats/*', disk_full)
            await ask('Mine')
            await page.get_by_text('This conversation is not saved yet: Disk unavailable.', exact=True).wait_for()
            # Meanwhile another window saves its own follow-up to the same conversation.
            assert httpx.put(f"{url}/api/chats/{first['id']}", json=_their_follow_up(first)).status_code == 200
            failing = False
            await page.locator('#agent-close').click()
            await open_chat()
            await page.get_by_text('this version was kept as a separate conversation', exact=False).wait_for()
            await page.locator('#agent-thread option').nth(1).wait_for(state='attached')
            assert await _saved_contents(2) == [
                ['First', 'Answer 1.', 'Mine', 'Answer 2.'], ['First', 'Answer 1.', 'Theirs', 'Their answer.']]
            await page.locator('#agent-thread').select_option(index=1)
            await page.get_by_text('Their answer.', exact=True).wait_for()
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
def test_answer_saved_after_another_window_saved_first_is_kept_separately():
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session('chromium') as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            await page.goto(url)
            await open_chat()
            await ask('First')
            [first] = _saved_chats()
            assert httpx.put(f"{url}/api/chats/{first['id']}", json=_their_follow_up(first)).status_code == 200
            await ask('Mine')
            await page.get_by_text('this version was kept as a separate conversation', exact=False).wait_for()
            assert await _saved_contents(2) == [
                ['First', 'Answer 1.', 'Mine', 'Answer 2.'], ['First', 'Answer 1.', 'Theirs', 'Their answer.']]
            assert await page.locator('#agent-thread option').count() == 2
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_saves_finished_while_the_conversation_list_loads_are_kept(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            taken, release = asyncio.Event(), asyncio.Event()

            async def held_list(route):
                if route.request.method != 'GET':
                    await route.continue_()
                    return
                # Take the snapshot now and deliver it only after the page's save finished.
                response = await route.fetch()
                taken.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route('**/api/chats?all_scopes=true', held_list)
            await page.goto(url)
            for question, contents in (('First', ['First', 'Answer 1.']),
                                       ('Second', ['First', 'Answer 1.', 'Second', 'Answer 2.'])):
                await open_chat()
                await asyncio.wait_for(taken.wait(), timeout=10)
                if question == 'Second':
                    await page.get_by_text('Answer 1.', exact=True).wait_for()
                await ask(question)
                assert await _saved_contents(1) == [contents]
                release.set()
                await page.wait_for_timeout(200)
                assert await page.locator('#agent-thread option').all_text_contents() == ['First']
                assert await page.locator('#agent-messages .agent-content > p:not(.hint)').all_text_contents() == contents
                await page.locator('#agent-close').click()
                taken.clear()
                release.clear()
            await page.unroute('**/api/chats?all_scopes=true')
            await open_chat()
            await ask('Third')
            assert await _saved_contents(1) == [['First', 'Answer 1.', 'Second', 'Answer 2.', 'Third', 'Answer 3.']]
            assert not await page.get_by_text('kept as a separate conversation', exact=False).count()
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_an_older_conversation_list_arriving_last_is_ignored(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            taken, release = asyncio.Event(), asyncio.Event()
            lists = 0

            async def first_list_held(route):
                nonlocal lists
                lists += 1
                if lists > 1:
                    await route.continue_()
                    return
                response = await route.fetch()
                taken.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route('**/api/chats?all_scopes=true', first_list_held)
            await page.goto(url)
            await open_chat()
            await asyncio.wait_for(taken.wait(), timeout=10)
            # Another window saves a conversation; reopening lists it while the first, empty list is held.
            saved = {'paper': 'study', 'agent': 'claude_code', 'revision': 0, 'operation': 'e' * 32,
                     'messages': [{'role': 'user', 'content': 'Theirs'}, {'role': 'assistant', 'content': 'Their answer.'}]}
            assert httpx.put(f"{url}/api/chats/{'d' * 32}", json=saved).status_code == 200
            await page.locator('#agent-close').click()
            await open_chat()
            await page.get_by_text('Their answer.', exact=True).wait_for()
            release.set()
            await page.wait_for_timeout(200)
            assert await page.locator('#agent-thread option').all_text_contents() == ['Theirs']
            assert await page.locator('#agent-messages .agent-content > p:not(.hint)').all_text_contents() == ['Theirs', 'Their answer.']
            # A follow-up continues that conversation rather than forking it.
            await ask('Mine')
            assert await _saved_contents(1) == [['Theirs', 'Their answer.', 'Mine', 'Answer 1.']]
            assert await page.locator('#agent-thread option').all_text_contents() == ['Theirs']
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_draft_survives_another_window_deleting_its_conversation(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            await page.goto(url)
            await open_chat()
            await ask('First')
            [first] = _saved_chats()
            await page.locator('#agent-close').click()
            taken, release = asyncio.Event(), asyncio.Event()

            async def held_list(route):
                taken.set()
                await release.wait()
                await route.continue_()

            await page.route('**/api/chats?all_scopes=true', held_list)
            await open_chat()
            await asyncio.wait_for(taken.wait(), timeout=10)
            await page.locator('#agent-question').fill('An unsent follow-up')
            # Another window deletes the conversation before this list is read.
            assert httpx.delete(f"{url}/api/chats/{first['id']}?revision={first['revision']}").status_code == 200
            release.set()
            await page.get_by_text('kept as a separate conversation', exact=False).wait_for()
            assert await page.locator('#agent-question').input_value() == 'An unsent follow-up'
            assert await page.locator('#agent-messages .agent-content > p:not(.hint)').all_text_contents() == ['First', 'Answer 1.']
            assert await _saved_contents(1) == [['First', 'Answer 1.']]
            await page.unroute('**/api/chats?all_scopes=true')
            await page.locator('#agent-send').click()
            await page.get_by_text('Answer 2.', exact=True).wait_for()
            assert await _saved_contents(1) == [['First', 'Answer 1.', 'An unsent follow-up', 'Answer 2.']]
            [kept] = _saved_chats()
            assert kept['id'] != first['id']
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversation_deleted_while_an_older_list_loads_stays_deleted(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)
            await page.goto(url)
            await open_chat()
            await ask('First')
            await page.locator('#agent-close').click()
            taken, release = asyncio.Event(), asyncio.Event()

            async def held_list(route):
                # The snapshot still lists the conversation; it arrives after the delete.
                response = await route.fetch()
                taken.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route('**/api/chats?all_scopes=true', held_list)
            await open_chat()
            await asyncio.wait_for(taken.wait(), timeout=10)
            await page.locator('#agent-delete').click()
            await _answer(page, 'Delete conversation')
            await page.locator('#agent-thread option', has_text='First').wait_for(state='detached')
            release.set()
            await page.wait_for_timeout(200)
            assert await page.locator('#agent-thread option').all_text_contents() == ['New conversation']
            assert await page.locator('#agent-messages .agent-content > p:not(.hint)').count() == 0
            assert _saved_chats() == []
            assert not errors

    with _server() as url:
        run_browser(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_conversation_keeps_saving_after_a_save_response_is_lost(engine):
    config.set_ai_provider('claude_code')
    store.save_paper(store.new_paper('study', title='Memory Study'))
    store.pdf_path('study').write_bytes(b'%PDF-1.4 fake')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors, saves = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            ask, open_chat = await _paper_chat_with_answers(page)

            async def lose_second(route):
                saves.append(route.request.post_data_json)
                if len(saves) == 2:
                    # Commit on the test server, then lose only the response.
                    assert (await route.fetch()).status == 200
                    await route.abort('failed')
                else:
                    await route.continue_()

            await page.route('**/api/chats/*', lose_second)
            await page.goto(url)
            await open_chat()
            await ask('First')
            await ask('Second')
            await page.get_by_text('This conversation is not saved yet', exact=False).wait_for()
            await ask('Third')
            await page.locator('#agent-status').filter(has_text='not saved').wait_for(state='detached')
            assert saves[2]['revision'] == 1 and saves[2]['unconfirmed'] == [saves[1]['operation']]
            [saved] = _saved_chats()
            assert [m['content'] for m in saved['messages']][::2] == ['First', 'Second', 'Third']
            assert saved['revision'] == 3
            await page.reload()
            await open_chat()
            await page.get_by_text('Answer 3.', exact=True).wait_for()
            assert await page.locator('#agent-thread option').count() == 1
            assert not errors

    with _server() as url:
        run_browser(check(url))


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

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator('#agent-question').fill('What agrees on steering?')
            await page.locator('#agent-send').click()
            await page.get_by_text('Two papers agree.', exact=True).wait_for()
            assert await page.locator('#agent-messages .activity li').text_content() == 'Searched claims steering'
            assert await page.locator('#agent-messages .who').all_text_contents() == ['You', assistant]
            assert await page.locator('#agent-codex-guidance').is_visible() == (provider == 'codex')
            await page.locator('#agent-question').fill('And disagrees?')
            await page.locator('#agent-question').press('Enter')
            await page.locator('#agent-question:enabled').wait_for()
            assert [m['content'] for m in requests[1]] == ['What agrees on steering?', 'Two papers agree.', 'And disagrees?']
            await page.locator('#agent-clear').click()
            assert await page.locator('#agent-messages').inner_text() == ''
            await browser.close()
            assert errors == []

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
def test_library_agent_requires_explicit_provider_choice_when_settings_choose_an_api():
    config.set_ai_provider('api')

    async def check(url):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            requests = []

            async def respond(route):
                requests.append(route.request.post_data_json['agent'])
                await route.fulfill(json={'answer': 'From the library.', 'assistant': 'Claude'})

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            assert await page.locator('#agent-agent option').evaluate_all('options => options.map(o => o.value)') \
                == ['api', 'claude_code', 'codex']
            assert await page.locator('#agent-agent').input_value() == 'api'
            assert await page.locator('#agent-send').is_disabled()
            await page.locator('#agent-agent').select_option('claude_code')
            await page.locator('#agent-question').fill('Anything?')
            await page.locator('#agent-send').click()
            await page.get_by_text('From the library.', exact=True).wait_for()
            assert requests == ['claude_code']
            await browser.close()

    with _server() as url:
        asyncio.run(check(url))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_agent_switches_agent_without_losing_drafts_or_answer_labels(engine):
    config.set_ai_provider('claude_code')

    async def check(url):
        async with browser_session(engine) as browser:
            page = await browser.new_page()

            async def respond(route):
                assistant = 'ChatGPT' if route.request.post_data_json['agent'] == 'codex' else 'Claude'
                await route.fulfill(json={'answer': f'{assistant} answer.', 'assistant': assistant})

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await page.locator('#btn-agent:enabled').click()
            await page.locator('#agent-question').fill('First question')
            await page.locator('#agent-send').click()
            await page.get_by_text('Claude answer.', exact=True).wait_for()
            await page.locator('#agent-question').fill('Follow up')
            # Settings changes do not move a conversation off the agent it uses.
            config.set_ai_provider('api')
            await page.evaluate('pollOnce()')
            assert await page.locator('#agent-agent').input_value() == 'claude_code'
            assert await page.locator('#agent-send').is_enabled()
            await page.locator('#agent-agent').select_option('codex')
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

            await page.route('**/api/ask', respond)
            await page.goto(url)
            await page.locator('#ai-enabled:enabled').wait_for(state='attached')
            assert await page.locator('#btn-agent').is_enabled()
            await page.locator('#btn-agent').click()
            assert await page.locator('#library-agent').is_visible()
            assert 'Enable AI analysis in Settings.' in await page.locator('#agent-status').inner_text()
            await page.locator('#agent-question').fill('Keep my question')
            assert await page.locator('#agent-send').is_disabled()
            assert requests == []

            await page.locator('#agent-settings').click()
            assert await page.locator('#library-agent').is_hidden()
            assert await page.locator('#settings-menu').is_visible()
            await page.locator('#ai-enabled').check()
            await page.get_by_text('AI analysis enabled.', exact=True).wait_for()
            await page.locator('#btn-close-settings').click()
            await page.locator('#btn-agent').click()
            assert await page.locator('#agent-question').input_value() == 'Keep my question'
            if provider == 'api':
                assert await page.locator('#agent-send').is_disabled()
                await page.locator('#agent-agent').select_option('claude_code')
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
            await page.route('**/api/ask', respond)
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
