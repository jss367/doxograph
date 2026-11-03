"""Exercise dataset editing and linked evidence in Chromium and native WebKit."""
import asyncio
from pathlib import Path

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _answer, _server


async def ready(page):
    await page.wait_for_function("document.querySelector('#datasets-sheet').getAttribute('aria-busy') !== 'true'")


async def settled(page):
    await page.wait_for_function("document.querySelector('#datasets-status').textContent === 'Saved'")


async def refuse_saves(page):
    """Fail every dataset write, so a draft stays unsaved and leaving it asks."""
    async def refuse(route):
        if route.request.method in ('POST', 'PUT'):
            await route.fulfill(status=503, json={'detail': 'Saving is unavailable.'})
        else:
            await route.continue_()
    await page.route('**/api/datasets', refuse)
    await page.route('**/api/datasets/*', refuse)


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
                await settled(page)
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
                assert 'Loupe is ready' in await page.locator('#datasets-status').inner_text()
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
@pytest.mark.parametrize('pending', ['load', 'save', 'failed_save'])
def test_dataset_navigation_uses_the_latest_target_while_busy(engine, pending):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                first, second, third = [httpx.post(url + '/api/datasets', json={'name': name}).json()
                                        for name in ['First corpus', 'Second corpus', 'Third corpus']]
                held, release = asyncio.Event(), asyncio.Event()

                async def hold_request(route):
                    held.set()
                    await release.wait()
                    if pending == 'failed_save':
                        await route.fulfill(status=409, json={'detail': 'The dataset changed in another window.'})
                    else:
                        await route.continue_()

                if pending == 'load':
                    await page.route('**/api/datasets', hold_request)
                await page.goto(url + '/#view=datasets&dataset=' + first['id'])
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                if pending != 'load':
                    await ready(page)
                    await page.route('**/api/datasets/' + first['id'], hold_request)
                    await view.get_by_label('Version', exact=True).fill('My changed version')
                await asyncio.wait_for(held.wait(), timeout=5)
                # New hash targets and Back must win over the older pending work.
                for item in [second, third]:
                    await page.evaluate('id => { location.hash = "view=datasets&dataset=" + id; }', item['id'])
                    await page.wait_for_function('id => V.dataset === id', arg=item['id'])
                await page.go_back()
                await page.wait_for_function('id => V.dataset === id', arg=second['id'])
                release.set()
                if pending == 'failed_save':
                    await _answer(page, 'Cancel')
                    await ready(page)
                    assert await view.get_by_label('Dataset name', exact=True).input_value() == 'First corpus'
                    assert await view.get_by_label('Version', exact=True).input_value() == 'My changed version'
                    assert f'dataset={first["id"]}' in page.url
                else:
                    await page.wait_for_function("document.querySelector('#dataset-name')?.value === 'Second corpus'", timeout=5000)
                    await ready(page)
                    assert f'dataset={second["id"]}' in page.url
                    if pending == 'save':
                        saved = httpx.get(url + '/api/datasets').json()['datasets']
                        assert next(item for item in saved if item['id'] == first['id'])['version'] == 'My changed version'

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('answer', ['Cancel', 'Discard changes'])
def test_dataset_navigation_ignores_a_superseded_discard_prompt(engine, answer):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                first, second, third = [httpx.post(url + '/api/datasets', json={'name': name}).json()
                                        for name in ['First corpus', 'Second corpus', 'Third corpus']]
                await page.goto(url + '/#view=datasets&dataset=' + first['id'])
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                await refuse_saves(page)
                await view.get_by_label('Version', exact=True).fill('My unsaved version')
                await page.evaluate('id => { location.hash = "view=datasets&dataset=" + id; }', second['id'])
                await page.locator('#ask').wait_for(state='visible')
                # History cancels the old prompt; only the newest target may proceed.
                await page.evaluate('id => { location.hash = "view=datasets&dataset=" + id; }', third['id'])
                await page.wait_for_function('id => V.dataset === id', arg=third['id'])
                await _answer(page, answer)
                await ready(page)
                expected = first if answer == 'Cancel' else third
                assert f'dataset={expected["id"]}' in page.url
                assert await view.get_by_label('Dataset name', exact=True).input_value() == expected['name']
                assert await view.get_by_label('Version', exact=True).input_value() == ('My unsaved version' if answer == 'Cancel' else '')

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('target', ['paper', 'claim'])
def test_following_dataset_evidence_preserves_history_and_draft(engine, target):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Evaluation study'))
    claim = store.add_claim('study', {'text': 'The linked finding'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Corpus', 'papers': ['study'],
                    'claims': [{'paper': 'study', 'claim': claim['id']}]}).json()
                await page.goto(url + '/#view=datasets&dataset=' + item['id'])
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                await view.get_by_label('Version', exact=True).fill('My unsaved version')
                await page.locator('#dataset-research').get_by_role('button',
                    name='Evaluation study' if target == 'paper' else claim['text'], exact=True).click()
                await view.wait_for(state='hidden')
                assert 'paper=study' in page.url
                if target == 'claim':
                    assert f'sel={claim["id"]}' in page.url
                await page.go_back()
                await view.wait_for(state='visible', timeout=5000)
                await ready(page)
                assert f'dataset={item["id"]}' in page.url
                assert await view.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                assert httpx.get(url + '/api/datasets').json()['datasets'][0]['version'] == 'My unsaved version'

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('failed_refresh', [False, True])
def test_workspace_switch_hides_dataset_before_destination_refresh(engine, failed_refresh):
    config.set_ai_enabled(False)
    other = config.create_workspace('Another project')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Original workspace corpus'}).json()
                await page.goto(url + '/#view=datasets&dataset=' + item['id'])
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                held, release = asyncio.Event(), asyncio.Event()

                async def hold_destination(route):
                    if route.request.headers.get('x-doxograph-workspace') == other['id']:
                        held.set()
                        await release.wait()
                        if failed_refresh:
                            await route.fulfill(status=503, json={'detail': 'Destination is unavailable.'})
                            return
                    await route.continue_()

                await page.route('**/api/state', hold_destination)
                await page.evaluate('''id => {
                    void switchWorkspace(id).catch(error => { window.datasetSwitchError = error.message; });
                }''', other['id'])
                await asyncio.wait_for(held.wait(), timeout=5)
                try:
                    assert await page.locator('#workspace').input_value() == other['id']
                    assert not await view.is_visible()
                    assert not await page.locator('body').evaluate("node => node.classList.contains('page-view')")
                finally:
                    release.set()
                await page.wait_for_function('workspaceController.pending === null')
                if failed_refresh:
                    assert await page.evaluate('window.datasetSwitchError') == 'Destination is unavailable.'
                    assert not await view.is_visible()
                    assert await page.locator('#sync-status').is_visible()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('new_dataset', [False, True])
