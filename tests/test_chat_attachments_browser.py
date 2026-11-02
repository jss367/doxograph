"""Attachment composer workflows work in browsers and the app's WebKit engine."""
import base64
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _server
from test_chat_attachments import image_attachment


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('entry', ['paper', 'library'])
def test_chat_files_retry_reopen_followup_and_download(engine, entry):
    prefix = 'agent'
    config.set_ai_provider('codex')
    config.set_ai_enabled(True)
    store.save_paper(store.new_paper('study', title='A study of recovery'))
    store.pdf_path('study').write_bytes(minimal_pdf('A finding.'))
    image = image_attachment()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1280, 'height': 1050})
            errors, requests, imports = [], [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: imports.append(req.url) if '/api/upload' in req.url else None)

            async def answer(route):
                requests.append(route.request.post_data_json)
                if len(requests) == 1:
                    await route.fulfill(status=503, json={'detail': 'Service unavailable. Retry.'})
                else:
                    await route.fulfill(json={'answer': 'I read the chart and the data.', 'assistant': 'ChatGPT'})

            endpoint = '**/api/ask'
            await page.route(endpoint, answer)
            with _server() as url:
                await page.goto(url + ('/#paper=study' if entry == 'paper' else ''))

                async def open_chat():
                    if entry == 'library':
                        await page.locator('#btn-agent:enabled').click()
                    else:
                        await page.locator('[data-paper-chat="study"]:visible').click()
                    await expect(page.locator(f'#{prefix}-attach')).to_be_enabled()
                    if entry == 'paper' and len(requests) > 1:
                        await page.locator('#agent-thread').select_option(index=1)

                await open_chat()
                async with page.expect_file_chooser() as chooser:
                    await page.locator(f'#{prefix}-attach').click()
                await (await chooser.value).set_files([
                    {'name': 'chart.png', 'mimeType': 'image/png', 'buffer': base64.b64decode(image['data'])},
                    {'name': 'results.csv', 'mimeType': 'text/csv', 'buffer': b'condition,accuracy\nbaseline,0.7'},
                ])
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(2)
                assert await page.locator(f'#{prefix}-attachments img').evaluate('image => image.complete && image.naturalWidth === 16')
                # Attachment-only messages submit without the required-text validation blocking them.
                await page.locator(f'#{prefix}-send').click()
                await expect(page.locator(f'#{prefix}-status')).to_contain_text('Service unavailable')
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(2)
                await page.locator(f'#{prefix}-send').click()
                await expect(page.locator(f'#{prefix}-messages .chat-attachment')).to_have_count(2)
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(0)
                await expect(page.locator(f'#{prefix}-delete')).to_be_visible()
                assert requests[1]['messages'][0]['attachments'][0] == image
                saved_url = url + '/api/chats' + ('?paper=study' if entry == 'paper' else '')
                saved = httpx.get(saved_url).json()['chats'][0]
                assert len(saved['messages'][0]['attachments']) == 2
                async with page.expect_download() as download:
                    await page.locator(f'#{prefix}-messages a[download="results.csv"]').click()
                assert (await download.value).suggested_filename == 'results.csv'
                assert Path(await (await download.value).path()).read_bytes() == b'condition,accuracy\nbaseline,0.7'

                await page.reload()
                await open_chat()
                await expect(page.locator(f'#{prefix}-messages .chat-attachment')).to_have_count(2)
                await page.locator(f'#{prefix}-question').fill('Explain the chart.')
                await page.locator(f'#{prefix}-send').click()
                await expect(page.locator(f'#{prefix}-question')).to_be_empty()
                assert requests[-1]['messages'][0]['attachments'][0] == image
                assert requests[-1]['messages'][-1]['content'] == 'Explain the chart.'
                # Drop and paste land in this conversation, without importing a library paper.
                await page.locator(f'#{prefix}-question').evaluate('''el => {
                    const transfer = new DataTransfer();
                    transfer.items.add(new File(['Dropped text'], 'dropped.md', {type:'text/markdown'}));
                    el.dispatchEvent(new DragEvent('drop', {bubbles:true, cancelable:true, dataTransfer:transfer}));
                }''')
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(1)
                await page.locator(f'#{prefix}-question').evaluate('''(el, data) => {
                    const transfer = new DataTransfer();
                    const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
                    transfer.items.add(new File([bytes], 'pasted.png', {type:'image/png'}));
                    el.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:transfer}));
                }''', image['data'])
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(2)
                await page.locator(f'#{prefix}-attachments [data-remove-attachment="0"]').click()
                await expect(page.locator(f'#{prefix}-attachments .chat-attachment')).to_have_count(1)
                await expect(page.locator(f'#{prefix}-attachments')).to_contain_text('pasted.png')
                await page.locator(f'#{prefix}-question').fill('What does this figure show?')
                if engine == 'chromium' and prefix == 'agent':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/conversation-attachments.png')
                assert not imports and not errors
                assert await page.locator(f'#{prefix}-form').evaluate('el => el.scrollWidth <= el.clientWidth')

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_invalid_files_show_readable_errors_and_drafts_stay_with_their_chat(engine):
    config.set_ai_provider('codex')
    config.set_ai_enabled(True)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1000, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                # Seed a second conversation so the draft can be switched away from and restored.
                for chat_id, question in [('a' * 32, 'First conversation'), ('b' * 32, 'Second conversation')]:
                    response = httpx.put(url + '/api/chats/' + chat_id, json={
                        'agent': 'codex', 'revision': 0, 'operation': 'c' * 32,
                        'messages': [{'role': 'user', 'content': question}, {'role': 'assistant', 'content': 'Answer.'}]})
                    assert response.status_code == 200
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await expect(page.locator('#agent-thread option')).to_have_count(2)
                selected = await page.locator('#agent-thread').input_value()
                await page.locator('#agent-files').set_input_files({'name': 'archive.zip', 'mimeType': 'application/zip', 'buffer': b'binary'})
                await expect(page.locator('#agent-status')).to_contain_text('use PNG')
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(0)
                await page.locator('#agent-files').set_input_files({'name': 'invalid.txt', 'mimeType': 'text/plain', 'buffer': b'\xff'})
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(1)
                await page.locator('#agent-send').click()
                await expect(page.locator('#agent-status')).to_contain_text('UTF-8')
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(1)
                await page.locator('#agent-attachments button').click()
                await page.locator('#agent-files').set_input_files({'name': 'draft.md', 'mimeType': 'text/markdown', 'buffer': b'A draft attachment'})
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(1)
                await page.locator('#agent-thread').select_option('a' * 32 if selected != 'a' * 32 else 'b' * 32)
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(0)
                await page.locator('#agent-thread').select_option(selected)
                await expect(page.locator('#agent-attachments')).to_contain_text('draft.md')
                await page.locator('#agent-clear').click()
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(0)
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_composer_accepts_exact_decoded_byte_limit_and_rejects_one_more(engine):
    config.set_ai_provider('codex')
    config.set_ai_enabled(True)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1000, 'height': 900})
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-agent:enabled').click()
                await page.locator('#agent-files').set_input_files([
                    {'name': 'first.txt', 'mimeType': 'text/plain', 'buffer': b'a' * (4 * 1024 * 1024)},
                    {'name': 'second.txt', 'mimeType': 'text/plain', 'buffer': b'b' * (2 * 1024 * 1024 - 1)}])
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(2)
                await expect(page.locator('#agent-attach')).to_be_enabled()
                await expect(page.locator('#agent-status')).not_to_contain_text('at most 6 MB')
                await page.locator('#agent-files').set_input_files({'name': 'last-byte.txt', 'mimeType': 'text/plain', 'buffer': b'c'})
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(3)
                await expect(page.locator('#agent-attach')).to_be_enabled()
                await page.locator('#agent-files').set_input_files({'name': 'over-limit.txt', 'mimeType': 'text/plain', 'buffer': b'd'})
                await expect(page.locator('#agent-status')).to_contain_text('at most 6 MB')
                await expect(page.locator('#agent-attachments .chat-attachment')).to_have_count(3)

    run_browser(scenario())
