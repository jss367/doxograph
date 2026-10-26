"""Browser reuse must preserve isolation and clean up failed scenarios."""

import pytest

from browser_support import browser_session, run_browser


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reused_browser_discards_contexts_after_failure(engine):
    async def failed_scenario():
        async with browser_session(engine) as browser:
            context = await browser.new_context()
            await context.add_cookies([{
                "name": "draft", "value": "private", "url": "http://127.0.0.1",
            }])
            page = await context.new_page()
            # No server needed: storage and routes belong to this context.
            await context.route("http://127.0.0.1/**", lambda route: route.fulfill(body="first"))
            await page.goto("http://127.0.0.1/")
            await page.evaluate("localStorage.setItem('draft', 'private')")
            raise ValueError("A scenario failed")

    async def next_scenario():
        async with browser_session(engine) as browser:
            assert browser.is_connected()
            assert browser.contexts == []
            page = await browser.new_page()
            assert await page.context.cookies("http://127.0.0.1") == []
            await page.route("http://127.0.0.1/**", lambda route: route.fulfill(body="second"))
            await page.goto("http://127.0.0.1/")
            assert await page.locator("body").inner_text() == "second"
            assert await page.evaluate("localStorage.getItem('draft')") is None
            return browser

    with pytest.raises(ValueError, match="A scenario failed"):
        run_browser(failed_scenario())
    first = run_browser(next_scenario())
    assert run_browser(next_scenario()) is first
