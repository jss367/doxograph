"""Library controls stay discoverable without competing with research content."""

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _paper, _poll, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 390])
def test_library_menus_selection_and_keyboard(engine, width):
    config.set_ai_enabled(False)
    _paper("oversight", "Understanding model oversight", "oversight")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": width, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await expect(page.locator(".library-heading h2")).to_have_text("Research library")
                await expect(page.locator('[data-tool="compare"]')).to_be_hidden()
                await expect(page.locator('[data-tool="clear-selection"]')).to_be_hidden()

                # Workspace actions remain keyboard accessible in their disclosure.
                more = page.locator("#workspace-actions > summary")
                await more.focus()
                await page.keyboard.press("Enter")
                await expect(page.locator("#btn-workspace-rename")).to_be_visible()
                await page.locator("#btn-help").click()
                await expect(page.locator("#help")).to_be_visible()
                await expect(page.locator("#workspace-actions")).not_to_have_attribute("open", "")
                await page.keyboard.press("Escape")

                # Dismissing filters leaves a separate writing draft intact.
                await page.locator("#workspace-notes").fill("A question about oversight.")
                await expect(page.locator("#workspace-notes-status")).to_have_text("Saved")
                filters = page.locator("#claim-filter-options > summary")
                await filters.click()
                await page.locator("#only-unreviewed").check()
                await page.locator("#only-unreviewed").focus()
                await _poll(page)
                await expect(page.locator("#only-unreviewed")).to_be_focused()
                await page.keyboard.press("Escape")
                await expect(filters).to_be_focused()
                await expect(page.locator("#claim-filter-options")).not_to_have_attribute("open", "")
                await expect(page.locator("#active-filters")).to_contain_text("Unreviewed")
                await expect(page.locator("#workspace-notes")).to_have_value("A question about oversight.")
                await page.get_by_role("button", name="Clear all", exact=True).click()

                # Selecting through either list reveals the bulk workflow.
                await page.locator('#content [data-select-paper="oversight"]').check()
                await expect(page.locator('[data-tool="compare"]')).to_be_visible()
                await page.locator('[data-tool="clear-selection"]').click()
                await expect(page.locator('[data-tool="compare"]')).to_be_hidden()
                await page.locator('[data-tool="select-visible"]').click()
                await expect(page.locator("#selection-count")).to_have_text("0 claims · 1 papers selected")
                await page.locator('[data-tool="compare"]').click()
                await expect(page.locator("#research-tools")).to_be_visible()
                await expect(page.locator(".comparison-card")).to_contain_text("Understanding model oversight")
                await page.get_by_role("button", name="Close research tools").click()

                # Both menus fit narrow windows, including with a dark palette.
                await page.locator("#btn-settings").click()
                await page.locator('input[name="appearance"][value="dark"]').check()
                await page.locator("#btn-close-settings").click()
                for summary in [more, filters]:
                    await summary.click()
                    box = await summary.locator("..").locator(".action-popover").bounding_box()
                    assert box["x"] >= 0
                    assert box["x"] + box["width"] <= width
                    await page.keyboard.press("Escape")
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
def test_empty_library_hides_selection_until_papers_arrive():
    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await expect(page.locator("#workspace-notes-panel")).to_be_visible()
                await expect(page.locator("#selection-tools")).to_be_hidden()
                _paper("first", "The first paper")
                await _poll(page)
                await expect(page.locator("#selection-tools")).to_be_visible()
                await expect(page.locator("#btn-focus-review")).to_be_hidden()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_filter_changes_update_claim_controls_without_polling(engine):
    _paper("reviewed", "A reviewed finding", "oversight")
    _paper("unreviewed", "An unreviewed method", "methods")
    store.update_claim("unreviewed", "unreviewed-c1", {"reviewed": False, "kind": "method"})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=claims")
                await expect(page.locator("#selection-count")).to_have_text("2 claims")
                await page.evaluate("poller.stop()")
                count = page.locator("#selection-count")
                select = page.locator('[data-tool="select-visible"]')
                review = page.locator("#btn-focus-review")
                await page.locator("#q").fill("no matching claims")
                await expect(count).to_have_text("0 claims")
                await expect(select).to_be_hidden()
                await expect(review).to_be_hidden()
                await page.locator("#q").fill("")
                await expect(count).to_have_text("2 claims")
                await expect(select).to_be_visible()
                await expect(review).to_be_visible()

                await page.locator("#claim-filter-options > summary").click()
                await page.locator("#kind").select_option("finding")
                await expect(count).to_have_text("1 claim")
                await page.locator("#only-unreviewed").check()
                await expect(count).to_have_text("0 claims")
                await expect(review).to_be_hidden()
                await page.locator("#kind").select_option("")
                await expect(count).to_have_text("1 claim")
                await expect(review).to_be_visible()
                await page.locator("#btn-close-filters").click()
                await page.get_by_role("button", name="Clear all", exact=True).click()
                await expect(count).to_have_text("2 claims")
                await select.click()
                await page.locator("#q").fill("no matching claims")
                await expect(count).to_have_text("2 claims · 0 papers selected")
                await expect(review).to_be_hidden()
                await expect(page.locator('[data-tool="compare"]')).to_be_visible()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_sidebar_groups_preserve_drafts_and_import_remains_discoverable(engine):
    _paper("oversight", "Understanding model oversight")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            with _server() as url:
                await page.goto(url)
                library = page.locator('[data-sidebar-group="library"]')
                review = page.locator('[data-sidebar-group="review"]')
                write = page.locator('[data-sidebar-group="write"]')
                await expect(page.locator("#paper-import")).not_to_have_attribute("open", "")
                await page.locator("#workspace-notes").fill("Keep this research question.")
                await expect(page.locator("#workspace-notes-status")).to_have_text("Saved")
                await write.locator(":scope > summary").click()
                await review.locator(":scope > summary").click()
                await _poll(page)
                await expect(write).not_to_have_attribute("open", "")
                await expect(review).not_to_have_attribute("open", "")
                await page.reload()
                await expect(write).not_to_have_attribute("open", "")
                await expect(review).not_to_have_attribute("open", "")
                await expect(page.locator("#workspace-notes")).to_have_value("Keep this research question.")
                await library.locator(":scope > summary").click()
                await page.locator("#btn-command-palette").click()
                await page.locator("#command-query").fill("Add papers")
                await page.keyboard.press("Enter")
                await expect(library).to_have_attribute("open", "")
                await expect(page.locator("#refs")).to_be_focused()
                await page.locator("#refs").fill("A reference I am still typing")
                await _poll(page)
                await expect(page.locator("#refs")).to_have_value("A reference I am still typing")
                await page.locator('[data-sidebar-focus="write"]').click()
                await expect(write).to_have_attribute("open", "")
                await expect(library).not_to_have_attribute("open", "")
                await expect(page.locator('[data-sidebar-focus="write"]')).to_have_attribute("aria-pressed", "true")
                await expect(page.locator("#workspace-notes")).to_have_value("Keep this research question.")
                await page.locator('[data-sidebar-focus="library"]').click()
                await expect(page.locator("#refs")).to_have_value("A reference I am still typing")

    run_browser(scenario())
