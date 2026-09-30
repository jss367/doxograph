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


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_an_editor_keeps_its_draft_when_a_topic_was_renamed(engine):
    store.add_tag("old")
    _paper("paper", "A paper", "old")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            try:
                page = await browser.new_page()
                with _server() as url:
                    await page.goto(url)
                    await page.locator('[data-act="edit"][data-claim="paper-c1"]').click()
                    form = page.locator('form[data-form="paper-c1"]')
                    await form.locator('[name="text"]').fill("My unsaved wording")
                    store.rename_tag("old", "new")
                    await form.get_by_role("button", name="Save", exact=True).click()
                    await expect(page.locator('#content > .warn')).to_contain_text("Update the topic list")
                    await expect(form.locator('[name="text"]')).to_have_value("My unsaved wording")
                    await expect(form.locator('[name="tags"]')).to_have_value("old")
                    assert store.load_paper("paper")["claims"][0]["tags"] == ["new"]
                    await form.locator('[name="tags"]').fill("new")
                    await form.get_by_role("button", name="Save", exact=True).click()
                    await expect(form).to_have_count(0)
                    assert store.load_paper("paper")["claims"][0]["text"] == "My unsaved wording"
            finally:
                await browser.close()

    asyncio.run(scenario())
