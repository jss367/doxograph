"""Three pages in the header, and the findings as the place to land."""
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _paper, _server


def _corpus(with_findings=True):
    config.set_ai_enabled(False)
    store.add_tag("recovery", "How often a model returns to task.")
    _paper("paper-a", "Paper A", "recovery", year=2026)
    _paper("paper-b", "Paper B", "recovery", year=2025)
    if with_findings:
        live = {r["id"]: r for r in store.claim_rows()}
        store.record_tensions("recovery", [{"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension", "note": "n."}], live)


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_the_header_pages_open_their_views_and_mark_the_current_one(engine):
    _corpus()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            with _server() as url:
                await page.goto(f"{url}/#view=library")
                await expect(page.locator("#btn-papers")).to_have_attribute("aria-current", "page")
                await page.locator("#btn-workspace").click()
                await page.wait_for_url("**/#view=research")
                await expect(page.locator("#btn-workspace")).to_have_attribute("aria-current", "page")
                await expect(page.locator("#content .paperhead h2")).to_have_text("Workspace")
                await expect(page.locator("#research-form")).to_be_visible()
                await expect(page.locator(".library-heading")).to_be_hidden()
                await expect(page.locator('.topic-list li[data-open-topic="recovery"]')).to_contain_text("recovery")
                await expect(page.locator('.actions-row [data-reader-command="btn-export"]')).to_be_visible()
                await page.locator("#btn-findings").click()
                await expect(page.locator("#btn-findings")).to_have_attribute("aria-current", "page")
                await expect(page.locator("#btn-workspace")).not_to_have_attribute("aria-current", "page")
                await page.locator("#btn-papers").click()
                await page.wait_for_url("**/#view=library")
                await expect(page.locator("#btn-papers")).to_have_attribute("aria-current", "page")
                # Reading a paper is still the Papers page.
                await page.locator('#papers li[data-paper="paper-a"]').click()
                await expect(page.locator("#btn-papers")).to_have_attribute("aria-current", "page")
                # A topic on the Workspace page opens the library on it.
                await page.locator("#btn-workspace").click()
                await page.locator('.topic-list li[data-open-topic="recovery"]').click()
                await page.wait_for_url("**/#view=claims&tag=recovery*")

    run_browser(scenario())


@pytest.mark.browser
def test_an_address_naming_no_page_lands_on_the_findings_once_there_are_any():
    _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.wait_for_url("**/#view=findings")
                await expect(page.locator(".ftopic").first).to_be_visible()
                # A page named in the address is respected.
                await page.goto(f"{url}/#view=library")
                await page.reload()
                await expect(page.locator("#btn-papers")).to_have_attribute("aria-current", "page")
                await expect(page.locator(".library-heading")).to_be_visible()

    run_browser(scenario())


@pytest.mark.browser
def test_a_workspace_without_findings_still_lands_on_the_papers():
    _corpus(with_findings=False)

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1400, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.wait_for_url("**/#view=library")
                await expect(page.locator("#btn-papers")).to_have_attribute("aria-current", "page")

    run_browser(scenario())
