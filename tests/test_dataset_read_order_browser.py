import asyncio
from copy import deepcopy

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, datasets, store
from pdfs import minimal_pdf
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("new_first", [False, True])
def test_show_and_panel_reads_publish_in_request_order(engine, new_first):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    latest = datasets.create_dataset(
        datasets.DatasetBody(name="Latest artifact", papers=["a"])
    )
    old = deepcopy(latest)
    old["name"] = "Old artifact"

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            reads = 0

            async def held(route):
                nonlocal reads
                index = reads
                reads += 1
                assert index < 2
                started[index].set()
                await release[index].wait()
                await route.fulfill(json={"datasets": [old if index == 0 else latest]})

            await page.route("**/api/datasets", held)
            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + latest["id"])
                await started[0].wait()
                await page.locator('#papers li[data-paper="a"]').click()
                await started[1].wait()
                first, second = (1, 0) if new_first else (0, 1)
                release[first].set()
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "Latest artifact" if new_first else "Old artifact"
                )
                async with page.expect_response(
                    lambda r: r.url.endswith("/api/datasets")
                ) as received:
                    release[second].set()
                await (await received.value).finished()
                await page.evaluate("() => new Promise(requestAnimationFrame)")
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "Latest artifact"
                )
                assert await page.evaluate(
                    "Datasets.forPaper('a').map(row=>row.name)"
                ) == ["Latest artifact"]

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_an_old_inspection_read_cannot_undo_an_acknowledged_save(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Old artifact", papers=["a"])
    )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held_read(route):
                response = await route.fetch()
                started.set()
                await release.wait()
                await route.fulfill(response=response)

            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(
                    "Old artifact"
                )
                await page.locator('[data-catalog-action="reload"]').click()
                await expect(page.locator("#dataset-saved-version")).to_be_visible()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await page.route("**/api/datasets", held_read)
                # One browser task makes the edit/action ordering independent
                # of the autosave timer while exercising the real DOM handlers.
                await page.evaluate("""() => {
                    const field = document.querySelector('#dataset-name');
                    field.value = 'New saved artifact';
                    field.dispatchEvent(new Event('input', {bubbles:true}));
                    document.querySelector('[data-catalog-action="replace"]').click();
                }""")
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "New saved artifact"
                )
                assert (
                    datasets.list_datasets()["datasets"][0]["name"]
                    == "New saved artifact"
                )
                release.set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "New saved artifact"
                )
                await expect(page.locator("#dataset-name")).to_have_value(
                    "New saved artifact"
                )
                assert (
                    await page.evaluate("Datasets.forPaper('a')[0].name")
                    == "New saved artifact"
                )

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failed", ["old", "new"])
def test_a_failed_read_does_not_displace_success_or_block_recovery(engine, failed):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    latest = datasets.create_dataset(
        datasets.DatasetBody(name="Latest artifact", papers=["a"])
    )
    old = deepcopy(latest)
    old["name"] = "Old artifact"

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            reads = 0

            async def held(route):
                nonlocal reads
                index = reads
                reads += 1
                assert index < 2
                started[index].set()
                await release[index].wait()
                if (failed == "old") == (index == 0):
                    await route.fulfill(
                        status=503, json={"detail": "Synthetic read refusal"}
                    )
                else:
                    await route.fulfill(
                        json={"datasets": [old if index == 0 else latest]}
                    )

            await page.route("**/api/datasets", held)
            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + latest["id"])
                await started[0].wait()
                await page.evaluate(
                    "async()=>{poller.stop();if(poller.running)await poller.once().catch(()=>{})}"
                )
                await page.locator('#papers li[data-paper="a"]').click()
                await started[1].wait()
                # The newer request finishes first in either failure case.
                async with page.expect_response(
                    lambda response: response.url.endswith("/api/datasets")
                ) as received:
                    release[1].set()
                await (await received.value).finished()
                await page.evaluate("() => new Promise(requestAnimationFrame)")
                release[0].set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "Old artifact" if failed == "new" else "Latest artifact"
                )
                assert await page.evaluate("Datasets.loaded()")

    run_browser(scenario())
