"""A paper's summary and why it matters are the model's first draft.

The model writes both when it reads the paper, and the reader can rewrite
either from the paper's overview. A re-read replaces the model's text and
keeps the reader's.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from browser_support import browser_session, run_browser
from doxograph import extract, server, store

KEY = "doe2026recovery"


def payload(summary: str, relevance: str = "Relevant.") -> dict:
    return {"summary": summary, "relevance": relevance, "claims": [], "proposed_tags": []}


def client() -> TestClient:
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def setup_paper() -> None:
    store.save_paper(store.new_paper(KEY, title="Recovery under steering"))


def test_a_reread_replaces_the_models_summary():
    setup_paper()
    extract.merge_extraction(KEY, payload("First read."))
    paper = extract.merge_extraction(KEY, payload("Second read."))
    assert paper["summary"] == "Second read."
    assert paper["extraction"]["summary"] == "Second read."


def test_a_reread_keeps_the_readers_summary_and_relevance():
    setup_paper()
    extract.merge_extraction(KEY, payload("First read.", "Model relevance."))
    client().patch(f"/api/papers/{KEY}", json={"summary": "My summary.", "relevance": "My relevance."}).raise_for_status()

    paper = extract.merge_extraction(KEY, payload("Second read.", "New relevance."))
    assert (paper["summary"], paper["relevance"]) == ("My summary.", "My relevance.")
    # The model's text is still recorded, so undoing the edit hands it back.
    assert paper["extraction"]["summary"] == "Second read."


def test_a_summary_restored_to_the_models_text_is_the_models_again():
    setup_paper()
    extract.merge_extraction(KEY, payload("First read."))
    client().patch(f"/api/papers/{KEY}", json={"summary": "My summary."}).raise_for_status()
    client().patch(f"/api/papers/{KEY}", json={"summary": "First read."}).raise_for_status()
    assert extract.merge_extraction(KEY, payload("Second read."))["summary"] == "Second read."


def test_a_cleared_summary_is_written_again():
    setup_paper()
    extract.merge_extraction(KEY, payload("First read."))
    client().patch(f"/api/papers/{KEY}", json={"summary": ""}).raise_for_status()
    assert extract.merge_extraction(KEY, payload("Second read."))["summary"] == "Second read."


def test_a_paper_read_before_the_models_text_was_recorded_is_rewritten():
    paper = store.new_paper(KEY, title="Recovery under steering")
    paper.update(summary="Old read.", extraction={"model": "m", "schema_version": 1})
    store.save_paper(paper)
    assert extract.merge_extraction(KEY, payload("New read."))["summary"] == "New read."


def test_a_reread_without_a_summary_keeps_the_one_on_file():
    setup_paper()
    extract.merge_extraction(KEY, payload("First read."))
    paper = extract.merge_extraction(KEY, payload(""))
    assert paper["summary"] == "First read."
    # Still the model's, so the next read can replace it.
    assert extract.merge_extraction(KEY, payload("Third read."))["summary"] == "Third read."


# --- the browser ----------------------------------------------------------

@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_the_summary_and_relevance_are_edited_from_the_overview(engine):
    from playwright.async_api import expect

    from test_browser import _paper as _browser_paper, _server

    _browser_paper(KEY, "A Study", "steering")
    paper = store.load_paper(KEY)
    paper["summary"] = "The model's summary."
    store.save_paper(paper)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}#paper={KEY}")
                summary = page.locator(".paper-summary")
                await summary.wait_for()
                await summary.get_by_role("button", name="Edit summary").click()
                await expect(page.locator("#ask-title")).to_have_text("Summary for A Study")
                lines = page.locator("#ask-lines")
                await expect(lines).to_have_value("The model's summary.")
                await lines.fill("My summary.")
                await lines.press("Control+Enter")
                await expect(page.locator("#ask")).to_be_hidden()
                await expect(summary.locator("p")).to_have_text("My summary.")
                assert store.load_paper(KEY)["summary"] == "My summary."

                # A paper with no relevance yet can be given one.
                relevance = page.locator(".paper-relevance")
                await expect(relevance.locator(".hint")).to_have_text("Not written yet.")
                await relevance.get_by_role("button", name="Edit why it matters").click()
                await lines.fill("It measures my endpoint.")
                await page.locator("#ask").get_by_role("button", name="Save", exact=True).click()
                await expect(page.locator("#ask")).to_be_hidden()
                await expect(relevance.locator("p")).to_have_text("It measures my endpoint.")
                assert store.load_paper(KEY)["relevance"] == "It measures my endpoint."
            assert errors == []

    run_browser(scenario())
