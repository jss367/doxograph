"""The Ask pane stays beside the library: it survives a reload, resizes, and
follows what the reader is looking at."""
import re

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import store
from test_ask_browser import _server, seed


def _tension():
    live = {r["id"]: r for r in store.claim_rows()}
    a = next(c for c in live if c.startswith("a-"))
    b = next(c for c in live if c.startswith("b-"))
    store.record_tensions("recovery", [{"claims": [a, b], "kind": "tension",
                                        "note": "A and B disagree on the rate."}], live)
    return store.tension_rows()[0]["id"]


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_the_pane_comes_back_after_a_reload_and_stays_closed_once_closed(engine):
    seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                await expect(page.locator("#library-agent")).to_be_visible()
                assert await page.evaluate("localStorage.getItem('doxograph-ask-open')") == "1"
                await page.reload()
                await expect(page.locator("#library-agent")).to_be_visible()
                # Coming back does not take the keyboard from the page.
                assert await page.evaluate("document.activeElement === document.body") is True
                await page.locator("#agent-close").click()
                await expect(page.locator("#library-agent")).to_be_hidden()
                await page.reload()
                await page.locator("#btn-agent:enabled").wait_for()
                await expect(page.locator("#library-agent")).to_be_hidden()

    run_browser(scenario())


@pytest.mark.browser
def test_the_divider_resizes_the_pane_and_the_width_is_remembered():
    seed()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                handle = page.locator("#ask-resize")
                await expect(handle).to_be_visible()
                before = (await page.locator("#library-agent").bounding_box())["width"]
                await handle.focus()
                await page.keyboard.press("Shift+ArrowLeft")
                after = (await page.locator("#library-agent").bounding_box())["width"]
                assert after == pytest.approx(before + 48, abs=1)
                assert await page.evaluate("localStorage.getItem('doxograph-ask-width')") == str(round(after))
                await page.reload()
                await expect(page.locator("#library-agent")).to_be_visible()
                assert (await page.locator("#library-agent").bounding_box())["width"] == pytest.approx(after, abs=1)
                await page.locator("#ask-resize").focus()
                await page.keyboard.press("Home")
                assert (await page.locator("#library-agent").bounding_box())["width"] == pytest.approx(before, abs=1)
                # Docked below the workspace, there is no width to drag.
                await page.set_viewport_size({"width": 700, "height": 1000})
                await expect(page.locator("body")).to_have_class(re.compile(r"agent-docked"))
                await expect(page.locator("#ask-resize")).to_be_hidden()

    run_browser(scenario())


@pytest.mark.browser
def test_the_context_chip_follows_the_reader_and_switches_the_sources():
    seed()
    _tension()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                chip = page.locator("#agent-context .chip")
                await expect(chip).to_have_text("Whole library")
                await expect(chip).to_have_class(re.compile(r"\bon\b"))
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator("#reader")).to_be_visible()
                await expect(chip).to_have_text("Memory and recovery")
                await expect(chip).not_to_have_class(re.compile(r"\bon\b"))
                await chip.click()
                await expect(chip).to_have_class(re.compile(r"\bon\b"))
                await expect(page.locator("#agent-scope")).to_contain_text("Memory and recovery")
                assert await page.evaluate("activeAgentChat().scope.mode") == "paper"
                await page.locator("#btn-findings").click()
                await expect(chip).to_have_text("Findings")
                await expect(chip).not_to_have_class(re.compile(r"\bon\b"))

    run_browser(scenario())


@pytest.mark.browser
def test_a_finding_row_opens_ask_with_both_papers_and_the_claims_in_focus():
    seed()
    tid = _tension()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1600, "height": 1000})
            with _server() as url:
                await page.goto(f"{url}/#view=findings")
                row = page.locator(f'.frow[data-finding="{tid}"]')
                await row.locator("summary").click()
                await row.locator("[data-ask-finding]").click()
                await expect(page.locator("#library-agent")).to_be_visible()
                scope = page.locator("#agent-scope")
                await expect(scope).to_contain_text("Memory and recovery")
                await expect(scope).to_contain_text("Recovery under intervention")
                assert await page.evaluate("activeAgentChat().scope.mode") == "selected"
                assert await page.evaluate("activeAgentChat().scope.focus.length") == 2
                await expect(page.locator("#agent-starters")).to_contain_text("What would reconcile these?")
                # Closing puts the keyboard back on the row's button.
                await page.locator("#agent-close").click()
                assert await page.evaluate("document.activeElement?.dataset.askFinding") == tid

    run_browser(scenario())
