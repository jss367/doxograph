"""Workspace notes autosave, navigation, and recovery in real browsers."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _answer, _server
from test_research_tools_browser import ready
from test_research_notes import image_bytes


async def open_notes(page):
    await page.locator('.research-tools-nav').get_by_role('button', name='Workspace notes', exact=True).click()
    editor = page.locator('#workspace-notes')
    await expect(editor).to_be_visible()
    return editor


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
                await editor.evaluate('(field) => field.setSelectionRange(5, 12)')
                await page.evaluate('updateFromPoll()')
                await expect(editor).to_be_focused()
                assert await editor.evaluate('(field) => [field.selectionStart, field.selectionEnd]') == [5, 12]
                Path('.context').mkdir(exist_ok=True)
                if engine == 'chromium':
                    await page.screenshot(path='.context/workspace-notes.png')

                # Collapsing immediately flushes the debounced edit without a prompt.
                await editor.fill(text + '\nRead the methods.')
                await page.get_by_role('button', name='Collapse notes', exact=True).click()
                await expect(editor).to_be_hidden()
                await expect(page.locator('#workspace-notes-preview')).to_have_text(text + '\nRead the methods.')
                assert httpx.get(url + '/api/notebook').json()['notes'].endswith('Read the methods.')
                await page.reload()
                editor = await open_notes(page)
                await expect(editor).to_have_value(text + '\nRead the methods.')

                await editor.fill('Saved before changing tools')
                await page.locator('.research-tools-nav').get_by_role('button', name='Evidence boards', exact=True).click()
                await ready(page)
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Saved before changing tools'
                await page.get_by_role('button', name='Close research tools').click()
                await page.locator('#research-tools').wait_for(state='hidden')
                editor = await open_notes(page)
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
    other = config.create_workspace('Other research')
    store.save_paper(store.new_paper('paper', title='Evidence to browse'))
    for index in range(20):
        store.add_claim('paper', {'text': f'Finding {index}.', 'tags': [], 'quote': 'An observation.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}/#view=claims")
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
                # Tool navigation reveals an offscreen save error instead of
                # appearing to ignore the click while the draft stays hidden.
                await page.locator('#main').evaluate('(node) => { node.scrollTop = node.scrollHeight; }')
                await expect(page.locator('#workspace-notes-panel')).not_to_be_in_viewport()
                await page.route('**/api/notebook/notes', fail_save, times=1)
                await page.locator('.research-tools-nav').get_by_role('button', name='Evidence boards', exact=True).click()
                await expect(page.locator('#workspace-notes-panel')).to_be_in_viewport()
                await expect(page.locator('#research-tools')).not_to_be_visible()
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

                # A failed research-tool load leaves the inline editor usable.
                await page.route('**/api/notebook', fail_save, times=1)
                await page.locator('.research-tools-nav').get_by_role('button', name='Evidence boards', exact=True).click()
                await ready(page)
                await expect(page.locator('#research-message')).to_contain_text('Temporary connection failure')
                await page.get_by_role('button', name='Close research tools').click()
                await page.locator('#research-tools').wait_for(state='hidden')
                await editor.fill('Still editable after a failed tab load')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Still editable after a failed tab load'

                data = httpx.get(url + '/api/notebook').json()
                data['notes'] = 'A note from another window'
                assert httpx.put(url + '/api/notebook', json=data).status_code == 200
                await editor.fill('My conflicting draft')
                await expect(page.locator('#workspace-notes-status')).to_contain_text('another window')
                await page.evaluate('id => switchWorkspace(id)', other['id'])
                assert await page.locator('#workspace').input_value() == 'default'
                await page.get_by_role('button', name='Collapse notes', exact=True).click()
                await expect(editor).to_be_visible()
                await expect(editor).to_have_value('My conflicting draft')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'A note from another window'
                await page.locator('#workspace-notes-reload').click()
                await _answer(page, 'Discard changes')
                await expect(editor).to_have_value('A note from another window')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_in_main_view_and_beside_paper(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('recovery', title='Recovery after model steering', authors=['Ada Researcher'], year=2026))
    store.pdf_path('recovery').write_bytes(minimal_pdf(['Recovery depends on the training conditions.']))
    store.add_tag('recovery')
    store.add_claim('recovery', {'text': 'Recovery depends on the training conditions.', 'tags': ['recovery'],
                               'quote': 'Recovery depends on the training conditions.'})
    store.save_paper(store.new_paper('methods', title='Comparing recovery measurements', authors=['Bo Researcher'], year=2025))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                editor = page.locator('#workspace-notes')
                panel = page.locator('#workspace-notes-panel')
                await expect(editor).to_be_visible()
                await expect(editor).not_to_be_focused()
                assert await panel.evaluate('(node) => !!node.closest("#workspace-notes-home")')
                await editor.fill('Research question\nWhat helps a model recover after steering?\n\nNext steps\nCompare training conditions and check how recovery was measured.')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                await page.locator('#workspace-notes-keep').check()
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/workspace-notes-main.png')

                await page.locator('#papers li[data-paper="recovery"]').click()
                await page.locator('#reader .pdf-page canvas').wait_for()
                await expect(editor).to_be_visible()
                assert await panel.evaluate('(node) => !!node.closest("#claim-column")')
                # Reparenting keeps one editor, and its draft survives filtering.
                assert await page.locator('#workspace-notes').count() == 1
                snapshot_started, release_snapshot = asyncio.Event(), asyncio.Event()

                async def stale_snapshot(route):
                    response = await route.fetch()
                    snapshot_started.set()
                    await release_snapshot.wait()
                    await route.fulfill(response=response)

                await page.route('**/api/state', stale_snapshot, times=1)
                await page.evaluate('() => { workspaceClient.invalidateState(); window.oldNotesPoll = updateFromPoll(); }')
                await asyncio.wait_for(snapshot_started.wait(), timeout=10)
                await editor.fill('Working conclusion: recovery varies with the training conditions.')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                release_snapshot.set()
                await page.evaluate('window.oldNotesPoll')
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('no matching claims')
                await expect(editor).to_have_value('Working conclusion: recovery varies with the training conditions.')
                await page.locator('#q').fill('')
                if engine == 'chromium':
                    await page.screenshot(path='.context/workspace-notes-reading.png')

                await page.get_by_role('button', name='Collapse notes', exact=True).click()
                await expect(page.locator('#workspace-notes-preview')).to_have_text('Working conclusion: recovery varies with the training conditions.')
                await page.reload()
                await expect(panel).to_be_visible()
                await expect(editor).to_be_hidden()
                await expect(page.locator('#workspace-notes-keep')).to_be_checked()
                await page.locator('#workspace-notes-keep').uncheck()
                await expect(panel).to_be_hidden()
                # The sidebar brings the editor back without covering the paper.
                editor = await open_notes(page)
                await expect(page.locator('#research-tools')).not_to_be_visible()
                await expect(page.locator('#reader')).to_be_visible()
                await page.locator('#btn-back-library').click()
                await expect(page.locator('#workspace-notes-home #workspace-notes')).to_be_visible()

                # A clean, unfocused overview follows another window's saved text.
                data = httpx.get(url + '/api/notebook').json()
                data['notes'] = 'Updated from another window <script>unsafe()</script>'
                assert httpx.put(url + '/api/notebook', json=data).status_code == 200
                await page.locator('#q').focus()
                await page.evaluate('updateFromPoll()')
                await expect(editor).to_have_value(data['notes'])
                await page.get_by_role('button', name='Collapse notes', exact=True).click()
                await expect(page.locator('#workspace-notes-preview')).to_have_text(data['notes'])
                assert await page.locator('#workspace-notes-panel script').count() == 0
                # A narrow main view wraps controls without horizontal overflow.
                await page.set_viewport_size({'width': 900, 'height': 800})
                assert await panel.evaluate('(node) => node.scrollWidth <= node.clientWidth')
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
                dialog = page.locator('#workspace-notes-panel')
                preview = page.locator('#workspace-notes-rich-preview')
                await expect(editor).to_have_value(legacy)
                await page.locator('#workspace-notes-view').select_option('split')
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
                    await page.wait_for_function("document.querySelector('#workspace-notes-image-status').textContent !== 'Uploading image…'")
                    if attempt == 0:
                        await expect(page.locator('#workspace-notes-image-status')).to_have_text('Image upload failed')
                        await expect(editor).to_have_value('Project figure\n')
                    else:
                        await expect(preview.locator('img')).to_have_count(attempt)
                await page.wait_for_function("[...document.querySelectorAll('#workspace-notes-rich-preview img')].every(img => img.complete && img.naturalWidth === 80)")
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
                # A workspace switch waits for an image upload and saves its link
                # into the original workspace before exposing the next editor.
                upload_started, release_upload = asyncio.Event(), asyncio.Event()

                async def delayed_upload(route):
                    upload_started.set()
                    await release_upload.wait()
                    await route.continue_()

                await page.route('**/api/research-notes/images', delayed_upload, times=1)
                await page.locator('#workspace-notes-image').set_input_files(image)
                await asyncio.wait_for(upload_started.wait(), timeout=10)
                await page.evaluate('id => { window.imageWorkspaceSwitch = switchWorkspace(id); }', other['id'])
                assert await page.locator('#workspace').input_value() == 'default'
                release_upload.set()
                await page.evaluate('window.imageWorkspaceSwitch')
                assert httpx.get(url + '/api/notebook').json()['notes'].count('![figure.png]') == 3
                await page.locator('#research-tools').wait_for(state='hidden')
                await open_notes(page)
                await expect(preview.locator('img')).to_have_count(0)
                await expect(editor).to_have_value('')
                assert not errors
                assert not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_apply_focused_snapshot_on_blur_and_collapse(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                editor = await open_notes(page)
                await editor.fill('Original notes')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                for text, action in [('From another window', 'blur'), ('Another update', 'collapse')]:
                    await editor.focus()
                    data = httpx.get(url + '/api/notebook').json()
                    data['notes'] = text
                    httpx.put(url + '/api/notebook', json=data).raise_for_status()
                    before = await editor.input_value()
                    await page.evaluate('updateFromPoll()')
                    await expect(editor).to_have_value(before)
                    if action == 'blur':
                        # Moving focus does not request a render or another poll.
                        await page.locator('#workspace-notes-toggle').focus()
                        await expect(editor).to_have_value(text)
                    else:
                        # Programmatic clicks need not move focus automatically.
                        await page.evaluate("document.getElementById('workspace-notes-toggle').click()")
                        await expect(editor).to_be_hidden()
                        await expect(page.locator('#workspace-notes-preview')).to_have_text(text)
                    assert httpx.get(url + '/api/notebook').json()['notes'] == text

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('save_fails', [False, True])
def test_workspace_notes_typed_during_delete_drain_are_saved_before_switch(engine, save_fails):
    config.set_ai_enabled(False)
    other = config.create_workspace('Other research')
    store.save_paper(store.new_paper('paper', title='A paper to review'))
    store.add_claim('paper', {'text': 'A claim to delete.', 'tags': [], 'quote': 'A claim.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}/#view=claims")
                editor = await open_notes(page)
                await page.locator('.claim [data-act="del"]').first.click()
                await page.locator('#toasts .toast', has_text='Deleted the claim.').wait_for()
                deleting, release_delete = asyncio.Event(), asyncio.Event()

                async def delay_delete(route):
                    deleting.set()
                    await release_delete.wait()
                    await route.continue_()

                async def fail_save(route):
                    await route.fulfill(status=503, content_type='application/json', body='{"detail":"Save failed during navigation"}')

                await page.route('**/api/papers/paper/claims/*', delay_delete)
                if save_fails:
                    await page.route('**/api/notebook/notes', fail_save)
                await page.evaluate('id => { window.drainingNoteSwitch = switchWorkspace(id); }', other['id'])
                await asyncio.wait_for(deleting.wait(), timeout=10)
                text = 'Typed while the old workspace was waiting for a delete.'
                await editor.fill(text)
                release_delete.set()
                await page.evaluate('window.drainingNoteSwitch')
                if save_fails:
                    assert await page.locator('#workspace').input_value() == 'default'
                    await expect(editor).to_have_value(text)
                    await expect(page.locator('#workspace-notes-status')).to_contain_text('Save failed during navigation')
                    await page.unroute('**/api/notebook/notes', fail_save)
                    await page.locator('#workspace-notes-retry').click()
                    await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                else:
                    assert await page.locator('#workspace').input_value() == other['id']
                    await expect(editor).to_have_value('')
                assert httpx.get(url + '/api/notebook').json()['notes'] == text
                headers = {'X-Doxograph-Workspace': other['id']}
                assert httpx.get(url + '/api/notebook', headers=headers).json()['notes'] == ''
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('load_fails', [False, True])
def test_workspace_notes_old_editor_is_hidden_while_destination_loads(engine, load_fails):
    config.set_ai_enabled(False)
    other = config.create_workspace('Other research')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                httpx.put(url + '/api/notebook/notes', json={'notes': 'Original workspace notes', 'previous': ''}).raise_for_status()
                await page.goto(url)
                editor = await open_notes(page)
                loading, release = asyncio.Event(), asyncio.Event()

                async def delay_destination(route):
                    loading.set()
                    await release.wait()
                    if load_fails:
                        await route.fulfill(status=503, content_type='application/json', body='{"detail":"Destination unavailable"}')
                    else:
                        await route.continue_()

                await page.route('**/api/state', delay_destination, times=1)
                await page.evaluate('id => { window.loadingNoteSwitch = switchWorkspace(id).catch(error => error.message); }', other['id'])
                await asyncio.wait_for(loading.wait(), timeout=10)
                assert await page.evaluate('workspaceClient.workspaceId') == other['id']
                await expect(editor).to_be_hidden()
                # The sidebar must not reveal the old draft while state is absent.
                await page.locator('.research-tools-nav').get_by_role('button', name='Workspace notes', exact=True).click()
                await expect(editor).to_be_hidden()
                release.set()
                result = await page.evaluate('window.loadingNoteSwitch')
                if load_fails:
                    assert result == 'Destination unavailable'
                    await expect(editor).to_be_hidden()
                    await page.evaluate('refresh()')
                await expect(editor).to_be_visible()
                await expect(editor).to_have_value('')
                await editor.fill('Written in the destination')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'Original workspace notes'
                headers = {'X-Doxograph-Workspace': other['id']}
                assert httpx.get(url + '/api/notebook', headers=headers).json()['notes'] == 'Written in the destination'
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_escape_preserves_an_unrelated_claim_draft(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A paper to review'))
    store.add_claim('paper', {'text': 'Original claim.', 'tags': [], 'quote': 'An observation.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=claims")
                await page.locator('.claim [data-act="edit"]').first.click()
                form = page.locator('form[data-form]')
                claim = form.locator('textarea[name="text"]')
                await claim.fill('This claim draft must survive.')
                editor = await open_notes(page)
                await editor.fill('These workspace notes must survive too.')
                for target in ['#workspace-notes', '#workspace-notes-view', '#workspace-notes-toggle']:
                    await page.locator(target).focus()
                    await page.keyboard.press('Escape')
                    await expect(claim).to_have_value('This claim draft must survive.')
                    await expect(editor).to_have_value('These workspace notes must survive too.')
                await expect(page.locator('#workspace-notes-status')).to_have_text('Saved')
                await form.get_by_role('button', name='Save', exact=True).click()
                await expect(form).not_to_be_visible()
                assert httpx.get(url + '/api/notebook').json()['notes'] == 'These workspace notes must survive too.'
                assert httpx.get(url + '/api/papers/paper').json()['claims'][0]['text'] == 'This claim draft must survive.'
                # Escape still cancels the claim editor when that editor owns focus.
                await page.locator('.claim [data-act="edit"]').first.click()
                await claim.fill('Cancel this claim edit.')
                await claim.press('Escape')
                await expect(form).not_to_be_visible()
                assert httpx.get(url + '/api/papers/paper').json()['claims'][0]['text'] == 'This claim draft must survive.'

    run_browser(scenario())
