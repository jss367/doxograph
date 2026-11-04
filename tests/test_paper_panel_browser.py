"""Beside a paper: the findings its claims feed and the artifacts it links,
each opening the page it came from."""
import asyncio

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
    _paper("paper-c", "Paper C", "recovery", year=2024)
    store.pdf_path("paper-a").write_bytes(minimal_pdf(["A passage about recovery."]))
    live = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [{"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension",
                                        "note": "A and B disagree on the rate. The sweeps differ."}], live)
    store.record_agreements("recovery", [{"claims": ["paper-a-c1", "paper-c-c1"],
                                          "note": "A and C agree on the trend."}], live)
    store.set_agreement_status(store.agreement_rows()[0]["id"], "dismissed")
    linked = datasets.create_dataset(datasets.DatasetBody(name="STRIDE", roles=["evaluation"], papers=["paper-a"]))
    # Linked only through one of the paper's claims, as the record's Linked research counts it.
    through_claim = datasets.create_dataset(datasets.DatasetBody(
        name="Claimset", claims=[{"paper": "paper-a", "claim": "paper-a-c1"}]))
    # And one only a dataset facet on the paper's claim names.
    paper = store.load_paper("paper-a")
    paper["claims"][0]["facets"] = {"dataset": ["Facetset"]}
    store.save_paper(paper)
    datasets.create_dataset(datasets.DatasetBody(name="Facetset"))
    datasets.create_dataset(datasets.DatasetBody(name="Elsewhere", papers=["paper-b"]))
    return store.tension_rows()[0]["id"], linked["id"], through_claim["id"]


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_the_panel_lists_the_findings_and_artifacts_a_paper_is_part_of(engine):
    tid, did, claim_did = _corpus()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            with _server() as url:
                await page.goto(f"{url}/#view=claims&paper=paper-a")
                findings = page.locator(".paper-findings li")
                # The dismissed agreement is left out, as the drawer's handle leaves it out.
                await expect(findings).to_have_count(1)
                finding = page.locator(f'.paper-findings [data-finding-id="{tid}"]')
                await expect(finding).to_contain_text("A and B disagree on the rate.")
                await expect(page.locator("#claims-drawer-bar")).to_contain_text("in 1 tension")
                artifacts = page.locator(".paper-artifacts [data-act='open-dataset']")
                await expect(artifacts).to_have_text(["Claimset", "Facetset", "STRIDE"])
                await expect(page.locator(".paper-artifacts")).to_contain_text("evaluation")
                await finding.click()
                await page.wait_for_function("V.view === 'findings'")
                row = page.locator(f'.frow[data-finding="{tid}"]')
                assert await row.get_attribute("open") is not None
                await page.go_back()
                await expect(page.locator(".paper-overview")).to_be_visible()
                await page.locator(f'.paper-artifacts [data-dataset-id="{claim_did}"]').click()
                await page.wait_for_function(f"V.view === 'datasets' && V.dataset === '{claim_did}'")
                await expect(page.locator("#datasets-sheet")).to_be_visible()
                await expect(page.locator("#dataset-name")).to_have_value("Claimset")
                assert did

    run_browser(scenario())


@pytest.mark.browser
def test_a_paper_opened_while_the_datasets_load_still_lists_its_artifacts():
    _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page(viewport={"width": 1500, "height": 1000})
            release = asyncio.Event()

            async def held(route):
                await release.wait()
                await route.continue_()

            await page.route("**/api/datasets", held)
            with _server() as url:
                await page.goto(f"{url}/#view=claims&paper=paper-a")
                await expect(page.locator("#paperhead h2")).to_have_text("Paper A")
                await page.locator('#papers [data-paper="paper-b"]').click()
                await expect(page.locator("#paperhead h2")).to_have_text("Paper B")
                release.set()
                await expect(page.locator(".paper-artifacts [data-act='open-dataset']")).to_have_text(["Elsewhere"])

    run_browser(scenario())
