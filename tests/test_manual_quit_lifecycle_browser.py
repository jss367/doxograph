"""Autosave quit baselines follow focused edits, partial writes and confirmed deletes."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_browser import _answer
from test_core_workspace_boundary_browser import editor, seed
from test_quit_manual_editors_browser import save, stored
from browser_support import inert_tools as inert_tools


async def refuse_patch(route):
    if route.request.method == 'PATCH':
        await route.fulfill(status=503, json={'detail':'Synthetic uncertain manual write'})
    else:
        await route.continue_()


async def fail_manual(page, url, kind, claims):
    field, identity = await editor(page, kind, claims)
    path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery'
    await page.route('**'+path, refuse_patch)
    await field.fill('Unconfirmed manual writing')
    await save(page, kind, identity)
    await _answer(page, 'Cancel')
    await page.wait_for_function('!writingInFlight()')
    await expect(field).to_be_enabled()
    await expect(field).to_have_value('Unconfirmed manual writing')
    assert await page.evaluate('hasQuitWriting()') is True
    return field, identity, path


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('state', ['untouched', 'dirty', 'dom-only', 'reverted', 'failed', 'failed-reverted', 'retried'])
def test_focused_editor_quit_lifecycle(engine, state):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                await page.locator(f'.claim[data-claim="{claims[0]}"]').first.wait_for()
                await page.wait_for_function('workspaceClient.pullsInFlight === 0')
                await page.evaluate('id => selectClaim(id)', claims[0])
                await page.locator('#btn-focus-review').click()
                dialog = page.locator('#focused-review')
                await dialog.locator('[data-focus="edit"]').click()
                await expect(dialog.locator('form')).to_have_attribute('data-form', claims[0])
                field = dialog.locator('textarea[name="text"]')
                original = await field.input_value()
                if state == 'dom-only':
                    await field.evaluate("field => { field.value = 'Focused draft'; }")
                elif state != 'untouched':
                    await field.fill('Focused draft')
                if state == 'reverted':
                    await field.fill(original)
                if state in ('failed', 'failed-reverted', 'retried'):
                    path = '**/api/papers/study/claims/'+claims[0]
                    await page.route(path, refuse_patch)
                    await dialog.locator('button[type="submit"]').click()
                    await _answer(page, 'Cancel')
                    await expect(dialog.locator('[data-writing-status="claim"]')).to_contain_text('Couldn’t save')
                    await expect(field).to_be_enabled()
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    if state == 'failed-reverted':
                        await field.fill(original)
                    if state == 'retried':
                        await page.unroute(path, refuse_patch)
                        await dialog.locator('button[type="submit"]').click()
                        await expect(dialog.locator('[data-focus="edit"]')).to_be_visible()
                await dialog.locator('[data-focus="close"]').click()
                failed = state in ('failed', 'failed-reverted')
                assert await page.evaluate('window.doxographSaveDrafts()') is not failed
                expected = original if state in ('untouched', 'reverted', 'failed-reverted') else 'Focused draft'
                if failed:
                    assert await page.evaluate('id => V.drafts[id].text', claims[0]) == expected
                    assert await page.evaluate('id => Boolean(V.unconfirmedClaims[id])', claims[0])
                    assert stored(url, 'claim', claims[0]) == original
                    await page.unroute(path, refuse_patch)
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert stored(url, 'claim', claims[0]) == expected
                assert await page.evaluate('hasQuitWriting()') is False
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('operation', ['toggle', 'bulk', 'undo', 'quote'])
@pytest.mark.parametrize('state', ['clean', 'dirty', 'uncertain'])
def test_partial_ack_advances_only_saved_fields(engine, operation, state):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                assert httpx.patch(url+'/api/papers/study/claims/'+claims[0], json={'reviewed':False, 'tags':['recovery','scaling']}).status_code == 200
                await page.goto(url+'/#view=claims')
                if state == 'uncertain':
                    field, identity, _ = await fail_manual(page, url, 'claim', claims)
                    await field.fill('Stored claim')
                else:
                    field, identity = await editor(page, 'claim', claims)
                    if state == 'dirty':
                        await field.fill('Genuine unsaved text')
                path = '**/api/papers/study/claims/'+identity
                if state == 'uncertain':
                    await page.unroute(path, refuse_patch)

                async def refuse_full_write(route):
                    body = route.request.post_data_json if route.request.method == 'PATCH' else {}
                    if 'text' in body and 'reviewed' in body:
                        await refuse_patch(route)
                    else:
                        await route.continue_()

                await page.route(path, refuse_full_write)
                await page.evaluate('workspaceWritingSnapshot()')
                if operation == 'toggle':
                    await page.locator(f'[data-act="review"][data-claim="{identity}"]').first.click()
                    await page.wait_for_function('!savingClaims.size')
                elif operation in ('bulk', 'undo'):
                    await page.evaluate("reviewWholePaper('study')")
                    assert await page.evaluate('hasQuitWriting()') is (state != 'clean')
                    if operation == 'undo':
                        await page.locator('#toasts').get_by_role('button', name='Undo', exact=True).last.click()
                        await page.wait_for_function('!savingClaims.size && !editHistory.busy')
                else:
                    await page.evaluate("openPageView('library')")
                    await page.evaluate('''id => {
                      const row = S.claims.find(item => item.id === id);
                      V.quoteContext = {claim:id, data:{quote:row.quote, locator:row.locator, suggestion:'Acknowledged wording'}};
                    }''', identity)
                    await page.evaluate('id => usePaperWording("study", id)', identity)
                saved_row = next(row for row in httpx.get(url+'/api/state').json()['claims'] if row['id'] == identity)
                saved_field = 'quote' if operation == 'quote' else 'reviewed'
                expected_field = 'Acknowledged wording' if operation == 'quote' else operation != 'undo'
                assert saved_row[saved_field] == expected_field
                assert await page.evaluate('([id,key]) => V.quitClaimBases[id][key]', [identity, saved_field]) == expected_field
                assert await page.evaluate('hasQuitWriting()') is (state != 'clean')
                if state == 'dirty':
                    assert await page.evaluate('id => V.drafts[id].text', identity) == 'Genuine unsaved text'
                if state == 'uncertain':
                    assert await page.evaluate('id => Boolean(V.unconfirmedClaims[id])', identity)
                assert await page.evaluate('window.doxographSaveDrafts()') is (state == 'clean')
                await page.unroute(path, refuse_full_write)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                if state == 'dirty':
                    assert stored(url, 'claim', identity) == 'Genuine unsaved text'
                assert await page.evaluate('hasQuitWriting()') is False
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'paper', 'synthesis'])
@pytest.mark.parametrize('outcome', ['success', 'failure', 'undo', 'newer', 'other-workspace'])
def test_deleted_manual_record_retires_only_confirmed_old_state(engine, kind, outcome):
    _, claims, _ = seed()
    other = config.create_workspace('Synthetic other corpus')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            started, release = asyncio.Event(), asyncio.Event()
            attempts = []

            async def hold(route):
                if route.request.method == 'PATCH':
                    await refuse_patch(route)
                    return
                if route.request.method == 'DELETE':
                    attempts.append(route.request.url)
                    started.set()
                    await release.wait()
                    if outcome == 'failure':
                        await route.fulfill(status=503, json={'detail':'Synthetic delete failure'})
                        return
                await route.continue_()

            with _server() as url:
                await page.goto(url+'/#view=claims')
                _, identity, path = await fail_manual(page, url, 'synthesis' if kind == 'synthesis' else 'claim', claims)
                if kind == 'paper':
                    path = '/api/papers/study'
                await page.route('**'+path, hold)
                if kind == 'paper':
                    await page.evaluate("() => { window.removing = removePaper('study'); }")
                    await _answer(page, 'Cancel' if outcome == 'undo' else 'Remove')
                    if outcome == 'undo':
                        await page.evaluate('window.removing')
                        assert not attempts
                        assert await page.evaluate('window.doxographSaveDrafts()') is False
                        return
                else:
                    await page.evaluate("openPageView('library')")
                    await page.evaluate("openPageView('claims')")
                    selector = f'[data-act="del"][data-claim="{identity}"]' if kind == 'claim' else '[data-act="del-synth"][data-topic="recovery"]'
                    await page.locator(selector).first.click()
                    if outcome == 'undo':
                        await page.locator('#toasts').get_by_role('button', name='Undo', exact=True).last.click()
                        assert not attempts
                        assert await page.evaluate('window.doxographSaveDrafts()') is False
                        return
                    await page.evaluate('() => { window.removing = flushTrash(); }')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    assert await page.evaluate('hasQuitWriting()') is True
                    if outcome in ('newer', 'other-workspace'):
                        await page.evaluate('''([kind,id,other,move]) => {
                          if (move) { workspaceClient.select(other); resetWorkspaceView(); }
                          if (kind === 'synthesis') {
                            V.synthDrafts[id] = 'Newer retained writing';
                            V.quitSynthBases[id] = {text:'Newer retained writing'};
                            V.unconfirmedSyntheses[id] = {sequence:999};
                          } else {
                            V.drafts[id] = {text:'Newer retained writing'};
                            V.quitClaimBases[id] = {text:'Newer retained writing'};
                            V.unconfirmedClaims[id] = {sequence:999};
                          }
                        }''', [kind, identity, other, outcome == 'other-workspace'])
                    release.set()
                    await page.evaluate('window.removing')
                    clean = outcome == 'success'
                    assert await page.evaluate('hasQuitWriting()') is not clean
                    if outcome not in ('newer', 'other-workspace'):
                        assert await page.evaluate('window.doxographSaveDrafts()') is clean
                    if outcome in ('newer', 'other-workspace'):
                        assert await page.evaluate('([kind,id]) => kind === "synthesis" ? V.synthDrafts[id] : V.drafts[id].text', [kind, identity]) == 'Newer retained writing'
                        assert await page.evaluate('([kind,id]) => (kind === "synthesis" ? V.unconfirmedSyntheses : V.unconfirmedClaims)[id].sequence', [kind, identity]) == 999
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('pathway', ['normal-to-focused', 'focused-async', 'synthesis-same', 'synthesis-other'])
def test_manual_ack_keeps_writing_created_after_the_request(engine, pathway):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'PATCH':
                    if started.is_set():
                        await refuse_patch(route)
                        return
                    result = await route.fetch()
                    assert result.status == 200
                    started.set()
                    await release.wait()
                    await route.fulfill(response=result)
                else:
                    await route.continue_()

            with _server() as url:
                await page.goto(url+'/#view=claims')
                kind = 'synthesis' if pathway.startswith('synthesis') else 'claim'
                field, identity = await editor(page, kind, claims)
                if pathway == 'focused-async':
                    await page.evaluate('id => selectClaim(id)', identity)
                    await page.locator('#btn-focus-review').click()
                    field = page.locator('#focused-review textarea[name="text"]')
                path = '/api/syntheses/recovery' if kind == 'synthesis' else f'/api/papers/study/claims/{identity}'
                await page.route('**'+path, hold)
                if pathway == 'synthesis-other':
                    await page.route('**/api/syntheses/scaling', refuse_patch)
                await field.fill('Acknowledged submitted text')
                await page.evaluate('([kind,id]) => { window.savingWriting = writingAutosave(kind,id).flush(); }', [kind, identity])
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    if pathway == 'normal-to-focused':
                        await page.evaluate('id => selectClaim(id)', identity)
                        await page.locator('#btn-focus-review').click()
                        field = page.locator('#focused-review textarea[name="text"]')
                        await field.fill('Newer retained writing')
                    elif pathway == 'synthesis-other':
                        await page.locator('.synth[data-topic="scaling"] [data-act="edit-synth"]').click()
                        field = page.locator('textarea[data-synth="scaling"]')
                        await field.fill('Newer retained writing')
                        await page.locator('[data-act="del-synth"][data-topic="recovery"]').click()
                        assert await page.evaluate('trash.has("synthesis:recovery")') is False
                    else:
                        # A later edit is admitted while the first write awaits its acknowledgment.
                        await field.evaluate("field => { field.value = 'Newer retained writing'; field.dispatchEvent(new Event('input', {bubbles:true})); }")
                    release.set()
                    await page.evaluate('window.savingWriting')
                    await expect(field).to_have_value('Newer retained writing')
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    if pathway == 'synthesis-other':
                        assert await page.evaluate('V.synthEditing') == 'scaling'
                        await page.unroute('**/api/syntheses/scaling', refuse_patch)
                    await page.unroute('**'+path, hold)
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                    target = 'scaling' if pathway == 'synthesis-other' else identity
                    assert stored(url, kind, target) == 'Newer retained writing'
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('move_workspace', [False, True], ids=['newer-focused-edit', 'other-workspace'])
def test_partial_ack_does_not_replace_newer_or_other_workspace_writing(engine, move_workspace):
    _, claims, _ = seed()
    other = config.create_workspace('Synthetic partial-write destination')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'PATCH':
                    if 'text' in route.request.post_data_json:
                        await refuse_patch(route)
                        return
                    response = await route.fetch()
                    assert response.status == 200
                    started.set()
                    await release.wait()
                    await route.fulfill(response=response)
                else:
                    await route.continue_()

            with _server() as url:
                await page.goto(url+'/#view=claims')
                _, identity = await editor(page, 'claim', claims)
                await page.route('**/api/papers/study/claims/'+identity, hold)
                await page.evaluate('id => { window.toggling = toggleReviewed(S.claims.find(row => row.id === id)); }', identity)
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    if move_workspace:
                        await page.evaluate('''([other,id]) => {
                          workspaceClient.select(other); resetWorkspaceView();
                          V.drafts[id] = {text:'Newer retained writing', reviewed:false};
                          V.quitClaimBases[id] = {text:'Other stored value', reviewed:false};
                        }''', [other, identity])
                    else:
                        await page.evaluate('id => selectClaim(id)', identity)
                        await page.locator('#btn-focus-review').click()
                        field = page.locator('#focused-review textarea[name="text"]')
                        await field.fill('Newer retained writing')
                    release.set()
                    await page.evaluate('window.toggling')
                    assert await page.evaluate('id => V.drafts[id].text', identity) == 'Newer retained writing'
                    assert await page.evaluate('hasQuitWriting()') is True
                    if move_workspace:
                        assert await page.evaluate('id => V.quitClaimBases[id].reviewed', identity) is False
                    else:
                        await page.unroute('**/api/papers/study/claims/'+identity, hold)
                        assert await page.evaluate('window.doxographSaveDrafts()') is True
                        assert stored(url, 'claim', identity) == 'Newer retained writing'
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))
