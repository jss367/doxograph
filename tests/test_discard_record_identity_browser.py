"""Discard consent belongs to the record, even when replacement text matches."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_ask_browser import _server
from test_browser import _answer
from test_tools_action_history_browser import inert_tools as inert_tools


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('editor', ['dataset', 'highlight'])
@pytest.mark.parametrize('decision', ['Cancel', 'Discard changes', 'same-record'])
def test_held_delete_discard_consent_includes_record_identity(engine, editor, decision):
    store.save_paper(store.new_paper('study', title='Synthetic study'))
    store.pdf_path('study').write_bytes(minimal_pdf('Synthetic marked text.'))
    claim = store.add_claim('study', {'text':'Synthetic finding'})['id']
    other = config.create_workspace('Synthetic other workspace')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release = asyncio.Event(), asyncio.Event()

            async def hold_delete(route):
                if route.request.method == 'DELETE':
                    held.set()
                    await release.wait()
                await route.continue_()

            await page.route(f'**/api/papers/study/claims/{claim}', hold_delete)
            with _server() as url:
                if editor == 'dataset':
                    first = httpx.post(url+'/api/datasets', json={'name':'Dataset A'}).json()
                    second = httpx.post(url+'/api/datasets', json={'name':'Dataset B'}).json()
                else:
                    notebook = httpx.get(url+'/api/notebook').json()
                    notebook['highlights'] = [
                        {'id':identity, 'paper':'study', 'page':1, 'rects':[{'x':.1,'y':.08,'w':.3,'h':.02}],
                         'text':'Synthetic marked text.', 'color':'yellow', 'note':'Saved note'}
                        for identity in ['mark-a', 'mark-b']]
                    assert httpx.put(url+'/api/notebook', json=notebook).status_code == 200
                await page.goto(url+'/#view=claims')
                await page.locator(f'.claim[data-claim="{claim}"] [data-act="del"]').click()
                await page.locator('#toasts .toast', has_text='Deleted the claim.').wait_for()
                if editor == 'dataset':
                    await page.get_by_role('button', name='Datasets', exact=True).click()
                    await page.locator(f'#datasets-list [data-id="{first["id"]}"]').click()
                    field = page.locator('#dataset-name')
                    await field.fill('Identical replacement draft')
                else:
                    await page.locator('#papers li[data-paper="study"]').click()
                    await page.locator('[data-highlight="mark-a"] [data-tool="edit-highlight"]').click()
                    field = page.locator('[data-highlight-note="mark-a"]')
                    await field.fill('Identical replacement draft')
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', other['id'])
                await _answer(page, 'Discard changes' if editor == 'dataset' else 'Discard note')
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    if decision != 'same-record':
                        if editor == 'dataset':
                            await page.locator(f'#datasets-list [data-id="{second["id"]}"]').click()
                            await _answer(page, 'Discard changes')
                            await expect(field).to_have_value('Dataset B')
                        else:
                            await page.locator('[data-highlight="mark-b"] [data-tool="edit-highlight"]').click()
                            await _answer(page, 'Discard note')
                            field = page.locator('[data-highlight-note="mark-b"]')
                            await expect(field).to_have_value('Saved note')
                        await field.fill('Identical replacement draft')
                    release.set()
                    if decision == 'same-record':
                        await page.evaluate('window.switching')
                        assert await page.evaluate('workspaceClient.workspaceId') == other['id']
                        await expect(page.locator('#ask')).not_to_be_visible()
                    else:
                        # Fail promptly on the original bug rather than waiting for a vanished prompt.
                        await expect(page.locator('#ask')).to_be_visible(timeout=2000)
                        await expect(page.locator('#ask')).to_contain_text('Discard')
                        await _answer(page, decision)
                        await page.evaluate('window.switching')
                        if decision == 'Cancel':
                            assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                            await expect(field).to_have_value('Identical replacement draft')
                            if editor == 'dataset':
                                await expect(page.locator(f'#datasets-list [data-id="{second["id"]}"]')).to_have_attribute('aria-current', 'page')
                            else:
                                await expect(page.locator('[data-highlight-note="mark-b"]')).to_be_visible()
                        else:
                            assert await page.evaluate('workspaceClient.workspaceId') == other['id']
                    # Neither original nor replacement record was silently saved by switching.
                    if editor == 'dataset':
                        saved = httpx.get(url+'/api/datasets', headers={'X-Doxograph-Workspace':'default'}).json()['datasets']
                        assert {row['name'] for row in saved} == {'Dataset A', 'Dataset B'}
                    else:
                        saved = httpx.get(url+'/api/notebook', headers={'X-Doxograph-Workspace':'default'}).json()
                        assert [row['note'] for row in saved['highlights']] == ['Saved note', 'Saved note']
                finally:
                    release.set()

    run_browser(scenario())
