"""Existing writing saves without a submit, including edits made in flight."""
import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser, inert_tools as inert_tools
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_quit_manual_editors_browser import stored


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
