"""Exercise dataset editing and linked evidence in Chromium and native WebKit."""
from pathlib import Path

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _answer, _server


async def ready(page):
    await page.wait_for_function("document.querySelector('#datasets-sheet').getAttribute('aria-busy') !== 'true'")


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_dataset_editor_and_linked_research(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Evaluation study'))
    claim = store.add_claim('study', {'text': 'Accuracy improves on the held-out corpus.'})
    store.add_facet('dataset', 'Datasets used')
    store.update_claim('study', claim['id'], {'facets': {'dataset': ['Eval corpus']}})
    other = config.create_workspace('Another project')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Dataset name', exact=True).fill('Evaluation corpus')
                await dialog.get_by_label('Description', exact=True).fill('A shared evaluation set for the project.')
                await dialog.get_by_label('Version', exact=True).fill('2026-11')
                await dialog.get_by_label('Source URL or local path', exact=True).fill('https://example.org/datasets/evaluation')
                await dialog.get_by_label('License', exact=True).fill('CC BY 4.0')
                await dialog.get_by_label('Access notes', exact=True).fill('Public download; preserve attribution.')
                await dialog.get_by_label('Evaluation', exact=True).check()
                await dialog.get_by_label('Other dataset names in findings', exact=True).fill('EVAL, corpus')
                await page.locator('#dataset-research').get_by_role('button', name=claim['text'], exact=True).wait_for()
                await dialog.get_by_text('Link papers and claims', exact=True).click()
                await page.locator('#dataset-link-options').get_by_label('Paper Evaluation study', exact=True).check()
                await page.locator('#dataset-link-options').get_by_label('Claim ' + claim['text'], exact=True).check()
                await dialog.get_by_role('button', name='Save dataset', exact=True).click()
                await ready(page)
                [saved] = httpx.get(url + '/api/datasets').json()['datasets']
                assert saved['roles'] == ['evaluation'] and saved['papers'] == ['study']
                assert saved['claims'] == [{'paper': 'study', 'claim': claim['id']}]
                assert saved['facet_values'] == ['EVAL, corpus']
                # Another window updates the record; a stale save preserves this draft.
                httpx.put(url + '/api/datasets/' + saved['id'], json={
                    **{key: saved[key] for key in ['name','description','source','version','license','access_notes','roles','facet_values','papers','claims']},
                    'revision': saved['revision'], 'version': '2026-12'}).raise_for_status()
                await dialog.get_by_label('Version', exact=True).fill('My unsaved version')
                await dialog.get_by_role('button', name='Save dataset', exact=True).click()
                await ready(page)
                assert 'changed in another window' in await page.locator('#datasets-status').inner_text()
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                await dialog.get_by_role('button', name='Reload saved version', exact=True).click()
                await ready(page)
                assert '2026-12' in await page.locator('#dataset-saved-version').inner_text()
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                await dialog.get_by_label('Version', exact=True).fill('2026-11')
                await dialog.get_by_role('button', name='Replace draft with saved version', exact=True).click()
                await ready(page)
                assert await dialog.get_by_label('Version', exact=True).input_value() == '2026-12'
                # Record bookmarks reopen the selected dataset after a reload.
                await page.reload()
                await ready(page)
                await dialog.wait_for(state='visible')
                assert await dialog.get_by_label('Dataset name', exact=True).input_value() == 'Evaluation corpus'
                if engine == 'chromium':
                    screenshot = Path(__file__).parents[1] / '.context' / 'datasets.png'
                    screenshot.parent.mkdir(exist_ok=True)
                    await page.screenshot(path=str(screenshot))
                await page.locator('#dataset-research').get_by_role('button', name=claim['text'], exact=True).click()
                await ready(page)
                assert not await dialog.is_visible()
                assert f'sel={claim["id"]}' in page.url
                assert httpx.get(url + '/api/datasets', headers={'X-Doxograph-Workspace': other['id']}).json() == {'datasets': []}
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_dataset_drafts_missing_links_and_deletion(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Original source'))
    claim = store.add_claim('study', {'text': 'Original result'})
    other = config.create_workspace('Separate project')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 390, 'height': 844})
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={
                    'name': '<img src=x onerror=alert(1)>', 'source': 'javascript:alert(1)',
                    'papers': ['study'], 'claims': [{'paper': 'study', 'claim': claim['id']}]}).json()
                store.paper_path('study').unlink()
                await page.goto(url + '/?dataset=' + item['id'])
                dialog = page.locator('#datasets-sheet')
                await dialog.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#dataset-research').get_by_text('Missing paper: study', exact=True).count() == 1
                assert await page.locator('#dataset-research').get_by_text('Missing claim: ' + claim['id'], exact=True).count() == 1
                assert await dialog.locator('img, a[href^="javascript:"]').count() == 0
                assert await dialog.evaluate('node => node.scrollWidth <= node.clientWidth + 1')
                await dialog.get_by_label('Dataset name', exact=True).fill('My draft')
                await page.evaluate("void switchWorkspace('" + other['id'] + "')")
                await _answer(page, 'Cancel')
                await ready(page)
                assert await dialog.get_by_label('Dataset name', exact=True).input_value() == 'My draft'
                assert await page.locator('#workspace').input_value() == 'default'
                await page.evaluate("void switchWorkspace('" + other['id'] + "')")
                await _answer(page, 'Discard changes')
                await dialog.wait_for(state='hidden')
                await page.wait_for_function('id => workspaceClient.workspaceId === id', arg=other['id'])
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                assert await page.locator('#datasets-list button').count() == 0
                await dialog.get_by_role('button', name='Close datasets', exact=True).click()
                await page.locator('#workspace').select_option('default')
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                await page.locator('#datasets-search').fill('not present')
                assert 'No datasets match your search.' in await page.locator('#datasets-list').inner_text()
                await page.locator('#datasets-search').fill('')
                # Missing links remain removable without deleting the record's other metadata.
                await dialog.get_by_text('Link papers and claims', exact=True).click()
                await page.locator('#dataset-link-options').get_by_label('Paper Missing paper: study', exact=True).uncheck()
                await page.locator('#dataset-link-options').get_by_label('Claim Missing claim: ' + claim['id'], exact=True).uncheck()
                await dialog.get_by_role('button', name='Save dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_role('button', name='Delete record', exact=True).click()
                await _answer(page, 'Delete record')
                await ready(page)
                assert httpx.get(url + '/api/datasets').json() == {'datasets': []}

    run_browser(scenario())
