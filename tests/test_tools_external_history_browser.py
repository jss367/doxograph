"""External actions add only the requested destination to history."""
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import store
from test_ask_browser import _server
from test_research_tools_browser import ready
from test_tools_action_history_browser import inert_tools as inert_tools


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('action', ['queue-paper', 'save-search'])
def test_external_action_back_returns_to_its_original_page(engine, action):
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
                await page.reload()
                await ready(page)
                assert await page.evaluate('V.tool') == destination

    run_browser(scenario())
