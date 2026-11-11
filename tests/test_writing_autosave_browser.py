"""Existing writing saves without a submit, including edits made in flight."""
import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser, inert_tools as inert_tools
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_quit_manual_editors_browser import stored
from test_browser import _answer


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('failure', [False, True])
def test_existing_writing_autosaves_without_closing_editor(engine, kind, failure):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url + '/#view=claims')
                field, identity = await editor(page, kind, claims)
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                started, release = asyncio.Event(), asyncio.Event()
                requests = []

                async def hold(route):
                    requests.append(route.request.post_data_json)
                    if len(requests) == 1:
                        started.set()
                        await release.wait()
                    if failure:
                        await route.fulfill(status=503, json={'detail': 'Synthetic offline save'})
                    else:
                        await route.continue_()

                await page.route('**' + path, hold)
                await field.fill('First automatic edit')
                await asyncio.wait_for(started.wait(), 5)
                await expect(field).to_be_enabled()
                await field.fill('Newer writing during save')
                release.set()
                status = page.locator(f'[data-writing-status="{kind}"]').first
                if failure:
                    await expect(status).to_contain_text('Couldn’t save')
                    assert await page.evaluate('window.doxographSaveDrafts()') is False
                    await expect(field).to_have_value('Newer writing during save')
                    await page.unroute('**' + path, hold)
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                else:
                    await expect(status).to_have_text('Saved')
                    assert len(requests) >= 2
                await expect(field).to_be_visible()
                await expect(field).to_have_value('Newer writing during save')
                assert stored(url, kind, identity) == 'Newer writing during save'
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert not errors
                await page.reload()
                assert stored(url, kind, identity) == 'Newer writing during save'

    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
def test_workspace_switch_flushes_then_retires_writing_timers(engine, kind):
    from doxograph import config
    _, claims, _ = seed()
    other = config.create_workspace('Second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                field, identity = await editor(page, kind, claims)
                await field.fill('Save before switching')
                await page.evaluate('id => switchWorkspace(id)', other)
                assert await page.evaluate('workspaceClient.workspaceId') == other
                assert await page.evaluate('window.doxographSaveDrafts()') is True, await page.evaluate('({drafts:V.drafts, synth:V.synthDrafts, research:V.researchDraft, edit:V.editing, writing:hasQuitWriting(), pending:workspaceClient.pendingMutations, entries:[...writingAutosaves.values()].map(e=>({active:e.owner===V.drafts,saving:e.saver.saving,unconfirmed:e.saver.unconfirmed,failure:e.saver.failure}))})')
                assert stored(url, kind, identity) == 'Save before switching'
                await page.evaluate("switchWorkspace('default')")
                assert await page.evaluate('window.doxographSaveDrafts()') is True, await page.evaluate('({drafts:V.drafts, synth:V.synthDrafts, research:V.researchDraft, edit:V.editing, writing:hasQuitWriting(), pending:workspaceClient.pendingMutations, entries:[...writingAutosaves.values()].map(e=>({active:e.owner===V.drafts,saving:e.saver.saving,unconfirmed:e.saver.unconfirmed,failure:e.saver.failure}))})')
                assert stored(url, kind, identity) == 'Save before switching'
    run_browser(asyncio.wait_for(scenario(), 25))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research', 'focused-claim'])
def test_failed_done_can_be_cancelled_then_discarded(engine, kind):
    """Discard closes a failed editor, retires retries, and preserves saved data."""
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url + '/#view=claims')
                if kind == 'focused-claim':
                    await page.locator('#btn-focus-review').click()
                    await page.locator('[data-focus="edit"]').click()
                    field = page.locator('#focused-review textarea[name="text"]')
                    identity = await page.locator('#focused-review form[data-form]').get_attribute('data-form')
                    done = page.locator('#focused-review button[type="submit"]')
                else:
                    field, identity = await editor(page, kind, claims)
                    selector = f'form[data-form="{identity}"] button[type="submit"]' if kind == 'claim' else '[data-act="save-synth"]' if kind == 'synthesis' else '#research-form button[type="submit"]'
                    done = page.locator(selector).first
                saved_kind = 'claim' if kind == 'focused-claim' else kind
                original = stored(url, saved_kind, identity)
                path = f'/api/papers/study/claims/{identity}' if saved_kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                requests = []

                async def reject(route):
                    requests.append(route.request.method)
                    await route.fulfill(status=400, json={'detail': 'Synthetic validation failure'})

                await page.route('**' + path, reject)
                await field.fill('Unsavable writing')
                await done.click()
                await _answer(page, 'Cancel')
                await expect(field).to_have_value('Unsavable writing')
                await done.click()
                await _answer(page, 'Discard changes')
                await expect(field).to_have_count(0)
                count = len(requests)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.wait_for_timeout(700)
                assert len(requests) == count
                assert stored(url, saved_kind, identity) == original
                assert await page.evaluate('([kind, id]) => kind === "claim" ? !V.drafts[id] && !V.quitClaimBases[id] && !V.unconfirmedClaims[id] : kind === "synthesis" ? !V.synthDrafts[id] && !V.quitSynthBases[id] && !V.unconfirmedSyntheses[id] : V.researchDraft === null', [saved_kind, identity])
                await page.unroute('**' + path, reject)
                if kind == 'focused-claim':
                    await page.locator('[data-focus="edit"]').click()
                else:
                    field, _ = await editor(page, kind, claims)
                await expect(field).to_have_value(original)
                await field.fill('Valid writing after discard')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert stored(url, saved_kind, identity) == 'Valid writing after discard'
    run_browser(asyncio.wait_for(scenario(), 30))
