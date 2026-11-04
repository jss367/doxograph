"""An uncertain/reverted write is still writing at final workspace selection."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import store
from test_ask_browser import _server
from test_browser import _answer
from test_core_workspace_boundary_browser import remove_and_wait, seed
from test_tools_action_history_browser import inert_tools as inert_tools


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['dataset', 'paper-note'])
@pytest.mark.parametrize('mode', ['lost-cancel', 'lost-discard', 'held'])
def test_reverted_write_during_held_delete_cannot_silently_reset(engine, kind, mode):
    removed, _, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            deleting, release_delete = asyncio.Event(), asyncio.Event()
            writing, release_write = asyncio.Event(), asyncio.Event()
            attempts = []

            async def hold_delete(route):
                if route.request.method == 'DELETE':
                    deleting.set()
                    await release_delete.wait()
                await route.continue_()

            async def update(route):
                if route.request.method != method:
                    await route.continue_()
                    return
                attempts.append(route.request.post_data_json)
                if len(attempts) == 1:
                    response = await route.fetch()
                    assert response.status == 200
                    writing.set()
                    if mode == 'held':
                        await release_write.wait()
                    # The server committed, but this response never reached the editor.
                    await route.abort('failed')
                else:
                    await route.fulfill(status=409, json={'detail':'Synthetic conflicting revision; inspect saved version.'})

            with _server() as url:
                if kind == 'dataset':
                    item = httpx.post(url+'/api/datasets', json={'name':'Synthetic', 'description':'Stored'}).json()
                    path, field, key = '/api/datasets/'+item['id'], '#dataset-description', 'description'
                    open_editor = "openPageView('datasets', {dataset:'%s'})" % item['id']
                    flush = 'Datasets.flush()'
                elif kind == 'research-note':
                    item = httpx.post(url+'/api/research-notes', json={'title':'Synthetic', 'body':'Stored'}).json()
                    path, field, key = '/api/research-notes/'+item['id'], '#note-body', 'body'
                    open_editor = "openPageView('notes', {note:'%s'})" % item['id']
                    flush = 'ResearchNotes.flush()'
                else:
                    path, field, key = '/api/papers/study', 'textarea[data-note="study"]', 'notes'
                    open_editor = None
                    flush = 'flushPaperNotes()'
                method = 'PATCH' if kind == 'paper-note' else 'PUT'
                baseline = 'Stored note' if kind == 'paper-note' else 'Stored'
                await page.route('**/api/papers/study/claims/'+removed, hold_delete)
                await remove_and_wait(page, url, removed)
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', destination)
                try:
                    await asyncio.wait_for(deleting.wait(), 5)
                    if open_editor:
                        await page.evaluate(open_editor)
                    else:
                        await page.locator('#papers li[data-paper="study"]').click()
                        await page.locator('[data-act="edit-note"][data-paper="study"]').click()
                    await expect(page.locator(field)).to_be_visible()
                    await page.route('**'+path, update)
                    await page.locator(field).fill('Committed transient')
                    await page.evaluate('code => { window.writing = eval(code); }', flush)
                    await asyncio.wait_for(writing.wait(), 5)
                    if mode != 'held':
                        assert await page.evaluate('window.writing') is False
                    await page.locator(field).fill(baseline)
                    release_delete.set()
                    if mode == 'held':
                        await page.evaluate('window.switching')
                        assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                        await expect(page.locator(field)).to_have_value(baseline)
                        release_write.set()
                        assert await page.evaluate('window.writing') is False
                    else:
                        await expect(page.locator('#ask')).to_be_visible(timeout=2000)
                        await _answer(page, ('Cancel' if mode == 'lost-cancel' else
                                            'Discard and switch' if kind == 'paper-note' else 'Discard changes'))
                        await page.evaluate('window.switching')
                        expected = destination if mode == 'lost-discard' else 'default'
                        assert await page.evaluate('workspaceClient.workspaceId') == expected
                        if mode == 'lost-cancel':
                            await expect(page.locator(field)).to_have_value(baseline)
                            assert await page.evaluate(flush) is False
                    # A refusal never replaces the server's acknowledged record.
                    actual = store.load_paper('study') if kind == 'paper-note' else next(row for row in httpx.get(url+path.rsplit('/', 1)[0]).json()['datasets' if kind == 'dataset' else 'notes'] if row['id'] == item['id'])
                    assert actual[key] == 'Committed transient'
                finally:
                    release_delete.set()
                    release_write.set()
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['dataset'])
def test_later_async_guard_cannot_invalidate_an_earlier_editor_check(engine, kind):
    _, _, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                path, collection, view, field = ('/api/datasets', 'datasets', 'datasets', '#dataset-description') if kind == 'dataset' else ('/api/research-notes', 'notes', 'notes', '#note-body')
                item = httpx.post(url+path, json={'name':'Synthetic'} if kind == 'dataset' else {'title':'Synthetic'}).json()
                await page.goto(url+'/#view='+view+('&dataset=' if kind == 'dataset' else '&note=')+item['id'])
                await expect(page.locator(field)).to_be_visible()
                await page.evaluate('''selector => {
                  const original = ResearchTools.beforeWorkspaceSelect;
                  ResearchTools.beforeWorkspaceSelect = async () => {
                    const allowed = await original();
                    const field = document.querySelector(selector);
                    field.value = 'Writing after the earlier check';
                    field.dispatchEvent(new Event('input', {bubbles:true}));
                    return allowed;
                  };
                  window.restoreGuard = () => { ResearchTools.beforeWorkspaceSelect = original; };
                }''', field)
                await page.evaluate('id => switchWorkspace(id)', destination)
                assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                await expect(page.locator(field)).to_have_value('Writing after the earlier check')
                await page.evaluate('window.restoreGuard()')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                actual = next(row for row in httpx.get(url+path).json()[collection] if row['id'] == item['id'])
                assert actual['description' if kind == 'dataset' else 'body'] == 'Writing after the earlier check'
                await page.evaluate('id => switchWorkspace(id)', destination)
                assert await page.evaluate('workspaceClient.workspaceId') == destination
    run_browser(asyncio.wait_for(scenario(), 20))
