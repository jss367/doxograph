"""The findings page in a real browser: rows, their claims, and decisions."""

from __future__ import annotations

import pytest

from browser_support import browser_session, run_browser
from doxograph import store
from test_browser import _paper, _server


def _corpus():
    store.add_tag("recovery", "How often a model returns to task.")
    store.add_tag("scaling", "")
    _paper("paper-a", "Paper A", "recovery", "scaling", year=2026)
    _paper("paper-b", "Paper B", "recovery", year=2025)
    _paper("paper-c", "Paper C", "scaling", year=2024)
    live = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [
        {"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension",
         "note": "A and B disagree on the rate. The sweep ranges differ."},
    ], live)
    store.record_agreements("scaling", [
        {"claims": ["paper-a-c1", "paper-c-c1"], "note": "A and C both find no scale effect."},
    ], live)
    store.record_synthesis("recovery", "Recovery is rare under steering [paper-a-c1]. Nobody tests long responses.",
                           {k: v for k, v in live.items() if "recovery" in v["tags"]}, store.tension_rows())
    return store.tension_rows()[0]["id"], store.agreement_rows()[0]["id"]


@pytest.mark.browser
def test_findings_shows_a_section_per_topic_with_rows_that_open_onto_their_claims():
    tid, aid = _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=library")
                await page.locator("#btn-findings").click()
                await page.wait_for_url("**/#view=findings")
                sections = page.locator(".ftopic")
                await sections.first.wait_for(state="visible")
                # Vocabulary order: recovery before scaling.
                assert await sections.evaluate_all("els => els.map(e => e.dataset.topic)") == ["recovery", "scaling"]
                recovery = sections.nth(0)
                assert await recovery.locator(".pill").text_content() == "current"
                assert await recovery.locator(".fsynth .flead").text_content() == "Recovery is rare under steering."
                assert await recovery.locator(".fsynth .synth").is_visible() is False
                await recovery.locator(".fsynth > summary").click()
                assert await recovery.locator(".fsynth .synth").is_visible()

                row = page.locator(f'.frow[data-finding="{tid}"]')
                assert await row.locator(".fsent").text_content() == "A and B disagree on the rate."
                assert await row.locator(".fwho").text_content() == "paper-a 2026 · paper-b 2025"
                assert await row.locator(".claim").first.is_visible() is False
                await row.locator("summary").click()
                assert await row.locator(".claim").count() == 2
                assert await row.locator(".fnote").text_content() == "A and B disagree on the rate. The sweep ranges differ."

                # The scaling section has no synthesis and offers to write one.
                scaling = sections.nth(1)
                assert await scaling.locator(".flead.empty").count() == 1
                assert await scaling.locator(f'.frow[data-finding="{aid}"] .ftag').text_content() == "agreement"

    run_browser(scenario())


@pytest.mark.browser
def test_a_finding_can_be_confirmed_from_its_row_and_stays_open_across_the_redraw():
    tid, _ = _corpus()

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=findings")
                row = page.locator(f'.frow[data-finding="{tid}"]')
                await row.locator("summary").click()
                await row.get_by_role("button", name="Confirm").click()
                confirmed = page.locator(f'.frow[data-finding="{tid}"].confirmed')
                await confirmed.wait_for(state="visible")
                assert await confirmed.get_attribute("open") is not None
                assert await confirmed.locator("summary .st").text_content() == "confirmed"
                assert await confirmed.get_by_role("button", name="Reopen").count() == 1

    run_browser(scenario())


@pytest.mark.browser
def test_a_topic_reads_stale_once_a_claim_under_it_changes():
    tid, _ = _corpus()
    store.update_claim("paper-a", "paper-a-c1", {"text": "A reworded claim from Paper A."})

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=findings")
                recovery = page.locator('.ftopic[data-topic="recovery"]')
                await recovery.wait_for(state="visible")
                assert await recovery.locator(".pill").text_content() == "stale"
                assert await page.locator('.ftoc .chip[data-topic="recovery"]').get_attribute("class") == "chip is-stale"
                assert await page.locator(f'.frow[data-finding="{tid}"]').get_attribute("class") == "frow tension open is-stale"

    run_browser(scenario())


@pytest.mark.browser
def test_update_findings_posts_one_request_and_reports_an_empty_corpus():
    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                posted = []

                async def record(route, request):
                    posted.append(request.url)
                    await route.fulfill(json={"queued": 0})

                await page.route("**/api/findings", record)
                await page.goto(f"{url}/#view=findings")
                await page.wait_for_function("typeof S !== 'undefined' && S.workspace")
                await page.evaluate("S.ai_enabled = true; renderAll();")
                await page.locator('[data-act="update-findings"]').click()
                warning = page.locator("#content .warn")
                await warning.wait_for(state="visible")
                assert "nothing to compare" in (await warning.text_content())
                assert len(posted) == 1

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("cited", [False, True], ids=["uncited-change", "cited-change"])
def test_no_synthesis_status_names_existing_findings(engine, cited):
    from playwright.async_api import expect

    _, aid = _corpus()
    _paper("paper-d", "Uncited paper", "scaling", year=2023)
    paper = "paper-a" if cited else "paper-d"
    store.update_claim(paper, paper + "-c1", {"text": "Edited claim under this topic."})
    assert next(row for row in store.agreement_rows() if row["id"] == aid)["stale"] is cited

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + "/#view=findings")
                section = page.locator('.ftopic[data-topic="scaling"]')
                await expect(section.locator(".flead.empty")).to_contain_text("No synthesis yet.")
                await expect(section.locator(".fsynth")).to_have_count(0)
                await expect(section.locator(".pill")).to_have_text("stale" if cited else "findings current")
                if not cited:
                    await expect(section.locator(".pill")).to_have_attribute("title", "Existing findings reflect the claims they cite.")
                    await expect(section.locator(".frow.is-stale")).to_have_count(0)
                else:
                    await expect(section.locator(".frow.is-stale")).to_have_count(1)

    run_browser(scenario())
