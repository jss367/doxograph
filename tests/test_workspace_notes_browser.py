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
                await page.locator('[data-clear-filter="paper"]').click()
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
