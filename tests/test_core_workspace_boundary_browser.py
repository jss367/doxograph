"""Core writing created during held deletion is rechecked at selection."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server
from test_browser import _answer
from test_tools_action_history_browser import inert_tools as inert_tools


def seed():
    for key in ['study', 'second']:
        paper = store.new_paper(key, title='Synthetic ' + key)
        paper['notes'] = 'Stored note'
        store.save_paper(paper)
    removed = store.add_claim('study', {'text':'Removed claim', 'tags':['recovery']})['id']
    claims = [store.add_claim('study', {'text':'Stored claim', 'tags':['recovery']})['id'] for _ in range(2)]
    claims.append(store.add_claim('second', {'text':'Other topic claim', 'tags':['scaling']})['id'])
    rows = {row['id']:row for row in store.claim_rows()}
    for topic in ['recovery', 'scaling']:
        store.record_synthesis(topic, 'Stored synthesis', rows)
    return removed, claims, config.create_workspace('Synthetic destination')['id']


async def editor(page, kind, claims, second=False):
    if kind == 'claim':
        identity = claims[int(second)]
        await page.locator(f'[data-act="edit"][data-claim="{identity}"]').first.click()
        return page.locator(f'form[data-form="{identity}"] [name="text"]'), identity
    if kind == 'synthesis':
        identity = 'scaling' if second else 'recovery'
        await page.locator(f'.synth[data-topic="{identity}"] [data-act="edit-synth"]').click()
        return page.locator(f'textarea[data-synth="{identity}"]'), identity
    if kind == 'paper-note':
        identity = 'second' if second else 'study'
        await page.locator(f'#papers li[data-paper="{identity}"]').click()
        await page.locator(f'[data-act="edit-note"][data-paper="{identity}"]').click()
        return page.locator(f'textarea[data-note="{identity}"]'), identity
    await page.locator('#research-nav [data-view="research"]').click()
    return page.locator('#research-context'), 'context'


async def remove_and_wait(page, url, removed):
    await page.goto(url+'/#view=claims')
    await page.locator(f'.claim[data-claim="{removed}"] [data-act="del"]').first.click()
    await page.locator('#toasts .toast', has_text='Deleted the claim.').wait_for()


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'paper-note', 'research'])
@pytest.mark.parametrize('decision', ['Cancel', 'Discard and switch'])
def test_late_core_dom_writing_needs_consent_after_held_delete(engine, kind, decision):
    removed, claims, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'DELETE':
                    held.set()
                    await release.wait()
                await route.continue_()

            await page.route(f'**/api/papers/study/claims/{removed}', hold)
            with _server() as url:
                await remove_and_wait(page, url, removed)
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', destination)
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    field, identity = await editor(page, kind, claims)
                    # Autofill/programmatic DOM text must count even without an input event.
                    await field.evaluate('(field, value) => { field.value = value; }', 'Late DOM writing')
                    release.set()
                    await expect(page.locator('#ask')).to_be_visible(timeout=2000)
                    await _answer(page, decision)
                    await page.evaluate('window.switching')
                    expected_workspace = 'default' if decision == 'Cancel' else destination
                    assert await page.evaluate('workspaceClient.workspaceId') == expected_workspace
                    if decision == 'Cancel':
                        await expect(field).to_have_value('Late DOM writing')
                        draft = await page.evaluate('([kind, id]) => kind === "claim" ? V.drafts[id].text : kind === "synthesis" ? V.synthDrafts[id] : kind === "paper-note" ? V.noteDrafts[id] : V.researchDraft.context', [kind, identity])
                        assert draft == 'Late DOM writing'
                    else:
                        assert await page.evaluate('[Object.keys(V.drafts).length,Object.keys(V.synthDrafts).length,Object.keys(V.noteDrafts).length,V.researchDraft]') == [0, 0, 0, None]
                    assert store.load_paper('study')['notes'] == 'Stored note'
                    assert next(row for row in store.claim_rows() if row['id'] == claims[0])['text'] == 'Stored claim'
                finally:
                    release.set()
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_unchanged_approved_core_draft_does_not_ask_twice(engine):
    removed, claims, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'DELETE':
                    held.set()
                    await release.wait()
                await route.continue_()

            await page.route(f'**/api/papers/study/claims/{removed}', hold)
            with _server() as url:
                await remove_and_wait(page, url, removed)
                field, _ = await editor(page, 'claim', claims)
                await field.fill('Approved existing writing')
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', destination)
                await _answer(page, 'Discard and switch')
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    release.set()
                    await page.evaluate('window.switching')
                    assert await page.evaluate('workspaceClient.workspaceId') == destination
                    await expect(page.locator('#ask')).not_to_be_visible()
                finally:
                    release.set()
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'paper-note'])
def test_same_content_replacement_core_identity_needs_new_consent(engine, kind):
    removed, claims, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method == 'DELETE':
                    held.set()
                    await release.wait()
                await route.continue_()

            await page.route(f'**/api/papers/study/claims/{removed}', hold)
            with _server() as url:
                await remove_and_wait(page, url, removed)
                field, _ = await editor(page, kind, claims)
                await field.fill('Identical writing')
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', destination)
                await _answer(page, 'Discard and switch')
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    action = {'claim':'cancel', 'synthesis':'cancel-synth', 'paper-note':'cancel-note'}[kind]
                    await page.locator(f'[data-act="{action}"]').first.click()
                    field, _ = await editor(page, kind, claims, second=True)
                    await field.fill('Identical writing')
                    release.set()
                    await expect(page.locator('#ask')).to_be_visible(timeout=2000)
                    await _answer(page, 'Cancel')
                    await page.evaluate('window.switching')
                    assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                    await expect(field).to_have_value('Identical writing')
                finally:
                    release.set()
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'claim-conflict', 'synthesis', 'paper-note'])
def test_save_started_during_held_delete_vetoes_selection_until_it_finishes(engine, kind):
    removed, claims, destination = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            held, release, saving, save_release = (asyncio.Event() for _ in range(4))

            async def hold_delete(route):
                if route.request.method == 'DELETE':
                    held.set()
                    await release.wait()
                await route.continue_()

            async def hold_save(route):
                if route.request.method == 'PATCH':
                    saving.set()
                    await save_release.wait()
                await route.continue_()

            await page.route(f'**/api/papers/study/claims/{removed}', hold_delete)
            claim_id = claims[0] if kind == 'claim-conflict' else claims[2]
            target = f'/api/papers/{"study" if kind == "claim-conflict" else "second"}/claims/{claim_id}' if kind.startswith('claim') else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/papers/second'
            await page.route('**'+target, hold_save)
            with _server() as url:
                await remove_and_wait(page, url, removed)
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', destination)
                try:
                    await asyncio.wait_for(held.wait(), 5)
                    edit_kind = 'claim' if kind.startswith('claim') else kind
                    field, _ = await editor(page, edit_kind, [claim_id, claims[1]], second=kind == 'paper-note')
                    await field.fill('Saved while switching')
                    if kind.startswith('claim'):
                        await page.locator(f'form[data-form="{claim_id}"] button[type="submit"]').click()
                    else:
                        await page.locator('[data-act="save-synth"]' if kind == 'synthesis' else '[data-act="save-note"]').click()
                    release.set()
                    await asyncio.wait_for(saving.wait(), 5)
                    await page.evaluate('window.switching')
                    assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                    await expect(page.locator('#toasts')).to_contain_text('Wait for the current change')
                    save_release.set()
                    await page.wait_for_function('workspaceClient.pendingMutations === 0 && !savingClaims.size && !V.synthSaving && !V.noteSaving')
                    assert await page.evaluate('workspaceClient.workspaceId') == 'default'
                    if kind == 'claim-conflict':
                        assert next(row for row in store.claim_rows() if row['id'] == claim_id)['text'] == 'Stored claim'
                        assert await page.evaluate('id => V.drafts[id].text', claim_id) == 'Saved while switching'
                        assert 'changed' in await page.evaluate('V.error')
                    elif kind == 'claim':
                        assert next(row for row in store.claim_rows() if row['id'] == claim_id)['text'] == 'Saved while switching'
                    elif kind == 'paper-note':
                        assert store.load_paper('second')['notes'] == 'Saved while switching'
                    else:
                        rows = httpx.get(url+'/api/state').json()['syntheses']
                        assert next(row for row in rows if row['topic'] == 'recovery')['text'] == 'Saved while switching'
                finally:
                    release.set()
                    save_release.set()
    run_browser(scenario())
