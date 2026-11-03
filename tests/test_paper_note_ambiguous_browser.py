"""Paper-note recovery must not mistake the old local value for an acknowledgement."""
import asyncio
import json

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models, open_note, seed


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('failure', ['definite', 'ambiguous'])
@pytest.mark.parametrize('leaving', ['done', 'native'])
def test_failed_write_keeps_reverted_note_and_retry_compensates(engine, failure, leaving):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            requests = []

            async def first_write_fails(route):
                if route.request.method != 'PATCH':
                    await route.continue_()
                    return
                requests.append(route.request.post_data_json)
                if len(requests) > 1:
                    await route.continue_()
                    return
                started.set()
                await release.wait()
                if failure == 'ambiguous':
                    await route.fetch()  # Commit the write, then lose only its response.
                    await route.abort()
                else:
                    await route.fulfill(status=503, json={'detail': 'Synthetic save refused'})

            with _server() as url:
                await open_note(page, url)
                await page.route('**/api/papers/paper', first_write_fails)
                field = page.locator('textarea[data-note="paper"]')
                await field.fill('Transient saved text')
                await page.evaluate('void noteAutosave("paper").flush()')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await field.fill('Stored note')
                    # Hold the debounce while the leave request waits for the same write.
                    await page.evaluate('''leaving => {
                      void noteAutosave('paper').settle();
                      window.noteLeaveResult = null;
                      void (leaving === 'done' ? closeNoteEditor() : window.doxographSaveDrafts())
                        .then(value => { window.noteLeaveResult = value === undefined ? 'done' : value; });
                    }''', leaving)
                    release.set()
                    await page.wait_for_function('!paperNotesSaving() && window.noteLeaveResult !== null')
                    state = await page.evaluate('''() => ({draft: V.noteDrafts.paper ?? null,
                      editing: V.noteEditing, leave: window.noteLeaveResult,
                      error: noteAutosave('paper').failure})''')
                    state['server'] = store.load_paper('paper')['notes']
                    print('POST_FAILURE_STATE', json.dumps(state, sort_keys=True))
                    assert state['draft'] == 'Stored note'
                    assert state['error']
                    assert len(requests) == 1  # Waiting for a failed write is not a retry.
                    if leaving == 'native':
                        assert state['leave'] is False
                    await expect(field).to_have_value('Stored note')
                    await expect(field).to_be_visible()
                    assert state['server'] == ('Transient saved text' if failure == 'ambiguous' else 'Stored note')

                    # Explicit retry must save the retained intent even though it equals
                    # the stale local snapshot from before the ambiguous first write.
                    assert await page.evaluate('noteAutosave("paper").flush()') is True
                    assert store.load_paper('paper')['notes'] == 'Stored note'
                    assert [r['notes'] for r in requests] == ['Transient saved text', 'Stored note']
                    assert await page.evaluate('Object.keys(V.noteDrafts)') == []
                    assert await page.evaluate('noteAutosave("paper").failure') == ''
                    await page.locator('[data-act="done-note"][data-paper="paper"]').click()
                    await expect(field).to_have_count(0)
                finally:
                    release.set()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_uncertain_note_survives_navigation_workspace_cancel_and_native_retry(engine):
    seed()
    other_workspace = config.create_workspace('Synthetic second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            requests = []
            allow_recovery = False

            async def fail_until_recovery(route):
                if route.request.method != 'PATCH':
                    await route.continue_()
                    return
                requests.append(route.request.post_data_json)
                if len(requests) == 1:
                    started.set()
                    await release.wait()
                    await route.fetch()
                    await route.abort()
                elif allow_recovery:
                    await route.continue_()
                else:
                    await route.fulfill(status=503, json={'detail': 'Still offline'})

            with _server() as url:
                await open_note(page, url)
                await page.route('**/api/papers/paper', fail_until_recovery)
                field = page.locator('textarea[data-note="paper"]')
                await field.fill('Transient saved text')
                await page.evaluate('void noteAutosave("paper").flush()')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await field.fill('Stored note')
                    await page.evaluate('void noteAutosave("paper").settle()')
                    await page.locator('#papers [data-paper="other"]').click()
                    release.set()
                    await page.wait_for_function('!paperNotesSaving()')
                    assert await page.evaluate('V.noteDrafts.paper') == 'Stored note'
                    assert store.load_paper('paper')['notes'] == 'Transient saved text'
                    await page.locator('#papers [data-paper="paper"]').click()
                    await expect(field).to_have_value('Stored note')
                    await page.evaluate('''id => { window.noteSwitchFinished = false;
                      void switchWorkspace(id).then(() => { window.noteSwitchFinished = true; });
                    }''', other_workspace)
                    await expect(page.locator('#ask')).to_be_visible()
                    await page.locator('#ask').get_by_role('button', name='Cancel', exact=True).click()
                    await page.wait_for_function('window.noteSwitchFinished')
                    assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                    assert await page.evaluate('V.noteDrafts.paper') == 'Stored note'
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    assert await page.evaluate('V.noteDrafts.paper') == 'Stored note'
                    assert store.load_paper('paper')['notes'] == 'Transient saved text'
                    allow_recovery = True
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                    assert store.load_paper('paper')['notes'] == 'Stored note'
                    assert await page.evaluate('Object.keys(V.noteDrafts)') == []
                    assert [r['notes'] for r in requests] == ['Transient saved text'] + ['Stored note'] * 3
                finally:
                    release.set()

    run_browser(scenario())
