"""Library answers render safely and can be kept as workspace research notes."""
import asyncio
from pathlib import Path

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config
from test_browser import _answer, _server
from test_research_notes_browser import ready


ANSWER = '''## What the papers show

The strongest result is **better recovery after an intervention**, with an important *task-dependent* caveat. See `recovery2026-c1`.

- Two papers report improved recovery.
- One paper finds that the effect varies with the task.

| Condition | Recovery |
| --- | --- |
| Baseline | 46% |
| Intervention | 71% |

### Open question

Does the result generalize? Compare $p = 0.71$ with the baseline.

[Read the source](https://example.com/paper)
'''
QUESTION = 'Synthesize all papers in this workspace, with citations.'


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_answer_markdown_save_reopen_and_export(engine):
    config.set_ai_provider('codex')
    workspace = config.create_workspace('Recovery studies')
    headers = {'X-Doxograph-Workspace': workspace['id']}

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors, external, requests = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: external.append(req.url)
                    if not req.url.startswith(('http://127.0.0.1:', 'blob:', 'data:')) else None)

            async def respond(route):
                requests.append(route.request.post_data_json['messages'])
                await route.fulfill(json={'answer': ANSWER, 'assistant': 'ChatGPT',
                                          'activity': ['Searched papers recovery', 'Read claims recovery2026']})

            await page.route('**/api/agent/chat', respond)
            with _server() as url:
                await page.goto(url + '/#ws=' + workspace['id'])
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill(QUESTION)
                await page.locator('#agent-send').click()
                content = page.locator('#agent-messages .note-prose')
                await content.locator('table').wait_for()
                assert await content.locator('h2').inner_text() == 'What the papers show'
                assert await content.locator('strong').inner_text() == 'better recovery after an intervention'
                assert await content.locator('li').count() == 2
                assert await content.locator('.katex').count() == 1
                link = content.get_by_role('link', name='Read the source')
                assert await link.get_attribute('rel') == 'noopener noreferrer'
                assert await content.locator('p').first.evaluate("el => getComputedStyle(el).whiteSpace") == 'normal'
                # Saving a generated answer is local and still works with AI off.
                config.set_ai_enabled(False)
                await page.evaluate('pollOnce()')
                await page.locator('#agent-question').fill('An unsent follow-up')
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                notes = httpx.get(url + '/api/research-notes', headers=headers).json()['notes']
                assert len(notes) == 1
                note = notes[0]
                assert note['title'] == QUESTION
                assert QUESTION in note['body'] and ANSWER in note['body']
                assert 'Saved from Ask the library · ChatGPT · ' in note['body']
                assert 'Read claims recovery2026' in note['body']
                assert httpx.get(url + '/api/research-notes').json()['notes'] == []
                assert await page.locator('#agent-question').input_value() == 'An unsent follow-up'
                assert len(requests) == 1
                # Reopening the chat does not offer to save a duplicate.
                await page.locator('#agent-close').click()
                await page.locator('#btn-agent').click()
                assert await page.get_by_role('button', name='Save as research note', exact=True).count() == 0
                config.set_ai_enabled(True)
                await page.evaluate('pollOnce()')
                await page.locator('#agent-messages').evaluate('el => el.scrollTop = 0')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/library-answer.png')
                # The composer stays inside smaller windows while only answers scroll.
                await page.set_viewport_size({'width': 640, 'height': 720})
                send_box = await page.locator('#agent-send').bounding_box()
                assert send_box['y'] + send_box['height'] <= 720
                assert await page.locator('#library-agent').evaluate('el => el.scrollWidth <= el.clientWidth')
                if engine == 'chromium':
                    await page.screenshot(path='.context/library-answer-small.png')
                await page.set_viewport_size({'width': 1440, 'height': 1100})
                await page.get_by_role('button', name='Open saved note', exact=True).click()
                await page.locator('#research-notes').wait_for(state='visible')
                await ready(page)
                assert await page.locator('#library-agent').is_hidden()
                assert await page.locator('#note-body').input_value() == note['body']
                await page.reload()
                await page.locator('#research-notes').wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-title').input_value() == QUESTION
                assert await page.locator('#note-preview table').count() == 1
                await page.locator('#note-title').fill('Recovery across the project')
                await page.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                async with page.expect_download() as download:
                    await page.get_by_role('button', name='Export Markdown', exact=True).click()
                exported = Path(await (await download.value).path()).read_text()
                assert exported.startswith('# Recovery across the project\n\n')
                assert ANSWER in exported
                await page.get_by_role('button', name='Close research notes', exact=True).click()
                await page.locator('#btn-agent').click()
                assert await page.locator('#agent-messages').inner_text() == ''
                assert not errors and not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_committed_save_with_lost_response_retries_once_and_deleted_note_can_be_saved_again(engine):
    config.set_ai_provider('claude_code')
    workspace = config.create_workspace('Synthesis retry studies')
    headers = {'X-Doxograph-Workspace': workspace['id']}

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors, operations, questions = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            # Saving also works on HTTP LAN deployments without randomUUID.
            await page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined})")

            async def answer(route):
                questions.append(route.request.post_data_json['messages'])
                await route.fulfill(json={'answer': ANSWER, 'assistant': 'Claude'})

            async def save(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                operations.append((route.request.headers['idempotency-key'],
                                   route.request.headers['x-doxograph-workspace']))
                if len(operations) == 1:
                    # Commit on the real test server, then lose only the response.
                    response = await route.fetch()
                    assert response.status == 201
                    await route.abort('failed')
                else:
                    await route.continue_()

            await page.route('**/api/agent/chat', answer)
            await page.route('**/api/research-notes', save)
            with _server() as url:
                await page.goto(url + '/#ws=' + workspace['id'])
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill(QUESTION)
                await page.locator('#agent-send').click()
                await page.locator('#agent-messages .note-prose h2').wait_for()
                config.set_ai_enabled(False)
                await page.evaluate('pollOnce()')
                await page.locator('#agent-question').fill('Keep this follow-up')
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('alert').filter(has_text='Could not save this answer').wait_for()
                [first] = httpx.get(url + '/api/research-notes', headers=headers).json()['notes']
                assert first['id'] == operations[0][0]
                assert ANSWER in first['body']
                assert await page.locator('#agent-question').input_value() == 'Keep this follow-up'
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                assert operations == [(first['id'], workspace['id'])] * 2
                assert httpx.get(url + '/api/research-notes', headers=headers).json()['notes'] == [first]
                await page.get_by_role('button', name='Open saved note', exact=True).click()
                await page.locator('#research-notes').wait_for(state='visible')
                await ready(page)
                await page.get_by_role('button', name='Delete note', exact=True).click()
                await _answer(page, 'Delete note')
                await ready(page)
                await page.get_by_role('button', name='Close research notes', exact=True).click()
                await page.locator('#btn-agent').click()
                await page.get_by_role('button', name='Open saved note', exact=True).click()
                await page.get_by_role('alert').filter(has_text='The saved note was deleted').wait_for()
                assert await page.locator('#library-agent').is_visible()
                assert 'What the papers show' in await page.locator('#agent-messages').inner_text()
                assert await page.locator('#agent-question').input_value() == 'Keep this follow-up'
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                [replacement] = httpx.get(url + '/api/research-notes', headers=headers).json()['notes']
                assert replacement['id'] != first['id']
                assert replacement['body'] == first['body']
                assert operations[-1] == (replacement['id'], workspace['id'])
                assert httpx.get(url + '/api/research-notes').json()['notes'] == []
                assert len(questions) == 1 and not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_library_answer_safe_rendering_and_failed_save_retry(engine):
    config.set_ai_provider('claude_code')
    unsafe = '''## Findings

<script>window.chatUnsafe = true</script>

[Unsafe](javascript:alert(1))

![Remote](https://example.invalid/tracking.png)

```html
<button onclick="alert(1)">Example only</button>
```
'''

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: external.append(req.url)
                    if not req.url.startswith(('http://127.0.0.1:', 'blob:', 'data:')) else None)
            attempts = 0
            held, release = asyncio.Event(), asyncio.Event()

            async def save(route):
                nonlocal attempts
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                attempts += 1
                if attempts == 1:
                    await route.fulfill(status=503, json={'detail': 'Disk unavailable. Try again.'})
                else:
                    held.set()
                    await release.wait()
                    await route.continue_()

            await page.route('**/api/agent/chat', lambda route: route.fulfill(json={
                'answer': unsafe, 'assistant': 'Claude'}))
            await page.route('**/api/research-notes', save)
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-question').fill('What do the papers show?')
                await page.locator('#agent-send').click()
                content = page.locator('#agent-messages .note-prose')
                await content.locator('h2').wait_for()
                assert await page.evaluate('window.chatUnsafe') is None
                assert await content.locator('script, img, button, [onclick], [href^="javascript:"]').count() == 0
                assert '<button onclick=' in await content.locator('pre code').inner_text()
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await page.get_by_role('alert').filter(has_text='Disk unavailable').wait_for()
                assert await content.inner_text()
                assert httpx.get(url + '/api/research-notes').json()['notes'] == []
                await page.get_by_role('button', name='Save as research note', exact=True).click()
                await asyncio.wait_for(held.wait(), timeout=10)
                try:
                    assert await page.get_by_role('button', name='Saving…', exact=True).is_disabled()
                    assert await page.locator('#agent-clear').is_disabled()
                    await page.locator('#agent-close').click()
                finally:
                    release.set()
                await page.locator('#btn-agent').click()
                await page.get_by_role('button', name='Open saved note', exact=True).wait_for()
                assert attempts == 2
                assert len(httpx.get(url + '/api/research-notes').json()['notes']) == 1
                await page.locator('#agent-clear').click()
                assert await page.locator('#agent-messages').inner_text() == ''
                await page.locator('#agent-close').click()
                await page.get_by_role('button', name='Research notes', exact=True).click()
                await ready(page)
                assert unsafe in await page.locator('#note-body').input_value()
                assert await page.locator('#note-preview script, #note-preview img').count() == 0
                assert not errors and not external

    run_browser(scenario())
