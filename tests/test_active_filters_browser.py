"""Active filters stay removable when they hide every result."""

import asyncio
from pathlib import Path
from urllib.parse import urlencode

import pytest
from playwright.async_api import async_playwright, expect

from doxograph import config, store
from test_browser import _paper, _polled, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_clear_active_filters(engine):
    config.set_ai_enabled(False)
    title = 'Alignment <study> & "evidence"'
    _paper("one", title, "alignment")
    _paper("two", "Another paper", "methods")
    store.add_tag("alignment")
    store.add_tag("methods")
    store.set_labels("one", ["reading-list"])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                params = urlencode(dict(paper="one", tag="alignment", label="reading-list",
                                        q="no matching phrase", kind="finding", unreviewed="1",
                                        unverified="1", group="0"))
                await page.goto(f"{url}/#{params}")
                chips = page.get_by_role("group", name="Active filters", exact=True)
                await expect(chips.locator(".filter-chip")).to_have_count(7)
                await expect(page.locator("#content .empty")).to_have_text("No claims match these filters.")
                await expect(chips.get_by_role("button", name=f"Remove filter: Paper: {title}", exact=True)).to_be_visible()
                await expect(chips.get_by_role("button", name="Remove filter: Topic: alignment", exact=True)).to_be_visible()
                if engine == "chromium":
                    Path(".context").mkdir(exist_ok=True)
                    await page.screenshot(path=".context/active-filters.png")

                # Remove just one filter with the keyboard, keeping every other one.
                remove_unreviewed = chips.get_by_role("button", name="Remove filter: Unreviewed", exact=True)
                await remove_unreviewed.focus()
                await _polled(page, rounds=1)
                await expect(remove_unreviewed).to_be_focused()
                await page.keyboard.press("Enter")
                await expect(chips.locator(".filter-chip")).to_have_count(6)
                await expect(page.locator("#only-unreviewed")).not_to_be_checked()
                await expect(page.locator("#only-unverified")).to_be_checked()
                assert "unreviewed=" not in page.url
                assert "tag=alignment" in page.url
                assert await page.evaluate("document.activeElement.closest('#active-filters') !== null")

                await chips.get_by_role("button", name="Clear all", exact=True).click()
                await expect(chips).to_be_hidden()
                await expect(page.locator("#content .claim")).to_have_count(2)
                await expect(page.locator("#papers [data-paper='one'].active")).to_have_count(0)
                await expect(page.locator("#q")).to_have_value("")
                await expect(page.locator("#q")).to_be_focused()
                await expect(page.locator("#kind")).to_have_value("")
                await expect(page.locator("#only-unverified")).not_to_be_checked()
                await expect(page.locator("#group-by-tag")).not_to_be_checked()
                assert await page.evaluate("Object.keys(FILTER_DEFAULTS).every(k => V[k] === FILTER_DEFAULTS[k])")
                await page.reload()
                await expect(page.locator("#content .claim")).to_have_count(2)
                await expect(chips).to_be_hidden()

                # Clearing paper scope preserves text typed in an open editor.
                await page.goto(f"{url}/#paper=one&tag=alignment&group=0")
                await page.locator("[data-act='edit']").first.click()
                draft = page.locator("form[data-form='one-c1'] textarea[name='text']")
                await draft.fill("Unsaved changes survive clearing filters.")
                await chips.get_by_role("button", name=f"Remove filter: Paper: {title}", exact=True).click()
                await expect(draft).to_have_value("Unsaved changes survive clearing filters.")
                await expect(chips.locator(".filter-chip")).to_have_count(1)
                await expect(page.locator("#tags .active")).to_have_count(1)
                await chips.get_by_role("button", name="Clear all", exact=True).click()
                await expect(draft).to_have_value("Unsaved changes survive clearing filters.")

                # Other views expose their own status filter as well.
                for view, status in [("tensions", "tstatus"), ("agreements", "astatus")]:
                    await page.goto(f"{url}/#view={view}&{status}=confirmed&tag=alignment")
                    await expect(chips.locator(".filter-chip")).to_have_count(2)
                    await chips.get_by_role("button", name="Remove filter: Status: confirmed", exact=True).click()
                    await expect(page.locator(f"#{view[:-1]}-status")).to_have_value("")
                    await chips.get_by_role("button", name="Clear all", exact=True).click()
                    await expect(chips).to_be_hidden()
                    assert f"view={view}" in page.url
                assert not errors
            await browser.close()

    asyncio.run(scenario())
