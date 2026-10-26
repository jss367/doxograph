"""Workspace notes autosave, navigation, and recovery in real browsers."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from doxograph import config
from test_browser import _answer, _server
from test_research_tools_browser import ready


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
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
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
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_workspace_notes_inflight_edits_failures_and_conflicts(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
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
            await browser.close()
    asyncio.run(scenario())
