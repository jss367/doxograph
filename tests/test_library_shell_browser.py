"""Library controls stay discoverable without competing with research content."""

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from test_browser import _paper, _poll, _server


@pytest.mark.browser
def test_empty_library_hides_selection_until_papers_arrive():
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await expect(page.locator("#selection-tools")).to_be_hidden()
                _paper("first", "The first paper")
                await _poll(page)
                await expect(page.locator("#selection-tools")).to_be_visible()
                await expect(page.locator("#btn-focus-review")).to_be_hidden()

    run_browser(scenario())

