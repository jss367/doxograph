"""Manual quit baselines follow focused edits, partial writes and confirmed deletes."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_browser import _answer
from test_core_workspace_boundary_browser import editor, seed
from test_quit_manual_editors_browser import save
from test_tools_action_history_browser import inert_tools as inert_tools


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
    await page.wait_for_function('!savingClaims.size && !V.synthSaving')
    await expect(field).to_be_enabled()
    await page.unroute('**'+path, refuse_patch)
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
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                elif state != 'untouched':
                    await field.fill('Focused draft')
                if state == 'reverted':
                    await field.fill(original)
                if state in ('failed', 'failed-reverted', 'retried'):
                    path = '**/api/papers/study/claims/'+claims[0]
                    await page.route(path, refuse_patch)
                    await dialog.locator('button[type="submit"]').click()
                    await expect(dialog.locator('#focus-error')).to_contain_text('Could not save')
                    await expect(field).to_be_enabled()
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    await page.unroute(path, refuse_patch)
                    if state == 'failed-reverted':
                        await field.fill(original)
                    if state == 'retried':
                        await dialog.locator('button[type="submit"]').click()
                        await expect(dialog.locator('[data-focus="edit"]')).to_be_visible()
                await dialog.locator('[data-focus="close"]').click()
                assert await page.evaluate('window.doxographSaveDrafts()') is (state in ('untouched', 'reverted', 'retried'))
                if state in ('dirty', 'failed'):
                    assert await page.evaluate('id => V.drafts[id].text', claims[0]) == 'Focused draft'
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
                await page.evaluate('window.doxographSaveDrafts()')
                if operation == 'toggle':
                    await page.locator(f'[data-act="review"][data-claim="{identity}"]').first.click()
                    await page.wait_for_function('!savingClaims.size')
                elif operation in ('bulk', 'undo'):
                    await page.evaluate("reviewWholePaper('study')")
                    assert await page.evaluate('window.doxographSaveDrafts()') is (state == 'clean')
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
                assert await page.evaluate('window.doxographSaveDrafts()') is (state == 'clean')
                if state == 'dirty':
                    assert await page.evaluate('id => V.drafts[id].text', identity) == 'Genuine unsaved text'
                if state == 'uncertain':
                    assert await page.evaluate('id => Boolean(V.unconfirmedClaims[id])', identity)
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
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
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
                await field.fill('Acknowledged submitted text')
                if pathway == 'focused-async':
                    await page.locator('#focused-review button[type="submit"]').click()
                else:
                    await save(page, kind, identity)
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    if pathway == 'normal-to-focused':
                        await page.evaluate('id => selectClaim(id)', identity)
                        await page.locator('#btn-focus-review').click()
                        field = page.locator('#focused-review textarea[name="text"]')
                        await field.fill('Newer retained writing')
                        await page.locator('#focused-review [data-act="cancel"]').click()
                        await expect(field).to_have_value('Newer retained writing')
                    elif pathway == 'synthesis-other':
                        await page.locator('.synth[data-topic="scaling"] [data-act="edit-synth"]').click()
                        field = page.locator('textarea[data-synth="scaling"]')
                        await field.fill('Newer retained writing')
                        await page.locator('[data-act="del-synth"][data-topic="recovery"]').click()
                        assert await page.evaluate('trash.has("synthesis:recovery")') is False
                    else:
                        # Model an already admitted asynchronous form update while controls are disabled.
                        await field.evaluate("field => { field.value = 'Newer retained writing'; field.dispatchEvent(new Event('input', {bubbles:true})); }")
                    release.set()
                    await page.wait_for_function('!savingClaims.size && !V.synthSaving')
                    if pathway == 'focused-async':
                        await expect(page.locator('#focused-review [data-focus="edit"]')).to_be_visible()
                        await page.locator('#focused-review [data-focus="edit"]').click()
                        field = page.locator('#focused-review textarea[name="text"]')
                    await expect(field).to_have_value('Newer retained writing')
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    if pathway == 'synthesis-other':
                        assert await page.evaluate('V.synthEditing') == 'scaling'
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
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    if move_workspace:
                        assert await page.evaluate('id => V.quitClaimBases[id].reviewed', identity) is False
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 25))
