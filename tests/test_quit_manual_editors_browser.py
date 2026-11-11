"""Manual writing warns on quit, retaining its explicit Save/Cancel semantics."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_core_workspace_boundary_browser import editor, seed
from test_quit_page_imports_browser import native_contract
from browser_support import inert_tools as inert_tools


async def frozen_report(page):
    save, _, _ = native_contract()
    return await page.evaluate('async () => { const freezeEditing = true; '+save+' }')


async def resume(page):
    _, script, _ = native_contract()
    await page.evaluate('() => {'+script+'}')


async def cancel(page, kind, identity):
    # Closing an existing editor now flushes its writing.
    await save(page, kind, identity)


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
@pytest.mark.parametrize('parked', [False, True])
def test_quit_flushes_existing_writing(engine, kind, parked):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                await field.fill('Writing before quit')
                if parked:
                    await page.evaluate("openPageView('library')")
                report = await frozen_report(page)
                assert report['saved'] is True
                assert stored(url, kind, identity) == 'Writing before quit'
                await resume(page)
    run_browser(asyncio.wait_for(scenario(), 20))


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('kind', ['claim', 'synthesis', 'research'])
@pytest.mark.parametrize('success', [False, True])
def test_quit_waits_for_save_and_preserves_failures(engine, kind, success):
    _, claims, _ = seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                started.set()
                await release.wait()
                if success:
                    await route.continue_()
                else:
                    await route.fulfill(status=503, json={'detail':'Synthetic save failure'})

            with _server() as url:
                await page.goto(url+'/#view=claims')
                field, identity = await editor(page, kind, claims)
                path = f'/api/papers/study/claims/{identity}' if kind == 'claim' else '/api/syntheses/recovery' if kind == 'synthesis' else '/api/context'
                await page.route('**'+path, hold)
                original = stored(url, kind, identity)
                await field.fill('Writing in flight')
                task = asyncio.create_task(frozen_report(page))
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    assert not task.done()
                    assert stored(url, kind, identity) == original
                    release.set()
                    assert (await task)['saved'] is success
                    await resume(page)
                    await expect(field).to_have_value('Writing in flight')
                    if not success:
                        await page.unroute('**'+path, hold)
                        assert await page.evaluate('window.doxographSaveDrafts()') is True
                    assert stored(url, kind, identity) == 'Writing in flight'
                finally:
                    release.set()
                    await task
    run_browser(asyncio.wait_for(scenario(), 20))