def test_dataset_draft_survives_a_later_workspace_guard_refusal(engine, new_dataset):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Evaluation study'))
    other = config.create_workspace('Another project')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Corpus'}).json()
                await page.goto(url + '/#view=datasets&dataset=' + item['id'])
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                await refuse_saves(page)
                if new_dataset:
                    await view.get_by_role('button', name='Add dataset', exact=True).click()
                    await ready(page)
                    await view.get_by_label('Dataset name', exact=True).fill('New corpus')
                await view.get_by_label('Version', exact=True).fill('My unsaved version')
                await page.locator('#papers').get_by_text('Evaluation study', exact=True).click()
                await view.wait_for(state='hidden')
                # Simulate the later research guard vetoing an otherwise accepted leave.
                await page.evaluate('''() => {
                    window.originalResearchGuard = ResearchTools.beforeWorkspaceSwitch;
                    ResearchTools.beforeWorkspaceSwitch = async () => false;
                }''')
                await page.evaluate('id => { void switchWorkspace(id); }', other['id'])
                await _answer(page, 'Discard changes')
                assert await page.locator('#workspace').input_value() == 'default'
                assert await page.evaluate('Datasets.hasUnsavedChanges()')
                await page.locator('#btn-datasets').click()
                await ready(page)
                assert await view.get_by_label('Dataset name', exact=True).input_value() == ('New corpus' if new_dataset else 'Corpus')
                assert await view.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                # Once every guard agrees, committed selection retires the old draft.
                await page.evaluate('ResearchTools.beforeWorkspaceSwitch = window.originalResearchGuard')
                await page.evaluate('id => { void switchWorkspace(id); }', other['id'])
                await _answer(page, 'Discard changes')
                await page.wait_for_function('id => workspaceClient.workspaceId === id && !workspaceController.pending', arg=other['id'])
                assert not await view.is_visible()
                assert not await page.evaluate('Datasets.hasUnsavedChanges()')
                await page.locator('#btn-datasets').click()
                await ready(page)
                assert await page.locator('#datasets-list button').count() == 0

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
                await settled(page)
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
                await page.wait_for_function("document.querySelector('#datasets-status').textContent.includes('changed in another window')")
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
                # Switching workspaces saves the draft before leaving it.
                await dialog.get_by_label('Dataset name', exact=True).fill('My draft')
                await page.evaluate("void switchWorkspace('" + other['id'] + "')")
                await dialog.wait_for(state='hidden')
                await page.wait_for_function('id => workspaceClient.workspaceId === id', arg=other['id'])
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                assert await page.locator('#datasets-list button').count() == 0
                await page.locator('#workspace').select_option('default')
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                assert await dialog.get_by_label('Dataset name', exact=True).input_value() == 'My draft'
                await page.locator('#datasets-search').fill('not present')
                assert 'No datasets match your search.' in await page.locator('#datasets-list').inner_text()
                await page.locator('#datasets-search').fill('')
                # Missing links remain removable without deleting the record's other metadata.
                await dialog.get_by_text('Link papers and claims', exact=True).click()
                await page.locator('#dataset-link-options').get_by_label('Paper Missing paper: study', exact=True).uncheck()
                await page.locator('#dataset-link-options').get_by_label('Claim Missing claim: ' + claim['id'], exact=True).uncheck()
                await settled(page)
                [kept] = httpx.get(url + '/api/datasets').json()['datasets']
                assert kept['papers'] == [] and kept['claims'] == [] and kept['labels'] == item['labels']
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
                second = httpx.post(url + '/api/datasets', json={'name': 'Second corpus'}).json()
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
                assert await page.locator('#datasets-status').inner_text() == 'Saved'
                # Leaving the catalog saved the draft without waiting for a pause.
                saved = {d['id']: d for d in httpx.get(url + '/api/datasets').json()['datasets']}
                assert saved[item['id']]['version'] == 'My unsaved version'
                # Opening the map also leaves the catalog and keeps its draft.
                await page.locator('#graph-nav [data-view="graph"]').click()
                await view.wait_for(state='hidden')
                assert 'view=graph' in page.url
                await page.go_back()
                await view.wait_for(state='visible')
                await ready(page)
                assert await view.get_by_label('Version', exact=True).input_value() == 'My unsaved version'
                # Keys typed in the catalog stay with it.
                await view.get_by_role('button', name='Reload saved version', exact=True).focus()
                await page.keyboard.press('Escape')
                assert await view.is_visible()
                # Picking a record moves the URL; a redraw that follows leaves the editor alone.
                await page.locator('#datasets-list').get_by_role('button', name='Second corpus').click()
                await ready(page)
                assert f'dataset={second["id"]}' in page.url
                await view.get_by_label('Description', exact=True).focus()
                await page.evaluate('render(); new Promise(resolve => setTimeout(resolve, 100))')
                await ready(page)
                assert await page.evaluate('document.activeElement.id') == 'dataset-description'
                if engine == 'chromium':
                    screenshot = Path(__file__).parents[1] / '.context' / 'datasets-view.png'
                    screenshot.parent.mkdir(exist_ok=True)
                    await page.screenshot(path=str(screenshot))
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_retried_create_keeps_edits_made_after_its_answer_was_lost(engine):
    """The server answers a repeated create with the record it stored first,
    so taking that answer as saved would drop what was typed since."""
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            creates = []

            async def lose_first_answer(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                creates.append(route.request.post_data_json)
                if len(creates) == 1:
                    # The create is stored, but its answer never arrives.
                    await route.fetch()
                    await route.fulfill(status=503, json={'detail': 'The connection dropped.'})
                else:
                    await route.continue_()

            await page.route('**/api/datasets', lose_first_answer)
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Dataset name', exact=True).fill('Evaluation corpus')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.includes('connection dropped')")
                [stored] = httpx.get(url + '/api/datasets').json()['datasets']
                assert stored['description'] == ''

                await dialog.get_by_label('Description', exact=True).fill('Written after the answer was lost.')
                await settled(page)
                assert len(creates) == 2
                [saved] = httpx.get(url + '/api/datasets').json()['datasets']
                assert saved['id'] == stored['id']
                assert saved['description'] == 'Written after the answer was lost.'
                field = dialog.get_by_label('Description', exact=True)
                assert await field.input_value() == 'Written after the answer was lost.'
                # Once saved, the record's actions replace the new draft's.
                await dialog.get_by_role('button', name='Delete record', exact=True).wait_for()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('edited', [True, False])
def test_a_retried_create_keeps_changes_another_window_made_after_its_answer_was_lost(engine, edited):
    """Another window can change the record while this one has not heard back
    from its create. The retry finds that newer record, and saving this draft
    over it would erase the other window's change, edited draft or not."""
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            creates, updates = [], []

            async def lose_first_answer(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                creates.append(route.request.post_data_json)
                if len(creates) == 1:
                    await route.fetch()
                    await route.fulfill(status=503, json={'detail': 'The connection dropped.'})
                else:
                    await route.continue_()

            async def count_updates(route):
                if route.request.method == 'PUT':
                    updates.append(route.request.post_data_json)
                await route.continue_()

            await page.route('**/api/datasets', lose_first_answer)
            await page.route('**/api/datasets/*', count_updates)
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Dataset name', exact=True).fill('Evaluation corpus')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.includes('connection dropped')")
                [stored] = httpx.get(url + '/api/datasets').json()['datasets']
                fields = {key: value for key, value in stored.items()
                          if key not in ('id', 'revision', 'created', 'updated')}
                httpx.put(url + '/api/datasets/' + stored['id'], json={
                    **fields, 'version': 'From another window', 'revision': stored['revision']}).raise_for_status()

                description = dialog.get_by_label('Description', exact=True)
                if edited:
                    await description.fill('Written after the answer was lost.')
                else:
                    await description.press('Control+s')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.includes('changed in another window')")
                assert len(creates) == 2
                assert updates == []
                [current] = httpx.get(url + '/api/datasets').json()['datasets']
                assert current['version'] == 'From another window'
                assert current['description'] == ''
                assert await description.input_value() == ('Written after the answer was lost.' if edited else '')

                # Saving again still holds the draft back until the writer reloads.
                await description.fill('Typed after the conflict.')
                await description.press('Control+s')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.includes('changed in another window')")
                assert updates == []
                assert httpx.get(url + '/api/datasets').json()['datasets'][0]['description'] == ''

                await dialog.get_by_role('button', name='Reload saved version', exact=True).click()
                await ready(page)
                await dialog.get_by_role('button', name='Replace draft with saved version', exact=True).click()
                await _answer(page, 'Discard changes')
                await settled(page)
                assert await dialog.get_by_label('Version', exact=True).input_value() == 'From another window'
                assert await description.input_value() == ''
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('create', ['answered', 'lost'])
def test_discarding_a_new_dataset_deletes_the_record_its_create_stored(engine, create):
    """Discard waits for a create under way, which can store the record before
    the prompt is answered, or store it and lose the answer. Confirming the
    discard must not leave that record behind."""
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            held, release = asyncio.Event(), asyncio.Event()

            async def hold_create(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                held.set()
                await release.wait()
                if create == 'lost':
                    await route.fetch()
                    await route.fulfill(status=503, json={'detail': 'The connection dropped.'})
                else:
                    await route.continue_()

            await page.route('**/api/datasets', hold_create)
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Dataset name', exact=True).fill('Evaluation corpus')
                await asyncio.wait_for(held.wait(), timeout=5)
                await dialog.get_by_role('button', name='Discard new dataset', exact=True).click()
                release.set()
                await _answer(page, 'Discard dataset')
                await ready(page)
                assert httpx.get(url + '/api/datasets').json()['datasets'] == []
                assert await page.locator('#datasets-count').text_content() == '0 datasets'
                assert await dialog.get_by_label('Dataset name', exact=True).count() == 0
                assert 'dataset=' not in page.url
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('then', ['retry', 'edit', 'discard'])
def test_a_lost_create_recognizes_the_record_in_its_stored_form(engine, then):
    """The server sorts and merges labels, trims the name, and drops repeated
    facet names. A record that differs from the draft only in that way is
    still the one this draft created, not another window's change."""
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            creates, updates = [], []

            async def lose_first_answer(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                creates.append(route.request.post_data_json)
                if len(creates) == 1:
                    await route.fetch()
                    await route.fulfill(status=503, json={'detail': 'The connection dropped.'})
                else:
                    await route.continue_()

            async def count_updates(route):
                if route.request.method == 'PUT':
                    updates.append(route.request.post_data_json)
                await route.continue_()

            await page.route('**/api/datasets', lose_first_answer)
            await page.route('**/api/datasets/*', count_updates)
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                await ready(page)
                dialog = page.locator('#datasets-sheet')
                await dialog.get_by_role('button', name='Add dataset', exact=True).click()
                await ready(page)
                await dialog.get_by_label('Labels', exact=True).fill('zeta, alpha, Alpha')
                await dialog.get_by_label('Other dataset names in findings', exact=True).fill('Eval set\nEval set')
                await dialog.get_by_label('Dataset name', exact=True).fill(' Evaluation corpus ')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.includes('connection dropped')")
                [stored] = httpx.get(url + '/api/datasets').json()['datasets']
                assert stored['name'] == 'Evaluation corpus'
                assert stored['labels'] == ['alpha', 'zeta']
                assert stored['facet_values'] == ['Eval set']

                if then == 'discard':
                    await dialog.get_by_role('button', name='Discard new dataset', exact=True).click()
                    await _answer(page, 'Discard dataset')
                    await ready(page)
                    assert httpx.get(url + '/api/datasets').json()['datasets'] == []
                    assert not errors
                    return
                description = dialog.get_by_label('Description', exact=True)
                if then == 'edit':
                    await description.fill('Written after the answer was lost.')
                else:
                    await description.press('Control+s')
                await settled(page)
                assert len(creates) == 2
                assert len(updates) == (1 if then == 'edit' else 0)
                [saved] = httpx.get(url + '/api/datasets').json()['datasets']
                assert saved['id'] == stored['id']
                assert saved['labels'] == ['alpha', 'zeta']
                assert saved['description'] == ('Written after the answer was lost.' if then == 'edit' else '')
                assert await dialog.get_by_label('Labels', exact=True).input_value() == 'alpha, zeta'
                await dialog.get_by_role('button', name='Delete record', exact=True).wait_for()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_save_whose_answer_was_lost_is_not_saved_by_restoring_the_old_text(engine):
    """The lost save may have stored its edit, so typing the old text back must
    not count as saved while the server holds a change the writer took back."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Evaluation study'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            updates = []

            async def lose_first_answer(route):
                if route.request.method != 'PUT':
                    await route.continue_()
                    return
                updates.append(route.request.post_data_json)
                if len(updates) == 1:
                    # The edit is stored, but its answer never arrives.
                    await route.fetch()
                    await route.abort()
                else:
                    await route.continue_()

            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Corpus', 'version': 'Saved'}).json()
                await page.route('**/api/datasets/*', lose_first_answer)
                await page.goto(url)
                await page.get_by_role('button', name='Datasets', exact=True).click()
                view = page.locator('#datasets-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                field = view.get_by_label('Version', exact=True)
                await field.fill('Transient')
                await page.wait_for_function(
                    "document.querySelector('#datasets-status').textContent.startsWith('Couldn’t save')")
                [stored] = httpx.get(url + '/api/datasets').json()['datasets']
                assert stored['version'] == 'Transient'

                await field.fill('Saved')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                assert [update['version'] for update in updates][:2] == ['Transient', 'Saved']
                # The lost save moved the record on, so the retry is a conflict.
                [stored] = httpx.get(url + '/api/datasets').json()['datasets']
                assert stored['version'] == 'Transient'
                assert await page.locator('#datasets-status').inner_text() != 'Saved'
                assert await page.evaluate('Datasets.hasUnsavedChanges()') is True
                # Leaving the catalog and coming back keeps the draft.
                await page.locator('#papers').get_by_text('Evaluation study').click()
                await view.wait_for(state='hidden')
                await page.go_back()
                await view.wait_for(state='visible')
                await ready(page)
                assert f'dataset={item["id"]}' in page.url
                assert await field.input_value() == 'Saved'
                assert not errors

    run_browser(scenario())
