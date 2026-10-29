"""A broken startup identifies the failing layer and offers explicit recovery."""

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import store
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failure", ["module", "connection", "script", "late-script"])
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

            async def late_script(route):
                if broken:
                    # A post-app script fails after boot has rendered the corpus.
                    await page.locator('#content .library-paper').first.wait_for()
                    await route.abort()
                else:
                    await route.continue_()

            async def connection(route):
                if broken:
                    await route.fulfill(status=503, json={"detail": "Temporarily unavailable"})
                else:
                    await route.continue_()

            if failure == "connection":
                await page.route("**/api/state", connection)
            elif failure == "late-script":
                await page.route("**/workspace-recovery.js*", late_script)
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
                await expect(page.locator("#content .library-paper")).to_contain_text("Understanding model oversight")
                await expect(page.locator("#selection-count")).to_have_text("1 paper")

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_initial_refresh_failure_requires_full_reload_before_refresh_only_retry(engine, failure):
    _paper("oversight", "Understanding model oversight")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            broken = True
            state_reads = 0

            async def initial_state(route):
                nonlocal state_reads
                state_reads += 1
                if broken:
                    if failure == "error":
                        await route.fulfill(status=503, json={"detail": "Temporarily unavailable"})
                    # A request with no response reaches the client's normal
                    # deadline. It must offer the same full startup recovery.
                    return
                await route.continue_()

            await page.route("**/api/state", initial_state)
            with _server() as url:
                await page.goto(f"{url}/#paper=oversight&kind=finding")
                await expect(page.locator("#startup-error")).to_be_visible(timeout=20000)
                await expect(page.locator("#startup-title")).to_have_text("The library could not connect")
                await expect(page.locator("#sync-status")).to_be_hidden()
                await expect(page.locator("#btn-refresh-library")).to_be_disabled()
                assert await page.evaluate("libraryInitialized") is False
                before_retry = state_reads
                await page.locator("#btn-refresh-library").dispatch_event("click")
                assert state_reads == before_retry

                broken = False
                await page.get_by_role("button", name="Reload library").click()
                await expect(page.locator("#startup-error")).to_be_hidden()
                await expect(page.locator("#kind")).to_have_value("finding")
                await expect(page.locator('.paperhead h2')).to_have_text("Understanding model oversight")
                await expect(page.locator("#btn-refresh-library")).to_be_enabled()
                assert await page.evaluate("libraryInitialized") is True

                # Recovery must start ongoing polling, rather than stopping
                # after drawing the snapshot fetched by the retry itself.
                store.save_paper(store.new_paper("newsource", title="A newly imported source"))
                await expect(page.locator('#papers [data-paper="newsource"]')).to_be_visible(timeout=10000)

    run_browser(scenario())
