"""Saved paper notes can be explored alongside their papers on the map."""

import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _paper, _poll, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("publication_date", [None, "2020-06", "2020-06-15"])
def test_map_notes_toggle_preview_filters_timeline_and_navigation(engine, publication_date):
    _paper("paper-a", "Paper A", "recovery", year=2020)
    _paper("paper-b", "Paper B", "gait")
    paper = store.load_paper("paper-a")
    paper["notes"] = "Check the control condition.\n<script>never execute</script>"
    paper["publication_date"] = publication_date
    store.save_paper(paper)
    paper = store.load_paper("paper-b")
    paper["notes"] = "  \n "
    store.save_paper(paper)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1200, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}/#view=graph")
                toggle = page.get_by_role("checkbox", name="my notes", exact=True)
                assert not await toggle.is_checked()
                await toggle.check()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                assert {n["id"] for n in graph["nodes"]} == {"p:paper-a", "p:paper-b", "n:paper-a"}
                assert [(e["type"], e["a"], e["b"]) for e in graph["edges"]] == [("note", "p:paper-a", "n:paper-a")]
                assert await page.locator("[data-graph-count]").inner_text() == "2 papers · 1 note · 1 link"

                # A note follows its paper's filter and publication date precision.
                await page.evaluate("V.tag = 'gait'; renderGraph()")
                assert await page.evaluate("window.doxographGraph().nodes.find(n => n.type === 'note').dimmed")
                await page.evaluate("V.tag = null; renderGraph()")
                await page.get_by_label("X axis", exact=True).select_option("year")
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                nodes = {n["id"]: n for n in graph["nodes"]}
                assert nodes["n:paper-a"]["year"] == 2020
                assert nodes["n:paper-a"]["date"] == (publication_date or "2020")
                assert nodes["n:paper-a"]["time"] == nodes["p:paper-a"]["time"]
                assert nodes["n:paper-a"]["x"] == nodes["p:paper-a"]["x"]

                box = await page.locator(".graph-wrap canvas").bounding_box()
                node = nodes["n:paper-a"]
                sx = box["x"] + box["width"] / 2 + graph["tx"] + node["x"] * graph["zoom"]
                sy = box["y"] + box["height"] / 2 + graph["ty"] + node["y"] * graph["zoom"]
                await page.mouse.move(sx, sy)
                tip = page.locator(".graph-tip")
                await tip.wait_for(state="visible")
                assert await tip.locator(".graph-note-preview").inner_text() == paper_note
                assert await tip.locator("script").count() == 0
                await page.mouse.click(sx, sy)
                await page.locator(".paperhead h2", has_text="Paper A").wait_for()
                await page.locator(".note", has_text="Check the control condition.").wait_for()
                await page.go_back()
                await page.locator(".graph-wrap canvas").wait_for()
                assert await toggle.is_checked()
                await toggle.uncheck()
                assert await page.evaluate("window.doxographGraph().nodes.every(n => n.type !== 'note')")
                assert await page.evaluate("window.doxographGraph().edges.every(e => e.type !== 'note')")
                assert errors == []

    paper_note = store.load_paper("paper-a")["notes"]
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_map_hides_changed_note_preview_without_pointer_movement(engine):
    _paper("paper-a", "Paper A", "recovery")
    paper = store.load_paper("paper-a")
    paper["notes"] = "First saved note."
    store.save_paper(paper)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=graph")
                await page.get_by_role("checkbox", name="my notes", exact=True).check()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                node = next(n for n in graph["nodes"] if n["id"] == "n:paper-a")
                box = await page.locator(".graph-wrap canvas").bounding_box()
                sx = box["x"] + box["width"] / 2 + graph["tx"] + node["x"] * graph["zoom"]
                sy = box["y"] + box["height"] / 2 + graph["ty"] + node["y"] * graph["zoom"]
                await page.mouse.move(sx, sy)
                tip = page.locator(".graph-tip")
                await tip.wait_for(state="visible")
                assert await tip.locator(".graph-note-preview").inner_text() == "First saved note."
                await _poll(page)
                assert await tip.is_visible()

                # A title-only correction changes the preview heading too.
                paper = store.load_paper("paper-a")
                paper["title"] = "Corrected title <script>still inert</script>"
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_hidden()
                await page.mouse.move(sx + 1, sy)
                await tip.wait_for(state="visible")
                assert await tip.locator("strong").inner_text() == "My note on " + paper["title"]
                assert await tip.locator(".graph-note-preview").inner_text() == "First saved note."
                assert await tip.locator("script").count() == 0
                await _poll(page)
                assert await tip.is_visible()

                # Another window saves while this pointer stays over the node.
                paper = store.load_paper("paper-a")
                paper["notes"] = "Updated note.\n<script>still inert</script>"
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_hidden()

                await page.mouse.move(sx + 2, sy)
                await tip.wait_for(state="visible")
                assert await tip.locator(".graph-note-preview").inner_text() == paper["notes"]
                assert await tip.locator("script").count() == 0

                paper["notes"] = ""
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_hidden()
                assert not await page.evaluate("GRAPH.nodes.has('n:paper-a')")

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_map_hides_changed_note_connection_preview_without_pointer_movement(engine):
    _paper("paper-a", "Paper A", "recovery")
    paper = store.load_paper("paper-a")
    paper["notes"] = "First saved note."
    store.save_paper(paper)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=graph")
                await page.get_by_role("checkbox", name="my notes", exact=True).check()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                a, b = graph["nodes"]
                box = await page.locator(".graph-wrap canvas").bounding_box()
                sx = box["x"] + box["width"] / 2 + graph["tx"] + (a["x"] + b["x"]) / 2 * graph["zoom"]
                sy = box["y"] + box["height"] / 2 + graph["ty"] + (a["y"] + b["y"]) / 2 * graph["zoom"]
                await page.mouse.move(sx, sy)
                tip = page.locator(".graph-tip")
                await tip.wait_for(state="visible")
                assert await tip.locator("strong").all_text_contents() == ["Paper A", "My note on Paper A"]

                # An edge preview shows labels, so body-only changes leave it intact.
                paper = store.load_paper("paper-a")
                paper["notes"] = "Updated note."
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_visible()

                paper["title"] = "Corrected title <script>still inert</script>"
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_hidden()
                await page.mouse.move(sx + 1, sy)
                await tip.wait_for(state="visible")
                assert await tip.locator("strong").all_text_contents() == [paper["title"], "My note on " + paper["title"]]
                assert await tip.locator("script").count() == 0

                paper["notes"] = ""
                store.save_paper(paper)
                await _poll(page)
                assert await tip.is_hidden()

    run_browser(scenario())


