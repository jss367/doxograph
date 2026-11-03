"""The timeline uses the map's whole width, and the map's bottom edge resizes it."""

import pytest

from browser_support import browser_session, run_browser
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_timeline_spreads_dates_across_the_width_and_the_bottom_edge_resizes_the_map(engine):
    # Many papers from one year stack up in a single lane; the dates should
    # still spread across the canvas rather than sit in its middle third.
    _paper("early", "Early paper", "recovery", year=2025)
    for i in range(5):
        _paper(f"late-{i}", f"Late paper {i}", "recovery", year=2026)
    _paper("undated", "Undated paper", "recovery")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 2000, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}/#view=graph")
                await page.get_by_label("X axis", exact=True).select_option("year")
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                box = await page.locator(".graph-wrap canvas").bounding_box()
                xs = [box["width"] / 2 + graph["tx"] + n["x"] * graph["zoom"] for n in graph["nodes"]]
                assert min(xs) >= 0 and max(xs) <= box["width"]
                assert max(xs) - min(xs) > 0.8 * box["width"]

                wrap = page.locator(".graph-wrap")
                handle = page.locator(".graph-resize")
                fitted = (await wrap.bounding_box())["height"]
                edge = await handle.bounding_box()
                x, y = edge["x"] + 300, edge["y"] + edge["height"] / 2

                # Dragging the edge up shrinks the map.
                await page.mouse.move(x, y)
                await page.mouse.down()
                await page.mouse.move(x, y - 100, steps=5)
                await page.mouse.up()
                assert (await wrap.bounding_box())["height"] == pytest.approx(fitted - 100, abs=4)
                canvas = await page.evaluate("GRAPH.canvas.height / devicePixelRatio")
                assert canvas == pytest.approx(fitted - 100, abs=4)

                # Holding the edge at the bottom of the window keeps growing it.
                edge = await handle.bounding_box()
                await page.mouse.move(x, edge["y"] + edge["height"] / 2)
                await page.mouse.down()
                await page.mouse.move(x, 995, steps=5)
                await page.wait_for_function(
                    f"document.querySelector('.graph-wrap').getBoundingClientRect().height > {fitted + 150}")
                await page.mouse.up()
                assert await page.evaluate("$('main').scrollTop") > 0
                grown = (await wrap.bounding_box())["height"]
                await page.wait_for_timeout(100)
                assert (await wrap.bounding_box())["height"] == grown

                # A double-click on the edge goes back to filling the window.
                await handle.dblclick()
                await page.evaluate("$('main').scrollTop = 0")
                assert (await wrap.bounding_box())["height"] == pytest.approx(fitted, abs=4)
                assert errors == []

    run_browser(scenario())
