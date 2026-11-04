"""Topic creation refreshes only confirmed rows and owns its submitted input."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


@pytest.fixture(autouse=True)
def topic_catalog():
    config.set_ai_enabled(False)
    for key in ['a', 'b']:
        store.save_paper(store.new_paper(key, title='Synthetic ' + key))
        store.add_claim(key, {'text': 'Synthetic claim ' + key})


@pytest.mark.browser
@pytest.mark.parametrize('move', ['stay', 'edit', 'leave', 'return', 'failure'])
def test_add_topic_completion_preserves_current_writing(move):
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def add(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                requests.append((route.request.headers['x-doxograph-workspace'], route.request.post_data_json))
                started.set()
                await release.wait()
                if move == 'failure':
                    await route.fulfill(status=503, json={'detail': 'Synthetic refusal'})
                else:
                    response = await route.fetch()
                    assert response.status == 200
                    await route.fulfill(response=response)
                finished.set()

            await page.route('**/api/tags', add)
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other'}).json()['workspace']['id']
                await page.goto(url)
                await page.locator('#btn-workspace').click()
                context = page.locator('#research-context')
                await context.fill('Preserve my context draft')
                await page.locator('#new-tag').fill('Synthetic added topic')
                await page.locator('#btn-tag').click()
                await asyncio.wait_for(started.wait(), 5)
                if move in ['leave', 'return']:
                    await page.locator('#btn-papers').click()
                    if move == 'return':
                        await page.locator('#btn-workspace').click()
                if move in ['edit', 'return']:
                    await page.locator('#new-tag').fill('Newer unsent topic')
                    await page.locator('#new-tag').focus()
                elif move != 'leave':
                    await context.focus()
                    await context.evaluate('field => field.setSelectionRange(3, 8)')
                await page.evaluate("window.topicFocus=document.activeElement;window.topicField=document.getElementById('new-tag');window.contextField=document.getElementById('research-context')")
                release.set()
                await asyncio.wait_for(finished.wait(), 5)
                await page.wait_for_function('workspaceClient.pendingMutations === 0')
                await page.evaluate('() => new Promise(requestAnimationFrame)')
                assert not errors, errors
                if move != 'failure':
                    await page.wait_for_function("S.tags.some(tag => tag.name === 'synthetic-added-topic')", timeout=5000)
                assert requests == [('default', {'name': 'Synthetic added topic', 'description': ''})]
                state = httpx.get(url + '/api/state').json()
                assert any(tag['name'] == 'synthetic-added-topic' for tag in state['tags']) is (move != 'failure')
                other_state = httpx.get(url + '/api/state', headers={'X-Doxograph-Workspace': other}).json()
                assert not any(tag['name'] == 'synthetic-added-topic' for tag in other_state['tags'])
                assert not errors, errors
                if move != 'leave':
                    assert await page.evaluate('document.activeElement === window.topicFocus')
                    assert await page.evaluate("document.getElementById('new-tag') === window.topicField && document.getElementById('research-context') === window.contextField")
                    await expect(context).to_have_value('Preserve my context draft')
                    expected_name = 'Newer unsent topic' if move in ['edit', 'return'] else 'Synthetic added topic' if move == 'failure' else ''
                    await expect(page.locator('#new-tag')).to_have_value(expected_name)
                    await expect(page.locator('[data-open-topic="synthetic-added-topic"]')).to_have_count(0 if move == 'failure' else 1)
                    if move in ['stay', 'failure']:
                        assert await context.evaluate('field => [field.selectionStart, field.selectionEnd]') == [3, 8]
    run_browser(scenario())


@pytest.mark.browser
def test_connection_counts_are_informational_and_keep_the_saved_data():
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                refs = [{'paper': key, 'claim': store.load_paper(key)['claims'][0]['id']} for key in ['a', 'b']]
                connection = {'id': 'synthetic-link', 'source': refs[0], 'target': refs[1],
                    'relation': 'qualifies', 'note': 'Synthetic annotation'}
                notebook['connections'] = [connection]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.add_init_script("localStorage.setItem('doxograph-claims-drawer','open')")
                await page.goto(url + '/#paper=a')
                await expect(page.locator('.claim-selection')).to_contain_text('1 manual connection')
                await expect(page.locator('[data-tool="connections"]')).to_have_count(0)
                assert await page.evaluate('V.view') == 'claims'
                assert httpx.get(url + '/api/notebook').json()['connections'] == [connection]
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('phase', ['write-pending', 'refresh-pending'])
def test_topic_completion_respects_workspace_guards_and_late_reads(phase):
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            write_started, release_write = asyncio.Event(), asyncio.Event()
            read_started, release_read = asyncio.Event(), asyncio.Event()
            hold_read = False

            async def add(route):
                if route.request.method != 'POST':
                    await route.continue_()
                    return
                write_started.set()
                await release_write.wait()
                response = await route.fetch()
                await route.fulfill(response=response)

            async def state_read(route):
                nonlocal hold_read
                if hold_read:
                    hold_read = False
                    read_started.set()
                    await release_read.wait()
                    response = await route.fetch()
                    await route.fulfill(response=response)
                else:
                    await route.continue_()

            await page.route('**/api/tags', add)
            await page.route('**/api/state', state_read)
            with _server() as url:
                other = httpx.post(url + '/api/workspaces', json={'name': 'Other'}).json()['workspace']['id']
                await page.goto(url)
                await page.locator('#btn-workspace').click()
                await page.locator('#new-tag').fill('Synthetic added topic')
                await page.locator('#btn-tag').click()
                await asyncio.wait_for(write_started.wait(), 5)
                if phase == 'write-pending':
                    await page.locator('#workspace').select_option(other)
                    await page.wait_for_function("document.querySelector('#workspace').value === 'default' && !workspaceController.pending")
                    assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                    await page.locator('#new-tag').fill('Newer unsent topic')
                    await page.locator('#new-tag').focus()
                    release_write.set()
                    await expect(page.locator('[data-open-topic="synthetic-added-topic"]')).to_have_count(1)
                else:
                    hold_read = True
                    release_write.set()
                    await asyncio.wait_for(read_started.wait(), 5)
                    await page.locator('#workspace').select_option(other)
                    await page.wait_for_function('id => workspaceClient.workspaceId === id && !workspaceController.pending', arg=other)
                    await page.locator('#btn-workspace').click()
                    await page.locator('#research-context').fill('Other workspace draft')
                    await page.locator('#new-tag').fill('Newer unsent topic')
                    await page.locator('#new-tag').focus()
                    release_read.set()
                    await page.wait_for_function('workspaceClient.pullsInFlight === 0')
                    await expect(page.locator('#research-context')).to_have_value('Other workspace draft')
                    await expect(page.locator('[data-open-topic="synthetic-added-topic"]')).to_have_count(0)
                await expect(page.locator('#new-tag')).to_have_value('Newer unsent topic')
                await expect(page.locator('#new-tag')).to_be_focused()
                await page.evaluate('() => new Promise(requestAnimationFrame)')
                assert not errors, errors
                original = httpx.get(url + '/api/state').json()
                assert any(tag['name'] == 'synthetic-added-topic' for tag in original['tags'])
                current = httpx.get(url + '/api/state', headers={'X-Doxograph-Workspace': other}).json()
                assert not any(tag['name'] == 'synthetic-added-topic' for tag in current['tags'])
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('research_changed', [False, True])
def test_unsent_topic_survives_workspace_redraw_and_is_part_of_quit_consent(research_changed):
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#btn-workspace').click()
                context = page.locator('#research-context')
                before = await context.input_value()
                if research_changed:
                    await context.fill('Keep this research writing')
                await page.locator('#new-tag').fill('Unsubmitted <topic>')
                await page.evaluate('renderAll()')
                await expect(page.locator('#new-tag')).to_have_value('Unsubmitted <topic>')
                await expect(context).to_have_value('Keep this research writing' if research_changed else before)
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                first_snapshot = await page.evaluate('window.doxographQuitWritingSnapshot()')
                await page.locator('#new-tag').fill('A different unsent topic')
                assert await page.evaluate('window.doxographQuitWritingSnapshot()') != first_snapshot
                await page.locator('#btn-papers').click()
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                await page.locator('#btn-workspace').click()
                await expect(page.locator('#new-tag')).to_have_value('A different unsent topic')
                await expect(context).to_have_value('Keep this research writing' if research_changed else before)
                await page.locator('#new-tag').fill('')
                assert await page.evaluate('window.doxographSaveDrafts()') is (not research_changed)
                assert not httpx.get(url + '/api/state').json()['tags']
    run_browser(scenario())


@pytest.mark.browser
def test_palette_opens_reading_queue_without_a_surviving_paper():
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['reading'] = [{'paper': 'a', 'status': 'queued', 'page': 1, 'bookmarks': []}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                assert httpx.delete(url + '/api/papers/a').status_code == 200
                assert httpx.delete(url + '/api/papers/b').status_code == 200
                await page.goto(url)
                await page.wait_for_function('libraryInitialized')
                await page.locator('#btn-command-palette').click()
                await page.locator('#command-query').fill('Open reading queue')
                await expect(page.locator('#command-results').get_by_text('Open reading queue', exact=True)).to_have_count(1)
                await page.locator('#command-results').get_by_text('Open reading queue', exact=True).click()
                entry = page.locator('.reading-entry[data-reading="a"]')
                await expect(entry).to_contain_text('Removed paper: a')
                await entry.locator('[data-tool="reading-remove"]').click()
                await expect(entry).to_have_count(0)
                assert httpx.get(url + '/api/notebook').json()['reading'] == []
    run_browser(scenario())
