"""A broken startup identifies the failing layer and offers explicit recovery."""

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failure", ["module", "connection", "script"])
def test_startup_failure_and_reload_recovery(engine, failure):
    _paper("oversight", "Understanding model oversight")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            broken = True

            async def module(route):
                if not broken:
                    return await route.continue_()
                if failure == "script":
                    return await route.abort()
                response = await route.fetch()
                source = (await response.text()).replace("sync,syncSelectionControls,", "sync,")
                await route.fulfill(response=response, body=source)

            async def connection(route):
                if broken:
                    await route.fulfill(status=503, json={"detail": "Temporarily unavailable"})
                else:
                    await route.continue_()

            if failure == "connection":
                await page.route("**/api/state", connection)
            else:
                await page.route("**/research-tools.js*", module)
            with _server() as url:
                await page.goto(url)
                await expect(page.locator("#startup-error")).to_be_visible()
                await expect(page.locator("#startup-title")).to_have_text(
                    "The library could not connect" if failure == "connection"
                    else "The interface could not start")
                await expect(page.locator("#content")).not_to_contain_text("Could not reach the server")
                if failure == "module":
                    await expect(page.locator("#startup-detail")).to_contain_text("syncSelectionControls")
                broken = False
                await page.get_by_role("button", name="Reload library").click()
                await expect(page.locator("#startup-error")).to_be_hidden()
                await expect(page.locator("#content")).to_contain_text("A claim from")
                await expect(page.locator("#selection-count")).to_have_text("1 claim")

    run_browser(scenario())
