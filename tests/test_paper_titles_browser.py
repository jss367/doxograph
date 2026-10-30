"""Correct imported titles through the paper menus."""

import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import store
from test_browser import _answer, _paper, _poll, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_edit_title_from_sidebar_and_paper_actions_persists(engine):
    _paper("target", "iclr2027 tamc", "steering", year=2027)
    _paper("other", "Another paper", "steering")
    original = store.load_paper("target")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}#paper=other")
                row = page.locator('#papers li[data-paper="target"]')
                trigger = row.locator(".pmenu")
                await trigger.click()
                await page.locator("#ctxmenu").get_by_role("button", name="Edit title…").click()
                field = page.get_by_role("textbox", name="Edit paper title")
                await expect(field).to_have_value("iclr2027 tamc")
                await expect(field).to_be_focused()
                title = 'A corrected title: "Models" & <Methods>'
                await _answer(page, "Save", f"  {title}  ")
                await expect(row.locator(".pt")).to_have_text(title)
                await expect(trigger).to_be_focused()
                await expect(page.locator(".paperhead h2")).to_have_text("Another paper")
                saved = store.load_paper("target")
                assert saved["title"] == title
                for key in ("key", "claims", "authors", "year", "notes", "source"):
                    assert saved[key] == original[key]

                await page.reload()
                await expect(row.locator(".pt")).to_have_text(title)
                await row.locator(".pt").click()
                await expect(page.locator(".paperhead h2")).to_have_text(title)
                await page.locator(".paper-actions > summary").click()
                action = page.locator(".paper-actions").get_by_role("button", name="Edit title…")
                await action.click()
                await expect(field).to_have_value(title)
                # Polling replaces the dialog's original focus-return target.
                await _poll(page)
                await field.fill("The final paper title")
                await field.press("Enter")
                await expect(page.locator(".paperhead h2")).to_have_text("The final paper title")
                await expect(row.locator(".pt")).to_have_text("The final paper title")
                await expect(page.locator(".paper-actions > summary")).to_be_focused()
                assert store.load_paper("target")["title"] == "The final paper title"
                assert await page.evaluate("S.claims.find(c => c.paper === 'target').paper_title") == "The final paper title"
                await page.locator(".paper-actions > summary").click()
                await action.click()
                await _poll(page)
                await field.press("Escape")
                await expect(page.locator(".paper-actions > summary")).to_be_focused()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("fails", [False, True], ids=["delayed-success", "delayed-failure"])
def test_pending_title_save_cannot_overlap_or_replace_a_newer_dialog(engine, fails):
    _paper("target", "Imported title", "steering")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            started, release = asyncio.Event(), asyncio.Event()
            requests = []

            async def delay_save(route):
                if route.request.method != "PATCH":
                    await route.continue_()
                    return
                requests.append(route.request.post_data_json)
                started.set()
                await release.wait()
                if fails:
                    await route.fulfill(status=503, json={"detail": "Temporarily offline"})
                else:
                    await route.continue_()

            with _server() as url:
                await page.goto(f"{url}#paper=target")
                await page.route("**/api/papers/target", delay_save)
                await page.locator('#papers li[data-paper="target"] .pmenu').click()
                await page.locator("#ctxmenu").get_by_role("button", name="Edit title…").click()
                dialog = page.locator("#ask")
                field = page.locator("#ask-input")
                await field.fill("First correction")
                await dialog.get_by_role("button", name="Save", exact=True).click()
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                    await expect(dialog).to_be_visible()
                    await expect(field).to_have_value("First correction")
                    await expect(field).to_be_disabled()
                    await expect(dialog.get_by_role("button", name="Saving…")).to_be_disabled()
                    await expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_disabled()
                    await page.keyboard.press("Enter")
                    await page.keyboard.press("Escape")
                    await expect(dialog).to_be_visible()
                    await page.evaluate("void editPaperTitle('target')")
                    assert requests == [{"title": "First correction"}]

                    # A forced replacement (for example navigation) retires the
                    # old dialog's owner while the request still has to finish.
                    await page.evaluate("""() => {
                        window.labelsFinished = false;
                        void editLabels('target').then(() => { window.labelsFinished = true; });
                    }""")
                    await expect(page.locator("#ask-title")).to_have_text("Labels for Imported title")
                    await expect(field).to_be_enabled()
                    await field.fill("Keep this label draft")
                    await page.evaluate("void editPaperTitle('target')")
                    await expect(field).to_have_value("Keep this label draft")
                finally:
                    release.set()
                await page.wait_for_function("workspaceClient.pendingMutations === 0 && titleEdits.size === 0")
                await expect(page.locator("#ask-title")).to_have_text("Labels for Imported title")
                await expect(field).to_have_value("Keep this label draft")
                await expect(field).to_be_enabled()
                await page.unroute("**/api/papers/target", delay_save)
                await _answer(page, "OK")
                await page.wait_for_function("window.labelsFinished")
                saved = store.load_paper("target")
                assert saved["labels"] == ["Keep this label draft"]
                assert saved["title"] == ("Imported title" if fails else "First correction")
                assert requests == [{"title": "First correction"}]

                # The guard is released once the original save finishes.
                await page.locator('#papers li[data-paper="target"] .pmenu').click()
                await page.locator("#ctxmenu").get_by_role("button", name="Edit title…").click()
                await _answer(page, "Save", "Latest correction")
                await expect(page.locator(".paperhead h2")).to_have_text("Latest correction")
                assert store.load_paper("target")["title"] == "Latest correction"
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_title_editor_cancels_validates_and_keeps_failed_corrections(engine):
    _paper("target", "Imported title", "steering")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                row = page.locator('#papers li[data-paper="target"]')
                trigger = row.locator(".pmenu")
                menu = page.locator("#ctxmenu")
                dialog = page.get_by_role("dialog", name="Edit paper title")
                field = dialog.get_by_role("textbox")

                for cancel in ("Cancel", "Escape"):
                    await row.click(button="right")
                    await menu.get_by_role("button", name="Edit title…").click()
                    await field.fill("Discard this correction")
                    if cancel == "Escape":
                        await field.press("Escape")
                    else:
                        await dialog.get_by_role("button", name="Cancel", exact=True).click()
                    await expect(dialog).to_be_hidden()
                    assert store.load_paper("target")["title"] == "Imported title"

                await trigger.click()
                await menu.get_by_role("button", name="Edit title…").click()
                await field.fill("   ")
                await dialog.get_by_role("button", name="Save", exact=True).click()
                await expect(dialog).to_be_visible()
                await expect(dialog.locator("#ask-note")).to_have_text("Enter a title for this paper.")
                assert store.load_paper("target")["title"] == "Imported title"

                async def refuse_save(route):
                    if route.request.method == "PATCH":
                        await route.fulfill(status=503, json={"detail": "Temporarily offline"})
                    else:
                        await route.continue_()

                await page.route("**/api/papers/target", refuse_save)
                await field.fill("My corrected title")
                await dialog.get_by_role("button", name="Save", exact=True).click()
                await expect(dialog.locator("#ask-note")).to_have_text(
                    "Could not save the title: Temporarily offline"
                )
                await expect(field).to_have_value("My corrected title")
                assert store.load_paper("target")["title"] == "Imported title"
                await page.unroute("**/api/papers/target", refuse_save)
                await dialog.get_by_role("button", name="Save", exact=True).click()
                await expect(row.locator(".pt")).to_have_text("My corrected title")
                await expect(trigger).to_be_focused()
                assert store.load_paper("target")["title"] == "My corrected title"
                assert not errors

    run_browser(scenario())
