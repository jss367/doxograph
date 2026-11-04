import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_ask_browser import _server


def seed_findings(kind, topics):
    config.set_ai_enabled(False)
    for topic in topics:
        store.add_tag(topic)
    for key in ["a", "b"]:
        store.save_paper(
            store.new_paper(key, title="Synthetic " + key, notes="Stored note")
        )
        store.pdf_path(key).write_bytes(minimal_pdf("Synthetic passage"))
        store.add_claim(key, {"text": "Synthetic claim " + key, "tags": topics})
    record = store.record_agreements if kind == "agreement" else store.record_tensions
    rows = store.agreement_rows if kind == "agreement" else store.tension_rows
    live = {row["id"]: row for row in store.claim_rows()}
    found = {"claims": list(live), "note": "Synthetic finding.", "kind": "tension"}
    for topic in topics:
        record(topic, [found], live)
    return rows()[0]["id"], found


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("kind", ["agreement", "tension"])
def test_last_topic_removal_and_restoration_match_renderable_findings(engine, kind):
    identifier, found = seed_findings(kind, ["first", "remaining"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            with _server() as url:
                await page.goto(url + "/#paper=a")
                link = page.locator('[data-finding-id="' + identifier + '"]')
                await expect(link).to_have_count(1)
                store.delete_tag("first")
                await page.evaluate("refreshAll()")
                await expect(link).to_have_count(1)
                store.delete_tag("remaining")
                await page.evaluate("refreshAll()")
                records = await page.evaluate(
                    "kind=>S[kind]", "agreements" if kind == "agreement" else "tensions"
                )
                assert any(
                    row["id"] == identifier and not row["topics"] for row in records
                )
                await expect(link).to_have_count(0)
                await expect(
                    page.locator('[data-act="claims-findings"]')
                ).to_have_count(0)
                store.add_tag("restored")
                for key in ["a", "b"]:
                    paper = store.load_paper(key)
                    paper["claims"][0]["tags"] = ["restored"]
                    store.save_paper(paper)
                live = {row["id"]: row for row in store.claim_rows()}
                record = (
                    store.record_agreements
                    if kind == "agreement"
                    else store.record_tensions
                )
                record("restored", [found], live)
                await page.evaluate("refreshAll()")
                await expect(link).to_have_count(1)
                await expect(
                    page.locator('[data-act="claims-findings"]')
                ).to_have_count(1)
                await link.click()
                await expect(
                    page.locator('.frow[data-finding="' + identifier + '"]').first
                ).to_be_visible()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("kind", ["agreement", "tension"])
@pytest.mark.parametrize("destination", ["single", "multi", "absent"])
def test_paper_finding_keyboard_focus_targets_rendered_summary(
    engine, kind, destination
):
    identifier, found = seed_findings(
        kind, ["first", "remaining"] if destination == "multi" else ["first"]
    )
    other_record = (
        store.record_tensions if kind == "agreement" else store.record_agreements
    )
    other_record("first", [found], {row["id"]: row for row in store.claim_rows()})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url + "/#paper=a")
                link = page.locator('[data-finding-id="' + identifier + '"]')
                await expect(link).to_have_count(1)
                if destination == "absent":
                    await page.locator('[data-act="edit-note"][data-paper="a"]').click()
                    await page.locator('textarea[data-note="a"]').fill(
                        "Keep parked writing"
                    )
                    # A poll can apply state while an active editor parks its
                    # redraw, leaving the previously drawn link on screen.
                    await page.evaluate(
                        """({kind,id})=>{
                        S[kind] = S[kind].filter(row=>row.id!==id);
                    }""",
                        {
                            "kind": "agreements" if kind == "agreement" else "tensions",
                            "id": identifier,
                        },
                    )
                await link.focus()
                await page.keyboard.press("Enter")
                await page.wait_for_function("V.view==='findings'")
                rows = page.locator('.frow[data-finding="' + identifier + '"]')
                if destination == "absent":
                    await expect(rows).to_have_count(0)
                    assert not await page.locator(".frow > summary").evaluate_all(
                        "rows=>rows.some(row=>row===document.activeElement)"
                    )
                else:
                    await expect(rows).to_have_count(2 if destination == "multi" else 1)
                    await expect(rows.first).to_have_attribute("open", "")
                    await expect(rows.first.locator("summary").first).to_be_focused()
                assert errors == []
                if destination == "absent":
                    await page.go_back()
                    await expect(page.locator(".paper-overview")).to_contain_text(
                        "Keep parked writing"
                    )

    run_browser(scenario())
