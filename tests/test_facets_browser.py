"""Facets in the web app: defined, filtered on, edited, and drawn on the map."""

from pathlib import Path

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _paper, _poll, _server


def _corpus() -> None:
    _paper("one", "Steering study", "steering")
    _paper("two", "Probing study", "probing")
    store.add_tag("steering")
    store.add_tag("probing")
    store.add_facet("model", "The language models a result was measured on")
    store.update_claim("one", "one-c1", {"facets": {"model": ["GPT-4"]}})
    paper = store.load_paper("one")
    store.mark_facets_filled(paper["claims"][0], store.load_facets())
    store.save_paper(paper)
    paper = store.load_paper("two")
    paper["claims"][0]["facets"] = {"model": ["Llama 3 8B"]}
    store.save_paper(paper)


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_facets_filter_edit_and_color_the_map(engine):
    config.set_ai_enabled(False)
    _corpus()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1280, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                facet = page.locator("#facets details[data-facet='model']")
                await expect(facet).to_be_visible()
                await facet.locator("summary").click()
                await expect(facet.locator("[data-facet-value]")).to_have_count(2)
                await expect(facet).to_contain_text("Not yet filled in on 1 paper.")
                await expect(page.locator("#btn-fill-facets")).to_have_text("Fill in 1 paper")
                await expect(page.locator("#btn-fill-facets")).to_be_disabled()   # AI is off

                # A value filters the library to the papers whose claims carry it.
                await facet.locator("[data-facet-value='GPT-4']").click()
                chips = page.get_by_role("group", name="Active filters", exact=True)
                await expect(chips.get_by_role("button", name="Remove filter: model: GPT-4", exact=True)).to_be_visible()
                await expect(page.locator(".library-paper")).to_have_count(1)
                assert "facet=model%3AGPT-4" in page.url
                await page.reload()
                await expect(page.locator(".library-paper")).to_have_count(1)
                # The disclosure for the active facet opens with the page.
                await expect(page.locator("#facets details[data-facet='model'][open]")).to_have_count(1)

                # Claims carry their values as chips, and the editor edits them.
                await page.locator("#btn-claims-view").click()
                await expect(page.locator("#content .claim")).to_have_count(1)
                card = page.locator(".claim[data-claim='one-c1']")
                await expect(card.locator(".facet-value")).to_have_text("model GPT-4")
                await card.locator("[data-act='edit']").click()
                field = page.locator("form[data-form='one-c1'] [data-facet-input='model']")
                await expect(field).to_have_value("GPT-4")
                await field.fill("GPT-4, Claude 3 Opus")
                await page.locator("form[data-form='one-c1'] button[type='submit']").click()
                await expect(card.locator(".facet-value")).to_have_count(2)
                claim = store.load_paper("one")["claims"][0]
                assert claim["facets"] == {"model": ["Claude 3 Opus", "GPT-4"]}
                assert claim["facets_hand"] == ["model"]
                if engine == "chromium":
                    Path(".context").mkdir(exist_ok=True)
                    await page.locator("#side").screenshot(path=".context/facets-sidebar.png")
                    await page.screenshot(path=".context/facets-claims.png")

                # The map dims papers without the value and colors by the facet.
                await page.locator("#graph-nav [data-view='graph']").click()
                await page.locator("[data-graph-opt='color']").select_option("model")
                legend = page.locator("[data-graph-colors]")
                await expect(legend).to_be_visible()
                # One claim with two values: the tie goes to the first in sort order.
                await expect(legend).to_contain_text("Claude 3 Opus")
                await expect(legend).to_contain_text("Llama 3 8B")
                nodes = {n["key"]: n for n in await page.evaluate("doxographGraph().nodes")}
                assert nodes["two"]["dimmed"] and not nodes["one"]["dimmed"]
                # A facet named like the built-in topic coloring is still selectable.
                store.add_facet("topic", "The research area")
                await _poll(page)
                color = page.locator("[data-graph-opt='color']")
                await expect(color.locator("option[value='topic']")).to_have_count(1)
                await color.select_option("topic")
                await expect(legend).to_contain_text("no topic")
                await color.select_option("")
                await expect(legend).to_be_hidden()
                await color.select_option("model")
                store.delete_facet("topic")
                if engine == "chromium":
                    await page.screenshot(path=".context/facets-map.png")

                # Renaming a value rewrites it on every claim and follows the filter.
                await chips.get_by_role("button", name="Clear all", exact=True).click()
                await page.locator("#facets details[data-facet='model'] [data-facet-value='Llama 3 8B']").click()
                await expect(chips.get_by_role("button", name="Remove filter: model: Llama 3 8B", exact=True)).to_be_visible()
                await page.locator("#facets details[data-facet='model'] [data-facet-act='values']").click()
                await page.locator("#ask-label-choices [data-label='Llama 3 8B']").click()
                await expect(page.locator("#ask-input")).to_have_value("Llama 3 8B")
                await page.locator("#ask-ok").click()
                await expect(page.locator("#ask-input")).to_have_value("Llama 3 8B")
                await page.locator("#ask-input").fill("Llama-3-8B-Instruct")
                await page.locator("#ask-ok").click()
                await expect(page.locator("#facets [data-facet-value='Llama-3-8B-Instruct']")).to_have_count(1)
                assert store.load_paper("two")["claims"][0]["facets"] == {"model": ["Llama-3-8B-Instruct"]}
                await expect(chips.get_by_role("button", name="Remove filter: model: Llama-3-8B-Instruct", exact=True)).to_be_visible()
                assert "facet=model%3ALlama-3-8B-Instruct" in page.url
                await chips.get_by_role("button", name="Clear all", exact=True).click()

                # A new facet is defined from the sidebar.
                await page.locator("#btn-facet").click()
                await page.locator("#ask-input").fill("Dataset")
                await page.locator("#ask-ok").click()
                await page.locator("#ask-input").fill("The benchmarks or datasets used")
                await page.locator("#ask-ok").click()
                await expect(page.locator("#facets details[data-facet='dataset']")).to_have_count(1)
                await expect(page.locator("#btn-fill-facets")).to_have_text("Fill in 2 papers")
                await _poll(page)
                assert errors == []

    run_browser(scenario())
