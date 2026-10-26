"""The command palette uses current workspace data and respects editing dialogs."""

import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from doxograph import config, notebook, store
from test_browser import _paper, _polled, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_command_palette_destinations_and_actions(engine):
    config.set_ai_enabled(False)
    _paper("one", "Steering and recovery", "recovery", year=2026)
    _paper("two", "Recovery across tasks", "methods", year=2025)
    paper = store.load_paper("two")
    paper["authors"] = ["Zoë Researcher"]
    store.save_paper(paper)
    store.add_tag("recovery", "When systems return to their original behavior")
    store.add_tag("methods")
    data = notebook.Notebook(boards=[
        notebook.Board(id="first", title="Methods to compare"),
        notebook.Board(id="second", title="Recovery <conditions> & evidence"),
    ]).model_dump()
    store.write_json(config.data_dir() / "notebook.json", data)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="one"]').wait_for()
                palette = page.locator("#command-palette")
                query = page.get_by_role("combobox", name="Search papers, topics, boards, and actions")
                options = palette.get_by_role("option")

                await page.keyboard.press("Meta+k")
                await expect(query).to_be_focused()
                await query.fill("recovery")
                await expect(options).to_have_count(4)
                assert await options.filter(has_text="Recovery <conditions> & evidence").count() == 1
                assert await palette.locator("conditions").count() == 0
                if engine == "chromium":
                    Path(".context").mkdir(exist_ok=True)
                    await page.screenshot(path=".context/command-palette.png")

                # Match author names without accents as well as paper titles.
                await query.fill("zoe 2025")
                await expect(options).to_have_count(1)
                await page.keyboard.press("Enter")
                await expect(palette).not_to_be_visible()
                await expect(page.locator(".paperhead h2")).to_have_text("Recovery across tasks")
                assert await page.evaluate("new URLSearchParams(location.hash.slice(1)).get('paper')") == "two"

                # A paper's old filters must not make a topic jump look empty.
                await page.locator("#q").fill("does not exist")
                await page.locator("#only-unreviewed").check()
                await page.keyboard.press("Control+k")
                await query.fill("topic recovery")
                await expect(options).to_have_count(1)
                await page.keyboard.press("Enter")
                await expect(page.locator("#q")).to_have_value("")
                await expect(page.locator("#only-unreviewed")).not_to_be_checked()
                assert await page.evaluate("V.paper === null && V.tag === 'recovery'")
                await expect(page.locator('.claim[data-paper="one"]')).to_be_visible()
                await page.reload()
                await expect(page.locator('#tags [data-tag="recovery"]')).to_have_class("active")
                await page.go_back()
                await page.wait_for_function("V.paper === 'two' && V.tag === null")

                # Direct board navigation opens the chosen board, not the first.
                await page.locator("#btn-command-palette").click()
                await query.fill("board conditions")
                await options.click()
                await expect(page.locator("#board-title")).to_have_value("Recovery <conditions> & evidence")
                await expect(page.locator("#research-tools")).to_have_attribute("aria-busy", "false")
                await page.locator("#board-title").fill("An unsaved title")
                await page.keyboard.press("Meta+k")
                await expect(palette).not_to_be_visible()
                await expect(page.locator("#board-title")).to_have_value("An unsaved title")
                # Discard the draft with the existing research tools dialog.
                await page.get_by_role("button", name="Close research tools").click()
                await page.locator("#ask-ok").click()
                await expect(page.locator("#research-tools")).not_to_be_visible()

                # Arrow navigation tracks the active descendant; Enter runs actions.
                await page.keyboard.press("Meta+k")
                await query.fill("export")
                await expect(options).to_have_count(2)
                first = await query.get_attribute("aria-activedescendant")
                await page.keyboard.press("ArrowUp")
                assert await query.get_attribute("aria-activedescendant") != first
                await page.keyboard.press("ArrowDown")
                assert await query.get_attribute("aria-activedescendant") == first
                await query.fill("retag")
                await expect(options).to_have_count(0)  # AI is disabled.
                await expect(page.locator("#command-status")).to_contain_text("No matches")
                await page.keyboard.press("Enter")
                await expect(palette).to_be_visible()
                await query.fill("keyboard shortcuts")
                await page.keyboard.press("Enter")
                await expect(page.locator("#help")).to_be_visible()
                await expect(palette).not_to_be_visible()
                assert not errors
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_command_palette_map_loads_citation_arrows(engine):
    _paper("one", "A paper that cites another")
    _paper("two", "The cited paper")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page()

            async def citations(route):
                await route.fulfill(json={"edges": [{"from": "one", "to": "two"}]})

            await page.route("**/api/citations", citations)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="one"]').wait_for()
                await page.keyboard.press("Meta+k")
                await page.locator("#command-query").fill("open paper map")
                async with page.expect_request("**/api/citations"):
                    await page.keyboard.press("Enter")
                await page.wait_for_function("""() => window.doxographGraph().edges.some(
                    edge => edge.type === 'cite' && edge.a === 'p:one' && edge.b === 'p:two'
                )""")
                await expect(page.locator("#command-palette")).not_to_be_visible()
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_command_palette_preserves_drafts_and_updates_with_workspace(engine):
    _paper("one", "Original paper", "original-topic")
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper("animal", "Animal paper", "gait")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await getattr(playwright, engine).launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('.claim[data-paper="one"]').click()
                await page.keyboard.press("e")
                field = page.locator('form[data-form] textarea[name="text"]')
                await field.fill("My unsaved wording")
                await page.keyboard.press("Meta+k")
                query = page.locator("#command-query")
                await query.fill("no matches anywhere")
                await page.keyboard.press("Escape")
                await expect(field).to_be_focused()
                await expect(field).to_have_value("My unsaved wording")
                # Navigating parks the edit so returning can recover its draft.
                await page.keyboard.press("Meta+k")
                await query.fill("topic original-topic")
                await page.keyboard.press("Enter")
                await page.keyboard.press("e")
                await expect(field).to_have_value("My unsaved wording")
                await page.keyboard.press("Escape")

                # The poll refreshes an already-open palette without losing query focus.
                await page.keyboard.press("Meta+k")
                await query.fill("new board")
                await expect(page.locator("#command-results li")).to_have_count(0)
                data = httpx.get(url + "/api/notebook").json()
                data["boards"].append({"id": "new", "title": "New board", "entries": []})
                assert httpx.put(url + "/api/notebook", json=data).status_code == 200
                await _polled(page)
                await expect(page.locator("#command-results li")).to_have_count(1)
                await expect(query).to_be_focused()
                await page.keyboard.press("Escape")

                await page.locator("#workspace").select_option(animal["id"])
                await page.locator('#papers [data-paper="animal"]').wait_for()
                await page.keyboard.press("Meta+k")
                await query.fill("original")
                await expect(page.locator("#command-results li")).to_have_count(0)
                await query.fill("animal paper")
                await expect(page.locator("#command-results li")).to_have_count(1)
            await browser.close()

    asyncio.run(scenario())
