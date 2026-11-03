"""External actions add only the requested destination to history."""
import asyncio

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import store
from test_ask_browser import _server
from test_research_tools_browser import ready
from test_tools_action_history_browser import inert_tools as inert_tools


RESTORED_ROUTE = """tool => {
  const panel = document.querySelector('#research-tools');
  const route = new URLSearchParams(location.hash.slice(1));
  return route.get('view') === 'tools' && route.get('tool') === tool
    && V.view === 'tools' && V.tool === tool && panel && !panel.hidden
    && panel.getAttribute('aria-busy') === 'false';
}"""


async def restored(page, destination, timeout=10000):
    await page.wait_for_function(RESTORED_ROUTE, arg=destination, timeout=timeout)


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('action', ['queue-paper', 'save-search'])
@pytest.mark.parametrize('delayed_startup', [False, True], ids=['normal', 'held-startup'])
def test_external_action_back_returns_to_its_original_page(engine, action, delayed_startup):
    store.save_paper(store.new_paper('p', title='Synthetic selected paper'))
    if action == 'save-search':
        store.add_claim('p', {'text':'Synthetic claim for saved filters'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + ('/#paper=p' if action == 'queue-paper' else '/#view=claims' if action == 'save-search' else '/'))
                await page.wait_for_function('workspaceClient.state.papers.some(p => p.key === "p")')
                await page.locator('[data-tool="' + action + '"]').first.wait_for(state='visible')
                origin = await page.evaluate('({view:V.view,paper:V.paper || null})')
                count = await page.evaluate('history.length')
                await page.locator('[data-tool="' + action + '"]').first.click()
                if action == 'save-search':
                    await page.locator('#ask-input').fill('Synthetic saved search')
                    await page.locator('#ask').get_by_role('button', name='Save search', exact=True).click()
                destination = 'searches' if action == 'save-search' else 'reading'
                await page.wait_for_function('tool => V.tool === tool', arg=destination)
                await ready(page)
                assert await page.evaluate('history.length') == count + 1
                await page.go_back()
                await expect(page.locator('#research-tools')).not_to_be_visible()
                assert await page.evaluate('({view:V.view,paper:V.paper || null})') == origin
                await page.go_forward()
                await page.wait_for_function('tool => V.tool === tool', arg=destination)
                await ready(page)
                before_reload = page.url
                held, release = asyncio.Event(), asyncio.Event()

                async def hold_workspaces(route):
                    held.set()
                    await release.wait()
                    await route.continue_()

                if delayed_startup:
                    await page.route('**/api/workspaces', hold_workspaces)
                try:
                    await page.reload()
                    if delayed_startup:
                        await asyncio.wait_for(held.wait(), 5)
                        # A non-busy hidden panel is not a restored route. The
                        # old helper returns here before boot has applied hash.
                        await ready(page)
                        assert await page.evaluate('V.tool') is None
                        assert await page.evaluate(RESTORED_ROUTE, destination) is False
                        assert page.url == before_reload
                        release.set()
                    await restored(page, destination)
                    assert await page.evaluate('V.tool') == destination
                    assert page.url == before_reload
                    if action == 'queue-paper':
                        await expect(page.locator('#research-tools [data-reading="p"]')).to_be_visible()
                    else:
                        await expect(page.locator('#research-body')).to_contain_text('Synthetic saved search')
                finally:
                    release.set()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_restoration_wait_rejects_a_different_route(engine):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + '/#view=library')
                await page.locator('#btn-agent:enabled').wait_for()
                # The old helper accepts this completely different route.
                await ready(page)
                assert await page.evaluate(RESTORED_ROUTE, 'reading') is False
                with pytest.raises(BrowserTimeout):
                    await restored(page, 'reading', timeout=500)
                assert await page.evaluate('V.view') == 'library'
                assert await page.evaluate('V.tool') is None
    run_browser(scenario())
