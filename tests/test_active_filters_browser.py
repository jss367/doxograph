"""Active filters stay removable when they hide every result."""

import asyncio
from pathlib import Path
from urllib.parse import urlencode

import httpx
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


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("editor", ["claim", "research"])
def test_poll_updates_missing_filter_chips_without_replacing_editor(engine, editor):
    config.set_ai_enabled(False)
    _paper("one", "Original paper", "alignment")
    _paper("two", "Another paper", "methods")
    store.add_tag("alignment")
    store.set_labels("one", ["reading-list"])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page()
            with _server() as url:
                view = "view=research&" if editor == "research" else ""
                await page.goto(f"{url}/#{view}paper=one&tag=alignment&label=reading-list&group=0")
                chips = page.get_by_role("group", name="Active filters", exact=True)
                await expect(chips.locator(".filter-chip")).to_have_count(3)
                if editor == "claim":
                    await page.locator("[data-act='edit']").first.click()
                    field = page.locator("form[data-form='one-c1'] textarea[name='text']")
                else:
                    field = page.locator("#research-context")
                await field.fill("An unfinished sentence that stays under the cursor.")
                await field.evaluate("el => { el.focus(); el.setSelectionRange(7, 7); }")
                original = await field.element_handle()

                # A second window removes the filter's paper and its last topic/label.
                assert httpx.delete(f"{url}/api/papers/one").status_code == 200
                await _polled(page)
                await expect(chips).to_be_hidden()
                assert await original.evaluate("el => el.isConnected && el === document.activeElement && el.selectionStart === 7 && el.selectionEnd === 7")
                await expect(field).to_have_value("An unfinished sentence that stays under the cursor.")
                assert await page.evaluate("V.paper === null && V.tag === null && V.label === null")
                assert all(f"{key}=" not in page.url for key in ("paper", "tag", "label"))
                await expect(page.locator("#papers [data-paper='one']")).to_have_count(0)
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["q", "all"])
@pytest.mark.parametrize("fail", [False, True])
def test_clearing_filters_preserves_pending_research_form(engine, action, fail):
    config.set_ai_enabled(False)
    _paper("one", "Original paper", "alignment")
    store.add_tag("alignment")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page()
            started = asyncio.Event()
            release_save = asyncio.Event()

            async def hold_save(route):
                started.set()
                await release_save.wait()
                if fail:
                    await route.fulfill(status=500, json={"detail": "Research save failed for this test"})
                else:
                    await route.continue_()

            with _server() as url:
                await page.goto(f"{url}/#view=research&q=alignment&tag=alignment")
                context = page.locator("#research-context")
                await context.fill("The context submitted while filters change.")
                form = await page.locator("#research-form").element_handle()
                await page.route("**/api/context", hold_save)
                await page.locator("#research-form").get_by_role("button", name="Save", exact=True).click()
                await asyncio.wait_for(started.wait(), timeout=5)
                chips = page.get_by_role("group", name="Active filters", exact=True)
                name = "Clear all" if action == "all" else "Remove filter: Search: alignment"
                await chips.get_by_role("button", name=name, exact=True).click()
                assert await form.evaluate("el => el.isConnected && el === document.querySelector('#research-form')")
                await expect(context).to_be_disabled()
                await expect(context).to_have_value("The context submitted while filters change.")
                assert "q=" not in page.url
                if action == "all":
                    await expect(chips).to_be_hidden()
                else:
                    await expect(chips.locator(".filter-chip")).to_have_count(1)

                release_save.set()
                if fail:
                    await expect(page.locator("#content .warn")).to_contain_text("Research save failed for this test")
                    await expect(context).to_be_enabled()
                    await expect(context).to_have_value("The context submitted while filters change.")
                    await page.unroute("**/api/context", hold_save)
                    await page.locator("#research-form").get_by_role("button", name="Save", exact=True).click()
                await expect(page.locator("#research-form")).to_have_count(0)
                assert httpx.get(f"{url}/api/state").json()["context"] == "The context submitted while filters change."
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["paper", "all"])
@pytest.mark.parametrize("draft_state", ["unsaved", "failed", "pending_failure"])
def test_clearing_paper_scope_parks_new_claim(engine, action, draft_state):
    config.set_ai_enabled(False)
    _paper("one", "Original paper", "alignment")
    _paper("two", "Another paper", "methods")
    store.add_tag("alignment")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page()
            started = asyncio.Event()
            release_failure = asyncio.Event()
            draft_text = "A new claim that still belongs to the original paper."
            new_form = page.locator("form[data-form='__new__']")
            chips = page.get_by_role("group", name="Active filters", exact=True)

            async def fail_save(route):
                started.set()
                if draft_state == "pending_failure":
                    await release_failure.wait()
                await route.fulfill(status=500, json={"detail": "Save failed for this test"})

            with _server() as url:
                await page.goto(f"{url}/#paper=one&tag=alignment&group=0")
                await page.get_by_role("button", name="Add claim by hand").click()
                await new_form.locator("textarea[name='text']").fill(draft_text)

                # Removing a topic keeps the new claim and its visible destination.
                await chips.get_by_role("button", name="Remove filter: Topic: alignment", exact=True).click()
                await expect(new_form.locator("textarea[name='text']")).to_have_value(draft_text)
                assert await page.evaluate("V.paper === 'one' && V.newClaim.paper === 'one'")

                if draft_state != "unsaved":
                    await page.route("**/api/papers/one/claims", fail_save)
                    await new_form.get_by_role("button", name="Save", exact=True).click()
                    await asyncio.wait_for(started.wait(), timeout=5)
                    if draft_state == "failed":
                        await expect(page.locator("#content .warn")).to_contain_text("Could not save")

                name = "Clear all" if action == "all" else "Remove filter: Paper: Original paper"
                await chips.get_by_role("button", name=name, exact=True).click()
                await expect(new_form).to_have_count(0)
                await expect(page.get_by_role("button", name="Resume", exact=True)).to_have_count(0)
                assert await page.evaluate("V.paper === null && V.newClaim === null")
                if draft_state == "pending_failure":
                    release_failure.set()
                    await expect(page.locator("#content .warn")).to_contain_text("What you typed is kept")
                await expect(new_form).to_have_count(0)
                await page.locator("#papers [data-paper='two']").click()
                await expect(new_form).to_have_count(0)
                await page.locator("#papers [data-paper='one']").click()
                await page.get_by_role("button", name="Resume", exact=True).click()
                await expect(new_form.locator("textarea[name='text']")).to_have_value(draft_text)
                assert await page.evaluate("V.paper === 'one' && V.newClaim.paper === 'one'")

                if draft_state != "unsaved":
                    await page.unroute("**/api/papers/one/claims", fail_save)
                async with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/api/papers/one/claims")):
                    await new_form.get_by_role("button", name="Save", exact=True).click()
                await expect(new_form).to_have_count(0)
                claims = httpx.get(f"{url}/api/state").json()["claims"]
                saved = [claim for claim in claims if claim["text"] == draft_text]
                assert len(saved) == 1
                assert saved[0]["paper"] == "one"
            await browser.close()

    asyncio.run(scenario())
