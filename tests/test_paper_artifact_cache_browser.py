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
@pytest.mark.parametrize("old_first", [True, False])
def test_artifacts_ignore_read_from_previous_visit_to_same_workspace(engine, old_first):
    config.set_ai_enabled(False)
    other = config.create_workspace("Synthetic other")["id"]
    for ws in ["default", other]:
        with config.use_workspace(ws):
            store.save_paper(store.new_paper("a", title="Synthetic a"))
            store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Latest artifact", papers=["a"])
    )
    stale = deepcopy(item)
    stale["name"] = "Old artifact"

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started = [asyncio.Event(), asyncio.Event()]
            release = [asyncio.Event(), asyncio.Event()]
            finished = [asyncio.Event(), asyncio.Event()]
            reads = 0

            async def hold(route):
                nonlocal reads
                if route.request.headers.get("x-doxograph-workspace") != "default":
                    await route.continue_()
                    return
                index = reads
                reads += 1
                assert index < 2
                started[index].set()
                await release[index].wait()
                await route.fulfill(json={"datasets": [stale if index == 0 else item]})
                finished[index].set()

            await page.route("**/api/datasets", hold)
            with _server() as url:
                await page.goto(url + "/#paper=a")
                await started[0].wait()
                await page.locator("#reader .pdf-page:not(.pending)").first.wait_for()
                await page.locator("#workspace").select_option(other)
                await page.wait_for_function(
                    "id=>workspaceClient.workspaceId===id&&!workspaceController.pending",
                    arg=other,
                )
                await page.locator('#papers li[data-paper="a"]').click()
                await page.wait_for_function("Datasets.loaded()")
                await page.locator("#workspace").select_option("default")
                await page.wait_for_function(
                    "workspaceClient.workspaceId==='default'&&!workspaceController.pending"
                )
                await page.locator('#papers li[data-paper="a"]').click()
                await started[1].wait()
                first, second = (0, 1) if old_first else (1, 0)
                async with page.expect_response(
                    lambda response: (
                        response.url.endswith("/api/datasets")
                        and response.request.headers.get("x-doxograph-workspace")
                        == "default"
                    )
                ) as received:
                    release[first].set()
                await (await received.value).finished()
                await finished[first].wait()
                # Observe the rendering opportunity after the held response,
                # before admitting the other response. An obsolete visit must
                # not publish even temporarily while the current read waits.
                await page.evaluate("() => new Promise(requestAnimationFrame)")
                if old_first:
                    await expect(page.locator(".paper-artifacts")).to_have_count(0)
                else:
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Latest artifact"
                    )
                release[second].set()
                await finished[second].wait()
                # A browser evaluation after the response lets the fetch promise publish.
                await page.wait_for_function("Datasets.loaded()")
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "Latest artifact"
                )
                await expect(page.locator(".paper-artifacts")).not_to_contain_text(
                    "Old artifact"
                )

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize(
    "field, timing",
    [
        ("name", "early"),
        ("name", "late"),
        ("name", "failure"),
        ("name", "editor"),
        ("roles", "early"),
        ("roles", "late"),
        ("roles", "failure"),
        ("links", "early"),
        ("links", "late"),
        ("links", "failure"),
    ],
)
def test_artifact_panel_observes_confirmed_dataset_changes(engine, field, timing):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(
            name="Old artifact", papers=[] if field == "links" else ["a"]
        )
    )
    saved = deepcopy(item)
    saved["revision"] = "synthetic-new-revision"
    if field == "name":
        saved["name"] = "New artifact"
    elif field == "roles":
        saved["roles"] = ["training"]
    else:
        saved["papers"] = ["a"]

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held_write(route):
                if route.request.method != "PUT":
                    await route.continue_()
                    return
                started.set()
                await release.wait()
                if timing == "failure":
                    await route.fulfill(
                        status=503, json={"detail": "Synthetic refusal"}
                    )
                else:
                    await route.fulfill(json=saved)

            await page.route("**/api/datasets/" + item["id"], held_write)
            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(
                    "Old artifact"
                )
                if field == "name":
                    await page.locator("#dataset-name").fill("New artifact")
                elif field == "roles":
                    await page.locator('[data-role="training"]').check()
                else:
                    await page.locator(".catalog-link-picker > summary").click()
                    await page.locator('input[data-link-paper="a"]').check()
                if timing == "early":
                    await started.wait()
                    release.set()
                    await page.wait_for_function("!Datasets.hasUnsavedChanges()")
                await page.locator('#dataset-research [data-paper="a"]').first.click()
                panel = page.locator(".paper-artifacts")
                if timing != "early":
                    await started.wait()
                    if field == "links":
                        await expect(panel).to_have_count(0)
                    else:
                        await expect(panel).to_contain_text("Old artifact")
                        await expect(panel).not_to_contain_text("training")
                    if timing == "editor":
                        await page.evaluate("()=>{V.noteEditing='a';renderContent()}")
                        await page.locator('textarea[data-note="a"]').fill(
                            "Later local writing"
                        )
                        await page.locator('textarea[data-note="a"]').focus()
                        await page.evaluate(
                            "window.keptNote=document.querySelector('textarea[data-note]')"
                        )
                    release.set()
                if timing == "failure":
                    await expect(page.locator("#datasets-status")).to_contain_text(
                        "Synthetic refusal"
                    )
                    assert await page.evaluate("Datasets.hasUnsavedChanges()")
                    if field == "links":
                        await expect(panel).to_have_count(0)
                    else:
                        await expect(panel).to_contain_text("Old artifact")
                        await expect(panel).not_to_contain_text("training")
                else:
                    await page.wait_for_function("!Datasets.hasUnsavedChanges()")
                    if timing == "editor":
                        assert await page.evaluate(
                            "window.keptNote===document.querySelector('textarea[data-note]')"
                        )
                        await expect(
                            page.locator('textarea[data-note="a"]')
                        ).to_have_value("Later local writing")
                        await expect(
                            page.locator('textarea[data-note="a"]')
                        ).to_be_focused()
                    else:
                        await expect(panel).to_contain_text(saved["name"])
                        if field == "roles":
                            await expect(panel).to_contain_text("training")

    run_browser(scenario())