@pytest.mark.browser
def test_map_notes_follow_saved_updates_and_reset_between_workspaces():
    _paper("shared", "Default paper", "recovery")
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _paper("shared", "Other paper", "gait")
        paper = store.load_paper("shared")
        paper["notes"] = "A note in the other workspace."
        store.save_paper(paper)

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=graph")
                toggle = page.get_by_role("checkbox", name="my notes", exact=True)
                await toggle.check()
                assert await page.evaluate("window.doxographGraph().nodes.length") == 1

                for text in ["First saved note.", "An updated note.", ""]:
                    paper = store.load_paper("shared")
                    paper["notes"] = text
                    store.save_paper(paper)
                    await _poll(page)
                    if text:
                        assert await page.evaluate("GRAPH.nodes.get('n:shared').text") == text
                    else:
                        assert not await page.evaluate("GRAPH.nodes.has('n:shared')")
                    assert await toggle.is_checked()

                await page.locator("#workspace").select_option(other["id"])
                await page.locator('#graph-nav [data-view="graph"]').click()
                assert not await toggle.is_checked()
                assert await page.evaluate("window.doxographGraph().nodes.length") == 1
                await toggle.check()
                assert await page.evaluate("GRAPH.nodes.get('n:shared').text") == "A note in the other workspace."

    run_browser(scenario())
