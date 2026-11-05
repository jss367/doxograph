"""Links made for other documents land on what they name, and can be copied."""

from pathlib import Path

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, export, store
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_permalinks_open_their_record_and_copy_from_the_page(engine):
    config.set_ai_enabled(False)
    store.add_tag("recovery")
    store.save_ledger([{"id": "L1", "text": "Recovery is path dependent."}])
    _paper("one", "Linked paper", "recovery")
    _paper("two", "Unlinked paper", "recovery")
    _paper("three", "Other paper", "recovery")
    store.update_claim("one", "one-c1", {"ledger_links": [{"claim": "L1", "relation": "supports"}]})
    for n in range(30):
        store.add_claim("three", {"text": f"Further claim {n} from the other paper.", "tags": ["recovery"]})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await page.add_init_script(
                "Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => { window.copied = text; }}})")
            chips = page.get_by_role("group", name="Active filters", exact=True)
            with _server() as url:
                # Another workspace remembered in this browser must not win over the link.
                await page.goto(f"{url}/")
                await page.evaluate("localStorage.setItem('doxograph-workspace', 'elsewhere')")

                await page.goto(f"{url}/claim/three-c31")
                selected = page.locator("#content .claim.sel")
                await expect(selected).to_have_attribute("data-claim", "three-c31")
                await expect(selected).to_be_in_viewport()
                assert "paper=three" in page.url

                await page.goto(f"{url}/ledger/L1")
                await expect(page.locator("#content .claim")).to_have_count(1)
                await expect(page.locator("#content .claim")).to_have_attribute("data-claim", "one-c1")
                await expect(chips.get_by_role(
                    "button", name="Remove filter: Bears on L1: Recovery is path dependent.", exact=True)).to_be_visible()
                if engine == "chromium":
                    Path(".context").mkdir(exist_ok=True)
                    await page.screenshot(path=".context/ledger-permalink.png")

                await page.locator("#content .claim [data-act='copy-link']").click()
                assert await page.evaluate("window.copied") == f"{url}/claim/one-c1"

                # A paper with no evidence on L1, chosen from the command
                # palette, opens with its claims.
                await page.keyboard.press("Control+k")
                await page.get_by_role("combobox", name="Search papers, topics, and actions").fill("Unlinked")
                await page.keyboard.press("Enter")
                await expect(page.locator("#content .claim")).to_have_attribute("data-claim", "two-c1")
                assert await page.evaluate("V.ledger") is None

                await page.goto(f"{url}/paper/two")
                await page.locator(".paper-actions summary").click()
                await page.locator(".paper-actions [data-act='copy-link']").click()
                assert await page.evaluate("window.copied") == f"{url}/paper/two"

                await page.goto(f"{url}/#view=research")
                await page.locator("[data-ledger-row] [data-act='copy-link']").click()
                assert await page.evaluate("window.copied") == f"{url}/ledger/L1"
            assert errors == []

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_link_into_the_export_clears_filters_that_hide_its_target(engine, tmp_path):
    store.add_tag("recovery")
    _paper("one", "First paper", "recovery")
    _paper("two", "Second paper", "recovery")
    out = export.write(tmp_path / "doxograph.html")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await page.goto(out.as_uri() + "#paper-one")
            await expect(page.locator("#paper-one")).to_be_in_viewport()
            await page.locator("#q").fill("First paper")
            await expect(page.locator("#claim-two-c1")).to_be_hidden()
            await page.evaluate("location.hash = '#claim-two-c1'")
            await expect(page.locator("#claim-two-c1")).to_be_in_viewport()
            await expect(page.locator("#q")).to_have_value("")

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_new_claim_of_my_own_skips_retired_ids(engine):
    store.save_ledger([{"id": "L1", "text": "Removed."}, {"id": "L2", "text": "Kept."}])
    store.save_ledger([{"id": "L2", "text": "Kept."}])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=research")
                await page.get_by_role("button", name="Add a claim", exact=True).click()
                await expect(page.locator('[name="ledger-id"]').last).to_have_value("L3")

    run_browser(scenario())
