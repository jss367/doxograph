"""Quit compares existing manual forms to their opened saved baselines."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from doxograph import config
from test_quit_manual_editors_browser import cancel, frozen_report, resume, stored
from browser_support import inert_tools as inert_tools


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis'])
@pytest.mark.parametrize('state', ['untouched', 'reverted', 'untouched-newer-poll', 'edited-matches-poll'])
@pytest.mark.parametrize('parked', [False, True], ids=['open', 'parked'])
def test_clean_editor_quit_compares_the_opened_baseline(engine, kind, state, parked):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                original = await field.input_value()
                if state == 'reverted':
                    await field.fill('An edit that was reverted')
                    assert await page.evaluate('hasQuitWriting()') is True
                    await field.fill(original)
                elif state == 'edited-matches-poll':
                    await field.fill('A genuinely edited value')
                if 'poll' in state:
                    # A mocked newer state cannot redefine the baseline under an open form.
                    await page.evaluate('''([kind, id]) => {
                      const record = kind === 'claim' ? S.claims.find(row => row.id === id) : synthesisFor(id);
                      record.text = 'A genuinely edited value';
                    }''', [kind, identity])
                if parked:
                    await page.evaluate("openPageView('library')")
                clean = state != 'edited-matches-poll'
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                report = await frozen_report(page)
                assert report['saved'] is True
                assert await frozen_report(page) == report
                await resume(page)  # Don't Quit keeps the opened/parked form state.
                assert stored(url, kind, identity) == (original if clean else 'A genuinely edited value')
                if not parked:
                    await expect(field).to_have_value('A genuinely edited value' if not clean else original)
                    await cancel(page, kind, identity)
                    assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('response', ['lost', 'definite-failure'])
def test_failed_write_stays_unsaved_after_reverting_to_baseline(engine, kind, response):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            async def uncertain(route):
                if response == 'lost':
                    committed = await route.fetch()
                    assert committed.status == 200
                    await route.abort('failed')
                else:
                    await route.fulfill(status=503, json={'detail':'Synthetic refused save'})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                original = await field.input_value()
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                await page.route('**'+path, uncertain)
                await field.fill('Transient writing')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                await field.fill(original)
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                await expect(field).to_have_value(original)
                await page.unroute('**'+path, uncertain)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                assert stored(url, kind, identity) == original
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_new_claim_matching_an_existing_record_still_needs_consent(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="study"]').click()
                await page.locator('.paper-actions[data-paper="study"] > summary').click()
                await page.locator('[data-act="add-claim"][data-paper="study"]').click()
                await page.locator('form[data-form="__new__"] [name="text"]').fill('Stored claim')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                report = await frozen_report(page)
                assert report['saved'] is False
                await resume(page)
                await page.locator('form[data-form="__new__"] [data-act="cancel"]').click()
                assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis'])
def test_manual_quit_baselines_belong_to_the_committed_workspace(engine, kind):
    _, claims, _ = seed()
    other = config.create_workspace('Synthetic second workspace')['id']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                _, identity = await editor(page, kind, claims)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.evaluate('id => { window.switching = switchWorkspace(id); }', other)
                await page.evaluate('window.switching')
                assert await page.evaluate('[Object.keys(V.quitClaimBases).length,Object.keys(V.quitSynthBases).length,Object.keys(V.unconfirmedClaims).length,Object.keys(V.unconfirmedSyntheses).length]') == [0, 0, 0, 0]
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery'
                response = httpx.patch(url+path, headers={'X-Doxograph-Workspace':'default'}, json={'text':'New stored baseline'})
                assert response.status_code == 200
                await page.evaluate("switchWorkspace('default')")
                await page.evaluate("openPageView('claims')")
                field, _ = await editor(page, kind, claims)
                await expect(field).to_have_value('New stored baseline')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
    run_browser(asyncio.wait_for(scenario(), 20))
