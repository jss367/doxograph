"""Frozen quit consent names the retained writing, not a whole quit attempt."""
import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_tools_action_history_browser import inert_tools as inert_tools


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['dataset', 'research-note', 'claim', 'references'])
def test_quit_snapshot_is_stable_for_same_writing_and_changes_for_new_state(engine, kind):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                if kind in ('dataset', 'research-note'):
                    path = '/api/datasets' if kind == 'dataset' else '/api/research-notes'
                    item = httpx.post(url+path, json={'name':'Synthetic'} if kind == 'dataset' else {'title':'Synthetic'}).json()
                    view, query, selector = ('datasets', 'dataset', '#dataset-description') if kind == 'dataset' else ('notes', 'note', '#note-body')
                    await page.goto(url+f'/#view={view}&{query}='+item['id'])
                    field = page.locator(selector)
                    await expect(field).to_be_visible()
                else:
                    await page.goto(url if kind == 'references' else url+'/#view=claims')
                    if kind == 'claim':
                        field, _ = await editor(page, kind, claims)
                    else:
                        field = page.locator('#refs')
                # Startup populates controls that are included in the snapshot.
                await page.wait_for_function('libraryInitialized')
                before = await page.evaluate('window.doxographQuitWritingSnapshot()')
                if kind == 'references':
                    # A populated library collapses import; hidden retained input still counts.
                    await field.evaluate("field => { field.value = 'New retained writing'; field.dispatchEvent(new Event('input', {bubbles:true})); }")
                else:
                    await field.fill('New retained writing')
                changed = await page.evaluate('window.doxographQuitWritingSnapshot()')
                assert before != changed
                assert await page.evaluate('window.doxographQuitWritingSnapshot()') == changed
                # Programmatic asynchronous changes are possible despite an inert page.
                await page.evaluate('document.body.inert = true')
                await field.evaluate("field => { field.value = 'Later completed writing'; }")
                assert await page.evaluate('window.doxographQuitWritingSnapshot()') != changed
    run_browser(scenario())
