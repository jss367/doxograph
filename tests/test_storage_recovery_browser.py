"""Storage warnings remain visible until the underlying files are repaired."""

import asyncio

import pytest
from playwright.async_api import async_playwright, expect

from doxograph import store
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_storage_warning_survives_navigation_and_clears_after_repair(engine):
    _paper("healthy", "Healthy paper", "topic")
    damaged = store.paper_path("damaged")
    damaged.write_text("{broken")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            try:
                page = await browser.new_page()
                with _server() as url:
                    await page.goto(url)
                    warning = page.locator("#storage-warning")
                    await expect(warning).to_be_visible()
                    await expect(warning).to_contain_text("Paper damaged could not be loaded")
                    await warning.get_by_text("File details").click()
                    await expect(warning.locator("code")).to_have_text(str(damaged))
                    await page.locator('#papers [data-paper="healthy"]').click()
                    await expect(warning).to_be_visible()
                    await page.reload()
                    await expect(warning).to_be_visible()
                    store.write_json(damaged, store.new_paper("damaged", title="Recovered paper"))
                    await expect(warning).to_be_hidden(timeout=15000)
                    await expect(page.locator('#papers [data-paper="damaged"]')).to_be_visible()
            finally:
                await browser.close()

    asyncio.run(scenario())
