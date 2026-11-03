"""Manual writing warns on quit, retaining its explicit Save/Cancel semantics."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_quit_page_imports_browser import native_contract
from test_tools_action_history_browser import inert_tools as inert_tools


async def frozen_report(page):
    save, _, _ = native_contract()
    return await page.evaluate('async () => { const freezeEditing = true; '+save+' }')


async def resume(page):
    _, script, _ = native_contract()
    await page.evaluate('() => {'+script+'}')


async def cancel(page, kind, identity):
    target = f'form[data-form="{identity}"] [data-act="cancel"]' if kind == 'claim' else '[data-act="cancel-synth"]' if kind == 'synthesis' else '[data-act="cancel-research"]'
    await page.locator(target).first.click()


async def save(page, kind, identity):
    target = f'form[data-form="{identity}"] button[type="submit"]' if kind == 'claim' else '[data-act="save-synth"]' if kind == 'synthesis' else '#research-form button[type="submit"]'
    await page.locator(target).first.click()


def stored(url, kind, identity):
    state = httpx.get(url+'/api/state').json()
    if kind == 'claim':
        return next(row['text'] for row in state['claims'] if row['id'] == identity)
    if kind == 'synthesis':
        return next(row['text'] for row in state['syntheses'] if row['topic'] == identity)
    return state['context']


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('decision', ['save-after-stay', 'discard-after-stay', 'quit-anyway'])
def test_manual_writing_warns_and_preserves_explicit_choices(engine, kind, decision):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                original = stored(url, kind, identity)
                await field.fill('Manual writing before quit')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                assert stored(url, kind, identity) == original
                report = await frozen_report(page)
                assert report['saved'] is False and report['snapshot']
                await page.keyboard.type('Blocked while frozen')
                await expect(field).to_have_value('Manual writing before quit')
                # The native actual-handler probe verifies the affirmative choice
                # uses this identical nonnil snapshot once, without a second alert.
                assert await frozen_report(page) == report
                if decision == 'quit-anyway':
                    await page.close()  # Model the explicitly accepted loss; never quit the app.
                    page = await browser.new_page()
                    await page.goto(url)
                    assert stored(url, kind, identity) == original
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
                    return
                await resume(page)  # Don't Quit retains both form and writing.
                await expect(field).to_have_value('Manual writing before quit')
                await field.fill('Manual writing after Stay')
                changed = await frozen_report(page)
                assert changed['saved'] is False and changed['snapshot'] != report['snapshot']
                await resume(page)
                if decision == 'save-after-stay':
                    await save(page, kind, identity)
                    await page.wait_for_function('!savingClaims.size && !V.synthSaving && !V.researchSaving && !hasWorkspaceDrafts()')
                    assert stored(url, kind, identity) == 'Manual writing after Stay'
                else:
                    await cancel(page, kind, identity)
                    assert stored(url, kind, identity) == original
                assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('success', [False, True], ids=['failed-save', 'acknowledged-save'])
def test_pending_manual_save_cannot_report_success_before_its_answer(engine, kind, success):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                if route.request.method in ('PATCH', 'PUT'):
                    started.set()
                    await release.wait()
                    if not success:
                        await route.fulfill(status=503, json={'detail':'Synthetic manual save failure'})
                        return
                await route.continue_()

            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                await page.route('**'+path, hold)
                original = stored(url, kind, identity)
                await field.fill('Manual save in flight')
                await save(page, kind, identity)
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    report = await frozen_report(page)
                    assert report['saved'] is False
                    assert stored(url, kind, identity) == original
                    release.set()
                    await page.wait_for_function('!savingClaims.size && !V.synthSaving && !V.researchSaving')
                    assert await page.evaluate('window.doxographSaveDrafts()') is success, await page.evaluate('[V.view,V.researchDraft,V.researchBase,S.context,Boolean(document.querySelector("#research-form")),hasWorkspaceDrafts(),V.researchSaving]')
                    assert stored(url, kind, identity) == ('Manual save in flight' if success else original)
                    await resume(page)
                    if not success:
                        await expect(field).to_have_value('Manual save in flight')
                        await cancel(page, kind, identity)
                        assert await page.evaluate('window.doxographSaveDrafts()') is True
                finally:
                    release.set()
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
def test_parked_manual_writing_remains_part_of_quit_consent(engine, kind):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                original = stored(url, kind, identity)
                await field.fill('Parked manual writing')
                await page.evaluate("openPageView('library')")
                assert await page.evaluate('V.view') == 'library'
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                report = await frozen_report(page)
                assert report['saved'] is False and 'Parked manual writing' in report['snapshot']
                assert stored(url, kind, identity) == original
                await resume(page)
    run_browser(asyncio.wait_for(scenario(), 20))
