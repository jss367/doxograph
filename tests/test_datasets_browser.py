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
def test_open_dataset_in_loupe_preserves_drafts_and_handles_errors(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1100, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Dataset name', exact=True).fill('Conversation examples')
                source = dialog.get_by_label('Source URL or local path', exact=True)
                await source.fill('/data/Conversation examples.jsonl')
                launches = []

                async def launch(route):
                    launches.append(route.request.post_data_json)
                    await route.fulfill(json={'url': url + '/?loupe-viewer'})

                await page.route('**/api/datasets/loupe', launch)
                async with page.expect_popup() as opened:
                    await dialog.get_by_role('button', name='Open in Loupe', exact=True).click()
                viewer = await opened.value
                await viewer.wait_for_url(url + '/?loupe-viewer')
                await ready(page)
                assert await viewer.evaluate('window.opener === null')
                assert launches == [{'source': '/data/Conversation examples.jsonl'}]
                assert 'still unsaved' in await page.locator('#datasets-status').inner_text()
                assert httpx.get(url + '/api/datasets').json() == {'datasets': []}
                assert await source.input_value() == '/data/Conversation examples.jsonl'
                await viewer.close()

                # A blocked popup still leaves a normal, usable viewer link.
                await page.evaluate('window.open = () => null')
                await dialog.get_by_role('button', name='Open in Loupe', exact=True).click()
                await ready(page)
                assert await page.get_by_role('link', name='Open viewer', exact=True).get_attribute('href') == url + '/?loupe-viewer'

                await page.unroute('**/api/datasets/loupe', launch)
                await page.route('**/api/datasets/loupe', lambda route: route.fulfill(
                    status=422, json={'detail': 'This dataset format is not supported by Loupe.'}))
                await dialog.get_by_role('button', name='Open in Loupe', exact=True).click()
                await ready(page)
                assert 'not supported' in await page.locator('#datasets-status').inner_text()
                assert await source.input_value() == '/data/Conversation examples.jsonl'
                assert await dialog.get_by_role('button', name='Open in Loupe', exact=True).is_enabled()
                await source.fill('https://example.org/dataset.json')
                assert await dialog.get_by_role('button', name='Open in Loupe', exact=True).count() == 0
                assert await dialog.get_by_role('link', name='Open source', exact=True).get_attribute('href') == 'https://example.org/dataset.json'
                await source.fill('javascript:alert(1)')
                assert await page.locator('#dataset-source-link').inner_text() == ''
                assert not errors

    run_browser(scenario())


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
                await dialog.get_by_label('Labels', exact=True).fill('needs review, Open source, OPEN SOURCE')
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
                assert saved['labels'] == ['needs review', 'Open source']
                assert await dialog.get_by_label('Labels', exact=True).input_value() == 'needs review, Open source'
                assert await page.locator('#datasets-list .dataset-label').all_text_contents() == saved['labels']
                await page.locator('#datasets-search').fill('OPEN SOURCE')
                assert await page.locator('#datasets-list button').count() == 1
                await page.locator('#datasets-search').fill('private')
                assert await page.locator('#datasets-list button').count() == 0
                await page.locator('#datasets-search').fill('')
                # Another window updates the record; a stale save preserves this draft.
                httpx.put(url + '/api/datasets/' + saved['id'], json={
                    **{key: saved[key] for key in ['name','description','source','version','license','access_notes','roles','labels','facet_values','papers','claims']},
                    'revision': saved['revision'], 'version': '2026-12'}).raise_for_status()
                await dialog.get_by_label('Version', exact=True).fill('My unsaved version')
                await dialog.get_by_role('button', name='Save dataset', exact=True).click()
                await ready(page)
                assert 'changed in another window' in await page.locator('#datasets-status').inner_text()
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                await dialog.get_by_role('button', name='Reload saved version', exact=True).click()
                await ready(page)
                assert '2026-12' in await page.locator('#dataset-saved-version').inner_text()
                assert 'Open source' in await page.locator('#dataset-saved-version').inner_text()
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
                assert await dialog.get_by_label('Labels', exact=True).input_value() == 'needs review, Open source'
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
                    'labels': ['<img src=x onerror=alert(1)>'],
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


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('intervening_change', ['update', 'delete'])
def test_replacing_dataset_draft_requires_inspecting_intervening_changes(engine, intervening_change):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                original = httpx.post(url + '/api/datasets', json={'name': 'Corpus', 'version': 'Original'}).json()
                endpoint = url + '/api/datasets/' + original['id']
                await page.goto(url + '/?dataset=' + original['id'])
                dialog = page.locator('#datasets-sheet')
                await dialog.wait_for(state='visible')
                await ready(page)
                await dialog.get_by_label('Version', exact=True).fill('My draft')
                inspected = httpx.put(endpoint, json={
                    'name': 'Corpus', 'version': 'Inspected version', 'revision': original['revision']}).json()
                await dialog.get_by_role('button', name='Reload saved version', exact=True).click()
                await ready(page)
                assert 'Inspected version' in await page.locator('#dataset-saved-version').inner_text()
                if intervening_change == 'update':
                    httpx.put(endpoint, json={
                        'name': 'Corpus', 'version': 'Newer version', 'revision': inspected['revision']}).raise_for_status()
                    expected = 'Newer version'
                else:
                    httpx.delete(endpoint, params={'revision': inspected['revision']}).raise_for_status()
                    expected = 'This dataset record has been deleted.'
                await dialog.get_by_role('button', name='Replace draft with saved version', exact=True).click()
                await ready(page)
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'My draft'
                assert 'saved version changed again' in await page.locator('#datasets-status').inner_text()
                assert expected in await page.locator('#dataset-saved-version').inner_text()
                # The refreshed comparison still needs explicit approval to discard writing.
                await dialog.get_by_role('button', name='Replace draft with saved version', exact=True).click()
                await _answer(page, 'Cancel')
                await ready(page)
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'My draft'
                await dialog.get_by_role('button', name='Replace draft with saved version', exact=True).click()
                await _answer(page, 'Discard changes')
                await ready(page)
                if intervening_change == 'update':
                    assert await dialog.get_by_label('Version', exact=True).input_value() == expected
                else:
                    assert await page.locator('#dataset-form').count() == 0
                    assert expected in await page.locator('#datasets-status').inner_text()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_datasets_view_keeps_the_library_usable_and_the_draft_across_navigation(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Evaluation study'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Corpus', 'version': 'Saved'}).json()
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                assert 'view=datasets' in page.url and f'dataset={item["id"]}' in page.url
                assert await page.locator('#btn-datasets').get_attribute('aria-current') == 'page'
                assert not await page.locator('#panes').is_visible()
                # The rest of the app stays live beside the catalog.
                assert await page.locator('#side').is_visible()
                assert await page.locator('dialog[open]').count() == 0
                await view.get_by_label('Version', exact=True).fill('My unsaved version')
                await page.locator('#papers').get_by_text('Evaluation study').click()
                await view.wait_for(state='hidden')
                assert 'paper=study' in page.url
                assert await page.locator('#btn-datasets').get_attribute('aria-current') is None
                await page.go_back()
                await view.wait_for(state='visible')
                await ready(page)
                assert await view.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                assert 'Unsaved changes' in await page.locator('#datasets-status').inner_text()
                assert httpx.get(url + '/api/datasets').json()['datasets'][0]['version'] == 'Saved'
                # Keys typed in the catalog stay with it.
                await view.get_by_role('button', name='Save dataset', exact=True).focus()
                await page.keyboard.press('Escape')
                assert await view.is_visible()
                await view.get_by_role('button', name='Save dataset', exact=True).click()
                await ready(page)
                assert httpx.get(url + '/api/datasets').json()['datasets'][0]['version'] == 'My unsaved version'
                if engine == 'chromium':
                    screenshot = Path(__file__).parents[1] / '.context' / 'datasets-view.png'
                    screenshot.parent.mkdir(exist_ok=True)
                    await page.screenshot(path=str(screenshot))
                assert not errors

    run_browser(scenario())
