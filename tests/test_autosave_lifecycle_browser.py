"""Clean editors and inactive redraws do not create save/discard work."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models, open_note, seed


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('saved_edit', [False, True], ids=['opened-clean', 'autosaved-open'])
def test_clean_open_paper_note_does_not_prompt_on_workspace_switch(engine, saved_edit):
    seed()
    other = config.create_workspace('Synthetic second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await open_note(page, url)
                if saved_edit:
                    await page.locator('textarea[data-note="paper"]').fill('Acknowledged edit')
                    assert await page.evaluate('flushPaperNotes()') is True
                assert await page.evaluate('V.noteEditing') == 'paper'
                assert await page.evaluate('Object.keys(V.noteDrafts)') == []
                await page.evaluate('''id => { window.lifecycleSwitch = false;
                  void switchWorkspace(id).then(() => { window.lifecycleSwitch = true; });
                }''', other)
                await page.wait_for_function('window.lifecycleSwitch || document.querySelector("#ask[open]")')
                await expect(page.locator('#ask')).not_to_be_visible()
                await page.wait_for_function('window.lifecycleSwitch')
                assert await page.evaluate('workspaceClient.workspaceId') == other

    run_browser(scenario())
    assert store.load_paper('paper')['notes'] == ('Acknowledged edit' if saved_edit else 'Stored note')


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('editor', ['dataset', 'research-note'])
def test_inactive_redraw_does_not_retry_failed_editor_save(engine, editor):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            writes, second = [], asyncio.Event()
            recover = False

            async def fail_write(route):
                if route.request.method != 'PUT':
                    await route.continue_()
                    return
                writes.append(route.request.post_data_json)
                if len(writes) == 2:
                    second.set()
                if recover:
                    await route.continue_()
                else:
                    await route.fulfill(status=503, json={'detail': 'Synthetic offline save'})

            with _server() as url:
                if editor == 'dataset':
                    item = httpx.post(url + '/api/datasets', json={'name': 'Synthetic dataset'}).json()
                    path, bookmark = '/api/datasets/' + item['id'], '/?dataset=' + item['id']
                    field, status, value_key = '#dataset-description', '#datasets-status', 'description'
                else:
                    item = httpx.post(url + '/api/research-notes', json={'title': 'Synthetic note', 'body': 'Stored'}).json()
                    path, bookmark = '/api/research-notes/' + item['id'], '/?note=' + item['id']
                    field, status, value_key = '#note-body', '#notes-status', 'body'
                await page.goto(url + bookmark)
                await expect(page.locator(field)).to_be_visible()
                await page.route('**' + path, fail_write)
                await page.locator(field).fill('Retained failed draft')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                assert len(writes) == 1
                await expect(page.locator(status)).to_contain_text('Synthetic offline save')
                # One actual hide transition flushes; later inactive redraws do not.
                await page.evaluate('V.view = "library"; renderContent()')
                await asyncio.wait_for(second.wait(), 5)
                await page.wait_for_function('workspaceClient.pendingMutations === 0')
                for _ in range(3):
                    await page.evaluate('drawPageViews()')
                    await page.wait_for_function('workspaceClient.pendingMutations === 0')
                assert len(writes) == 2
                assert await page.locator(field).input_value() == 'Retained failed draft'
                await expect(page.locator(status)).to_contain_text('Synthetic offline save')
                recover = True
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert len(writes) == 3
                saved = httpx.get(url + ('/api/datasets' if editor == 'dataset' else '/api/research-notes')).json()
                records = saved['datasets' if editor == 'dataset' else 'notes']
                assert next(r for r in records if r['id'] == item['id'])[value_key] == 'Retained failed draft'

    run_browser(scenario())
