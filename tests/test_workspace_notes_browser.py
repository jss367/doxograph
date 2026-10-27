"""Workspace notes autosave, navigation, and recovery in real browsers."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_browser import _answer, _server
from test_research_tools_browser import ready
from test_research_notes import image_bytes


async def open_notes(page):
    await page.locator('.research-tools-nav').get_by_role('button', name='Workspace notes', exact=True).click()
    await ready(page)
    return page.locator('#workspace-notes')


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_autosave_navigation_and_isolation(engine):
    config.set_ai_enabled(False)
    other = config.create_workspace('Other research')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                editor = await open_notes(page)
                await expect(editor).to_have_value('')
                await expect(editor).to_be_focused()
                text = '# Research questions\n\nWhat helps recovery?\n\n# Next steps\nCompare conditions.'
                await editor.fill(text)
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == text
                # Polls and saves must not redraw the editor or move its caret.
                await expect(editor).to_be_focused()
                Path('.context').mkdir(exist_ok=True)
                if engine == 'chromium':
                    await page.screenshot(path='.context/workspace-notes.png')

                # Closing immediately flushes the debounced edit without a prompt.
                await editor.fill(text + '\nRead the methods.')
                await page.get_by_role('button', name='Close research tools').click()
                await page.locator('#research-tools').wait_for(state='hidden')
                assert httpx.get(url + '/api/notebook').json()['notes'].endswith('Read the methods.')
                await page.reload()
                editor = await open_notes(page)
                await expect(editor).to_have_value(text + '\nRead the methods.')

                await editor.fill('Saved before changing tools')
                await page.locator('#research-tabs').get_by_role('button', name='Evidence boards', exact=True).click()
                await ready(page)
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Saved before changing tools'
                await page.locator('#research-tabs').get_by_role('button', name='Workspace notes', exact=True).click()
                await ready(page)
                await editor.fill('Saved in the original workspace')
                # Use the app's switch guard, also used by workspace links.
                await page.evaluate('id => switchWorkspace(id)', other['id'])
                await page.locator('#research-tools').wait_for(state='hidden')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Saved in the original workspace'
                editor = await open_notes(page)
                await expect(editor).to_have_value('')
                await editor.fill('Only in other research')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                headers = {'X-Doxograph-Workspace': other['id']}
                assert httpx.get(url + '/api/notebook', headers=headers).json()['notes'] == 'Only in other research'
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Saved in the original workspace'

                # Clearing the document is a saved edit as well.
                await editor.fill('')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook', headers=headers).json()['notes'] == ''
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_inflight_edits_failures_and_conflicts(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                editor = await open_notes(page)
                started, release = asyncio.Event(), asyncio.Event()

                async def delay_save(route):
                    started.set()
                    await release.wait()
                    await route.continue_()

                await page.route('**/api/notebook/notes', delay_save, times=1)
                await editor.fill('First edit')
                await asyncio.wait_for(started.wait(), timeout=10)
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saving…')
                await expect(editor).to_be_enabled()
                # New text during an in-flight save must also reach disk.
                literal = 'Latest edit <script>throw new Error("unsafe")</script>'
                await editor.fill(literal)
                release.set()
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == literal

                async def fail_save(route):
                    await route.fulfill(status=503, content_type='application/json', body='{"detail":"Temporary connection failure"}')

                await page.route('**/api/notebook/notes', fail_save, times=1)
                await editor.fill('Keep this draft after a failure')
                await expect(page.locator('#workspace-notes-status')).to_contain_text('Couldn’t save.')
                await expect(editor).to_have_value('Keep this draft after a failure')
                await page.get_by_role('button', name='Retry save', exact=True).click()
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Keep this draft after a failure'

                async def lose_response(route):
                    await route.fetch()
                    await fail_save(route)

                await page.route('**/api/notebook/notes', lose_response, times=1)
                await editor.fill('Saved even though the response was lost')
                await expect(page.locator('#workspace-notes-status')).to_contain_text('Couldn’t save.')
                await page.get_by_role('button', name='Retry save', exact=True).click()
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')

                # A failed tab load leaves the existing editor usable.
                await page.route('**/api/notebook', fail_save, times=1)
                await page.locator('#research-tabs').get_by_role('button', name='Evidence boards', exact=True).click()
                await ready(page)
                await expect(page.locator('#research-message')).to_contain_text('Temporary connection failure')
                await editor.fill('Still editable after a failed tab load')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Still editable after a failed tab load'

                data = httpx.get(url + '/api/notebook').json()
                data['notes'] = 'A note from another window'
                assert httpx.put(url + '/api/notebook', json=data).status_code == 200
                await editor.fill('My conflicting draft')
                await expect(page.locator('#workspace-notes-status')).to_contain_text('another window')
                await page.get_by_role('button', name='Close research tools').click()
                await expect(editor).to_have_value('My conflicting draft')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'A note from another window'
                await page.get_by_role('button', name='Reload saved version').click()
                await _answer(page, 'Discard changes')
                await ready(page)
                await expect(editor).to_have_value('A note from another window')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_rich_writing_and_images(engine):
    config.set_ai_enabled(False)
    other = config.create_workspace('Other research')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: external.append(req.url) if not req.url.startswith(('http://127.0.0.1:', 'blob:', 'data:')) else None)
            with _server() as url:
                # Existing plain-text notes open and stay intact.
                legacy = 'Questions and next steps\nCompare the conditions.'
                httpx.put(url + '/api/notebook/notes', json={'notes': legacy, 'previous': ''}).raise_for_status()
                await page.goto(url)
                editor = await open_notes(page)
                dialog = page.locator('#research-tools')
                preview = page.locator('#workspace-notes-preview')
                await expect(editor).to_have_value(legacy)
                await expect(preview).to_contain_text('Questions and next steps')

                content = r'''# Project questions

Which conditions improve recovery?

Inline math: $p = \frac{k}{n}$.

$$
\Delta = r_{\text{intervention}} - r_{\text{baseline}}
$$

| Condition | Recovery |
| --- | --- |
| Baseline | 0.46 |
| Intervention | 0.71 |

## Next steps

- Compare task differences
- Replicate the intervention
'''
                await editor.fill(content)
                await expect(preview.locator('h1')).to_have_text('Project questions')
                await expect(preview.locator('.katex')).to_have_count(2)
                await expect(preview.locator('table')).to_have_count(1)
                await expect(preview.locator('li')).to_have_count(2)
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == content
                assert httpx.get(url + '/api/research-notes').json()['notes'] == []

                await page.locator('#workspace-notes-view').select_option('preview')
                await expect(editor).not_to_be_visible()
                await expect(preview).to_be_visible()
                await page.locator('#workspace-notes-view').select_option('write')
                await expect(preview).not_to_be_visible()
                await expect(editor).to_have_value(content)
                await page.locator('#workspace-notes-view').select_option('split')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/workspace-notes-rich.png')

                # Formatting edits the selection and follows the same autosave path.
                await editor.fill('A useful question')
                await editor.evaluate('(field) => field.setSelectionRange(2, 8)')
                await dialog.get_by_role('button', name='Bold', exact=True).click()
                await expect(editor).to_have_value('A **useful** question')
                await expect(editor).to_be_focused()
                await expect(preview.locator('strong')).to_have_text('useful')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')

                # A toolbar insertion in Preview reveals the editable source again.
                await page.locator('#workspace-notes-view').select_option('preview')
                await dialog.get_by_role('button', name='Equation', exact=True).click()
                await expect(editor).to_be_visible()
                await expect(page.locator('#workspace-notes-view')).to_have_value('split')
                await expect(preview.locator('.katex-display')).to_have_count(1)

                # Unsafe HTML and remote images never execute or make requests.
                await editor.fill('<script>window.noteUnsafe = true</script>\n\n[Unsafe](javascript:alert(1))\n\n![Remote](https://example.invalid/tracking.png)')
                await expect(preview).to_contain_text('[Image: Remote]')
                await expect(preview.locator('script, [href^="javascript:"], img')).to_have_count(0)
                assert await page.evaluate('window.noteUnsafe') is None

                # A failed upload can be retried with the same file.
                async def fail_upload(route):
                    await route.fulfill(status=503, content_type='application/json', body='{"detail":"Image upload failed"}')

                await page.route('**/api/research-notes/images', fail_upload, times=1)
                await editor.fill('Project figure\n')
                image = {'name': 'figure.png', 'mimeType': 'image/png', 'buffer': image_bytes()}
                for attempt in range(3):
                    async with page.expect_file_chooser() as chooser:
                        await dialog.get_by_role('button', name='Image', exact=True).click()
                    await (await chooser.value).set_files(image)
                    await ready(page)
                    if attempt == 0:
                        await expect(page.locator('#research-message')).to_have_text('Image upload failed')
                        await expect(editor).to_have_value('Project figure\n')
                    else:
                        await expect(preview.locator('img')).to_have_count(attempt)
                await page.wait_for_function("[...document.querySelectorAll('#workspace-notes-preview img')].every(img => img.complete && img.naturalWidth === 80)")
                # Clipboard and dropped images use the same local upload path.
                for count, kind in enumerate(['paste', 'drop'], start=3):
                    await editor.evaluate('''(field, {kind, bytes}) => {
                        const transfer = new DataTransfer();
                        transfer.items.add(new File([new Uint8Array(bytes)], kind + '.png', {type:'image/png'}));
                        const event = kind === 'paste'
                          ? new ClipboardEvent('paste', {clipboardData:transfer, bubbles:true, cancelable:true})
                          : new DragEvent('drop', {dataTransfer:transfer, bubbles:true, cancelable:true});
                        field.dispatchEvent(event);
                    }''', {'kind': kind, 'bytes': list(image_bytes())})
                    await ready(page)
                    await expect(preview.locator('img')).to_have_count(count)
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                saved = httpx.get(url + '/api/notebook').json()['notes']
                assert saved.count('![figure.png]') == 2

                await page.reload()
                editor = await open_notes(page)
                await expect(editor).to_have_value(saved)
                await expect(preview.locator('img')).to_have_count(4)
                await page.evaluate('id => switchWorkspace(id)', other['id'])
                await page.locator('#research-tools').wait_for(state='hidden')
                await open_notes(page)
                await expect(preview.locator('img')).to_have_count(0)
                await expect(editor).to_have_value('')
                assert not errors
                assert not external

    run_browser(scenario())
