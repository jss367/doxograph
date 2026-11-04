"""Beside a paper: its panel first, its claims in a drawer that opens when
something sends the reader to a claim."""
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, datasets, store
from pdfs import minimal_pdf
from test_browser import _paper, _server


def _corpus():
    config.set_ai_enabled(False)
    store.add_tag("recovery", "How often a model returns to task.")
    _paper("paper-a", "Paper A", "recovery", year=2026)
    _paper("paper-b", "Paper B", "recovery", year=2025)
    store.pdf_path("paper-a").write_bytes(minimal_pdf(["A passage about recovery."]))
    live = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [{"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension",
                                        "note": "A and B disagree on the rate. The sweeps differ."}], live)
    dataset = datasets.create_dataset(datasets.DatasetBody(name="STRIDE", roles=["evaluation"], papers=["paper-a"]))
    return store.tension_rows()[0]["id"], dataset["id"]


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_paper_opens_on_its_panel_with_the_claims_in_a_closed_drawer(engine):
    _corpus()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper-a"]').click()
                await expect(page.locator(".paper-overview")).to_be_visible()
                drawer = page.locator("#claims-drawer")
                await expect(drawer).to_be_visible()
                assert await drawer.get_attribute("open") is None
                await expect(drawer.locator("summary")).to_contain_text("1 claim")
                await expect(drawer.locator("summary")).to_contain_text("in 1 finding")
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_hidden()
                await expect(page.locator("#btn-reader-claims")).to_have_text("Hide panel")
                await drawer.locator("summary").click()
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_visible()
                # Remembered for this paper, not for the next.
                # Without a PDF on screen the claims are the thing to read, so
                # Paper B's drawer starts open.
                await page.goto(f"{url}/#view=claims&paper=paper-b")
                await expect(page.locator("#paperhead h2")).to_have_text("Paper B")
                await expect(page.locator('#content .claim[data-claim="paper-b-c1"]')).to_be_visible()
                await page.locator("#claims-drawer summary").click()
                await expect(page.locator('#content .claim[data-claim="paper-b-c1"]')).to_be_hidden()
                await page.goto(f"{url}/#view=claims&paper=paper-a")
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_visible()

    run_browser(scenario())


@pytest.mark.browser
def test_a_link_to_a_claim_the_keyboard_and_a_new_claim_open_the_drawer():
    _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            with _server() as url:
                await page.goto(f"{url}/#view=claims&paper=paper-a&sel=paper-a-c1")
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_visible()
                await page.locator("#claims-drawer summary").click()
                await page.goto(f"{url}/#view=claims&paper=paper-b")
                await expect(page.locator("#paperhead h2")).to_have_text("Paper B")
                await page.goto(f"{url}/#view=claims&paper=paper-a")
                await expect(page.locator("#claims-drawer")).to_be_visible()
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_hidden()
                await page.keyboard.press("e")
                await expect(page.locator('form[data-form="paper-a-c1"]')).to_be_visible()
                await page.keyboard.press("Escape")
                await page.locator("#claims-drawer summary").click()
                await expect(page.locator('#content .claim[data-claim="paper-a-c1"]')).to_be_hidden()
                await page.locator(".paper-actions > summary").click()
                await page.locator('[data-act="add-claim"]').click()
                await expect(page.locator('#claims-drawer form[data-form="__new__"]')).to_be_visible()

    run_browser(scenario())


@pytest.mark.browser
def test_the_panel_lists_the_findings_and_artifacts_a_paper_is_part_of():
    tid, did = _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            with _server() as url:
                await page.goto(f"{url}/#view=claims&paper=paper-a")
                finding = page.locator(f'.paper-findings [data-open-finding="{tid}"]')
                await expect(finding).to_contain_text("A and B disagree on the rate.")
                artifact = page.locator(f'.paper-artifacts [data-open-dataset="{did}"]')
                await expect(artifact).to_have_text("STRIDE")
                await expect(page.locator(".paper-artifacts")).to_contain_text("evaluation")
                await finding.click()
                await page.wait_for_url("**/#view=findings")
                row = page.locator(f'.frow[data-finding="{tid}"]')
                assert await row.get_attribute("open") is not None
                await expect(row.locator(".claim").first).to_be_visible()
                await page.go_back()
                await expect(page.locator(".paper-overview")).to_be_visible()
                await page.locator(f'.paper-artifacts [data-open-dataset="{did}"]').click()
                await page.wait_for_function(f"V.view === 'datasets' && V.dataset === '{did}'")
                await expect(page.locator("#datasets-sheet")).to_be_visible()

    run_browser(scenario())
