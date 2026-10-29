"""The library browses papers, including papers without extracted claims."""

import asyncio
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, notebook, store
from pdfs import minimal_pdf
from test_browser import _answer, _paper, _poll, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("filters", ["q=recovery", "label=favorites"])
def test_legacy_search_links_keep_claims_and_explicit_library_links_keep_papers(engine, filters):
    config.set_ai_enabled(False)
    _paper("one", "A study of recovery")
    _paper("two", "Another study")
    store.set_labels("one", ["favorites"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#{filters}")
                claim = page.locator('#content .claim[data-claim="one-c1"]')
                await expect(claim).to_have_count(1)
                await expect(page.locator("#content .claim")).to_have_count(1)
                await expect(page.locator(".library-paper")).to_have_count(0)
                await page.reload()
                await expect(claim).to_have_count(1)

                # Choosing the new library keeps the same filters across reloads.
                await page.get_by_role("button", name="Papers", exact=True).click()
                await expect(page.locator('.library-paper[data-library-paper="one"]')).to_have_count(1)
                await expect(page.locator(".library-paper")).to_have_count(1)
                await page.reload()
                await expect(page.locator(".library-paper")).to_have_count(1)
                await expect(page.locator("#content .claim")).to_have_count(0)

                # A fresh home URL still defaults to the complete paper library.
                await page.goto(url)
                await expect(page.locator(".library-paper")).to_have_count(2)

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_library_includes_pdf_matches_beyond_both_search_page_limits(engine):
    config.set_ai_enabled(False)
    pdf = minimal_pdf(["Only this source passage mentions strawberry."])
    for index in range(105):
        key = f"study-{index:03d}"
        store.save_paper(store.new_paper(key, title=f"Study {index}"))
        store.pdf_path(key).write_bytes(pdf)
    store.save_paper(store.new_paper("unmatched", title="Another paper"))
    store.set_labels("study-104", ["favorites"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#q").fill("strawberry")
                cards = page.locator(".library-paper")
                await expect(cards).to_have_count(105)
                await expect(page.locator('#papers [data-paper=""] .pm')).to_have_text("105 of 106 match")
                await page.locator('#labels [data-label="favorites"]').click()
                await expect(cards).to_have_count(1)
                await expect(cards.first).to_have_attribute("data-library-paper", "study-104")
                await page.get_by_role("button", name="Clear all", exact=True).click()

                # A bounded claims search must expand when switching to the library.
                await page.get_by_role("button", name="Extracted claims", exact=True).click()
                await page.locator("#q").fill("strawberry")
                await expect(page.locator("#content .pdfhit")).to_have_count(20)
                await page.get_by_role("button", name="Papers", exact=True).click()
                await expect(cards).to_have_count(105)
                await page.reload()
                await expect(cards).to_have_count(105)

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("navigation", ["query", "workspace"])
def test_stale_search_page_cannot_change_results_or_continue_after_navigation(engine, navigation):
    config.set_ai_enabled(False)
    for key in ["one", "two", "fresh"]:
        store.save_paper(store.new_paper(key, title=f"Local paper {key}"))
    other = config.create_workspace("Other research")
    with config.use_workspace(other["id"]):
        store.save_paper(store.new_paper("remote", title="Another workspace paper"))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            held, release = asyncio.Event(), asyncio.Event()
            old_requests = []

            async def search_page(route):
                params = parse_qs(urlsplit(route.request.url).query)
                query = params["q"][0]
                offset = int(params.get("offset", ["0"])[0])
                if query == "strawberry":
                    old_requests.append((route.request.headers["x-doxograph-workspace"], offset))
                    if offset == 100:
                        held.set()
                        await release.wait()
                    await route.fulfill(json={"terms": [query], "papers": [{"key": "one" if offset == 0 else "two"}],
                                              "next_offset": offset + 100 if offset < 200 else None})
                else:
                    await route.fulfill(json={"terms": [query], "papers": [{"key": "fresh"}], "next_offset": None})

            with _server() as url:
                await page.goto(url)
                cards = page.locator(".library-paper")
                await expect(cards).to_have_count(3)
                await page.route("**/api/search*", search_page)
                try:
                    # Start the real pagination path with a promise we can await after release.
                    await page.evaluate("""() => {
                        V.q = 'strawberry'; document.getElementById('q').value = V.q;
                        window.oldSearch = runTextSearch(V.q);
                    }""")
                    await asyncio.wait_for(held.wait(), timeout=10)
                    if navigation == "query":
                        await page.locator("#q").fill("banana")
                        expected = "fresh"
                    else:
                        await page.locator("#workspace").select_option(other["id"])
                        expected = "remote"
                    await expect(cards).to_have_count(1)
                    await expect(cards.first).to_have_attribute("data-library-paper", expected)
                finally:
                    release.set()
                await page.evaluate("window.oldSearch")
                await expect(cards).to_have_count(1)
                await expect(cards.first).to_have_attribute("data-library-paper", expected)
                assert old_requests == [(config.DEFAULT_WORKSPACE_ID, 0), (config.DEFAULT_WORKSPACE_ID, 100)]

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [1440, 390])
def test_paper_library_metadata_navigation_and_selection(engine, width):
    config.set_ai_enabled(False)
    title = 'Understanding <oversight> & "evaluation"'
    _paper("oversight", title, "oversight", "evaluation", year=2025)
    paper = store.load_paper("oversight")
    paper["authors"] = ["Ada Lovelace", "Grace Hopper"]
    paper["summary"] = "A study of how people evaluate model behavior."
    paper["claims"].append(dict(paper["claims"][0], id="oversight-c2"))
    store.save_paper(paper)
    store.pdf_path("oversight").write_bytes(minimal_pdf(["A passage about oversight."]))
    store.save_paper(store.new_paper("unread", title="A paper to read later"))
    store.save_paper(store.new_paper("queued", title="Queued paper", year=2024))
    store.save_paper(store.new_paper("finished", title="Finished paper", year=2023))
    notebook.save_notebook(notebook.Notebook(reading=[
        notebook.Reading(paper="oversight", status="reading", page=1),
        notebook.Reading(paper="queued", status="queued"),
        notebook.Reading(paper="finished", status="finished"),
    ]))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": width, "height": 1050})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                cards = page.locator(".library-paper")
                await expect(cards).to_have_count(4)
                await expect(page.locator("#content .claim")).to_have_count(0)
                card = page.locator('[data-library-paper="oversight"]')
                await expect(card.get_by_role("link", name=title, exact=True)).to_be_visible()
                await expect(card.locator(".library-paper-meta")).to_have_text("Ada Lovelace, Grace Hopper · 2025 · Page 1")
                await expect(card.locator(".library-paper-summary")).to_have_text(paper["summary"])
                await expect(card.get_by_role("button", name="oversight", exact=True)).to_have_count(1)
                await expect(card.locator(".reading-status")).to_have_text("Reading")
                unread = page.locator('[data-library-paper="unread"]')
                await expect(unread).to_contain_text("Authors unknown · Year unknown")
                await expect(unread).to_contain_text("No summary yet.")
                await expect(unread).to_contain_text("No topics yet")
                await expect(unread.locator(".reading-status")).to_have_text("Unread")
                await expect(page.locator('[data-library-paper="queued"] .reading-status')).to_have_text("Queued")
                await expect(page.locator('[data-library-paper="finished"] .reading-status')).to_have_text("Finished")
                await expect(page.locator("#selection-count")).to_have_text("4 papers")
                await expect(page.locator("#btn-focus-review")).to_be_hidden()

                # Select from the card without opening it; the sidebar stays in sync.
                await card.locator("[data-select-paper]").check()
                await expect(page.locator('#papers [data-select-paper="oversight"]')).to_be_checked()
                await expect(page.locator("#selection-count")).to_have_text("0 claims · 1 papers selected")
                await expect(cards).to_have_count(4)
                await page.get_by_role("button", name="Clear selection", exact=True).click()
                await page.get_by_role("button", name="Select visible papers", exact=True).click()
                await expect(page.locator("#selection-count")).to_have_text("0 claims · 4 papers selected")
                await page.get_by_role("button", name="Clear selection", exact=True).click()

                # Keyboard focus survives polling, including a changed library.
                link = card.get_by_role("link", name=title, exact=True)
                await link.focus()
                await _poll(page)
                await expect(link).to_be_focused()
                saved = store.load_paper("queued")
                saved["summary"] = "Updated in another window."
                store.save_paper(saved)
                await _poll(page)
                await expect(link).to_be_focused()
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")

                if engine == "chromium" and width == 1440:
                    await page.locator("#workspace-notes-toggle").click()
                    await page.locator("#paper-sort").select_option("year-desc")
                    Path(".context").mkdir(exist_ok=True)
                    await page.screenshot(path=".context/paper-library.png")

                # Card space opens the source reader, even if the PDF was hidden.
                await page.evaluate("V.pdfHidden = true")
                await card.click(position={"x": 15, "y": 65})
                await expect(page.locator("#reader .pdf-page:not(.pending)").first).to_be_visible()
                assert "paper=oversight" in page.url
                await page.go_back()
                await expect(cards).to_have_count(4)
                await page.reload()
                await expect(cards).to_have_count(4)
                await page.get_by_role("button", name="Extracted claims", exact=True).click()
                await expect(page.locator("#content .claim")).not_to_have_count(0)
                await expect(cards).to_have_count(0)
                await page.locator('#papers [data-paper=""]').click()
                await expect(cards).to_have_count(4)
                await card.get_by_role("link", name=title, exact=True).click()
                if width < 800:
                    await page.locator('#btn-reader-library').click()
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.keyboard.press("/")
                await expect(cards).to_have_count(4)
                await expect(page.locator("#library-view-switch")).to_be_visible()
                assert await page.evaluate("V.paper === null")
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_library_search_topics_labels_sort_and_live_status(engine):
    config.set_ai_enabled(False)
    _paper("one", "Alpha paper", "oversight", "evaluation", year=2020)
    paper = store.load_paper("one")
    paper["summary"] = "Calibrated confidence helps evaluators."
    store.save_paper(paper)
    store.save_paper(store.new_paper("two", title="Beta paper", authors=["Grace Hopper"], year=2025))
    store.pdf_path("two").write_bytes(minimal_pdf(["Only the source mentions strawberry."]))
    store.set_labels("two", ["favorites"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=library&group=0")
                cards = page.locator(".library-paper")
                await expect(cards).to_have_count(2)
                await page.locator("#paper-sort").select_option("year-desc")
                await expect(cards.first).to_have_attribute("data-library-paper", "two")
                await page.locator("#paper-sort").select_option("title")
                await expect(cards.first).to_have_attribute("data-library-paper", "one")
                await page.reload()
                await expect(cards.first).to_have_attribute("data-library-paper", "one")

                for query, key in [("confidence", "one"), ("Hopper", "two"), ("strawberry", "two")]:
                    await page.locator("#q").fill(query)
                    await expect(cards).to_have_count(1)
                    await expect(cards.first).to_have_attribute("data-library-paper", key)
                    if query == "confidence":
                        await page.get_by_role("button", name="Extracted claims", exact=True).click()
                        await expect(page.locator('#content .claim[data-claim="one-c1"]')).to_be_visible()
                        await expect(page.locator("#content .claim")).to_have_count(1)
                        await page.reload()
                        await expect(page.locator('#content .claim[data-claim="one-c1"]')).to_be_visible()
                        await page.get_by_role("button", name="Papers", exact=True).click()
                        await expect(cards).to_have_count(1)
                await expect(cards.locator(".library-paper-passage")).to_contain_text("strawberry")
                await expect(page.locator('#papers [data-paper=""] .pm')).to_have_text("1 of 2 match")
                await expect(page.locator('#papers [data-paper="two"]')).to_have_count(1)
                await page.locator("#q").fill("no matching paper")
                await expect(cards).to_have_count(0)
                await expect(page.locator("#content .empty")).to_have_text("No papers match these filters.")
                await page.get_by_role("button", name="Clear all", exact=True).click()

                await cards.first.get_by_role("button", name="oversight", exact=True).click()
                await expect(cards).to_have_count(1)
                await expect(cards.first).to_have_attribute("data-library-paper", "one")
                await page.locator("#q").fill("confidence")
                await expect(cards).to_have_count(1)
                await page.reload()
                await expect(cards).to_have_count(1)
                await page.get_by_role("button", name="Clear all", exact=True).click()
                await page.locator('#labels [data-label="favorites"]').click()
                await expect(cards).to_have_count(1)
                await expect(cards.first).to_have_attribute("data-library-paper", "two")

                # A saved library search returns to papers after claim review.
                await page.get_by_role("button", name="Save this search", exact=True).click()
                await _answer(page, "Save search", "Favorite papers")
                await expect(page.locator("#research-body [data-search]")).to_have_count(1)
                await page.get_by_role("button", name="Close research tools").click()
                await page.get_by_role("button", name="Extracted claims", exact=True).click()
                await page.locator('.research-tools-nav [data-tool="searches"]').click()
                await page.get_by_role("button", name="Open search", exact=True).click()
                await expect(cards).to_have_count(1)
                await expect(cards.first).to_have_attribute("data-library-paper", "two")

                saved = notebook.Notebook.model_validate(notebook.load())
                saved.reading = [notebook.Reading(paper="two", status="finished")]
                notebook.save_notebook(saved)
                await _poll(page)
                await expect(cards.first.locator(".reading-status")).to_have_text("Finished")
                store.save_paper(store.new_paper("three", title="Gamma paper"))
                await page.get_by_role("button", name="Clear all", exact=True).click()
                await _poll(page)
                await expect(cards).to_have_count(3)

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_failed_claim_save_keeps_library_live_and_preserves_draft(engine):
    config.set_ai_enabled(False)
    _paper("one", "A paper with a draft")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def fail_save(route):
                started.set()
                await release.wait()
                await route.fulfill(status=503, json={"detail": "Try again later"})

            with _server() as url:
                await page.goto(f"{url}/#view=claims")
                await page.locator('[data-act="edit"]').click()
                form = page.locator('form[data-form="one-c1"]')
                await form.locator('[name="text"]').fill("My wording survives navigation.")
                await page.route("**/api/papers/one/claims/one-c1", fail_save)
                await form.get_by_role("button", name="Save", exact=True).click()
                await asyncio.wait_for(started.wait(), timeout=5)
                await page.get_by_role("button", name="Papers", exact=True).click()
                release.set()
                await expect(page.locator("#content .warn")).to_contain_text("Could not save a claim")
                assert await page.evaluate("V.editing === null")
                store.save_paper(store.new_paper("two", title="A newly imported paper"))
                await _poll(page)
                await expect(page.locator(".library-paper")).to_have_count(2)
                await page.get_by_role("button", name="Extracted claims", exact=True).click()
                await page.locator('[data-act="edit"][data-claim="one-c1"]').click()
                await expect(form.locator('[name="text"]')).to_have_value("My wording survives navigation.")

    run_browser(scenario())
