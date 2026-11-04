import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, datasets, store
from pdfs import minimal_pdf
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("result", ["success", "failure", "conflict"])
def test_artifacts_follow_confirmed_delete_only(engine, result):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Synthetic artifact", papers=["a"])
    )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held_delete(route):
                if route.request.method != "DELETE":
                    await route.continue_()
                    return
                started.set()
                await release.wait()
                if result == "failure":
                    await route.fulfill(
                        status=503, json={"detail": "Synthetic refusal"}
                    )
                elif result == "conflict":
                    await route.fulfill(
                        status=409, json={"detail": "Synthetic changed revision"}
                    )
                else:
                    response = await route.fetch()
                    await route.fulfill(response=response)

            await page.route("**/api/datasets/" + item["id"] + "?*", held_delete)
            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(item["name"])
                await page.locator('[data-dataset-action="delete"]').click()
                await page.locator("#ask-ok").click()
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    item["name"]
                )
                await page.evaluate(
                    "window.keptPanel=document.querySelector('.paper-artifacts')"
                )
                release.set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                if result == "success":
                    await expect(page.locator(".paper-artifacts")).to_have_count(0)
                    assert datasets.list_datasets()["datasets"] == []
                else:
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        item["name"]
                    )
                    assert await page.evaluate(
                        "window.keptPanel===document.querySelector('.paper-artifacts')"
                    )
                    await expect(page.locator("#datasets-status")).to_contain_text(
                        "Synthetic"
                    )
                    assert len(datasets.list_datasets()["datasets"]) == 1

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("boundary", ["editor", "other-paper", "workspace", "cancel"])
def test_delete_notification_respects_later_reader(engine, boundary):
    config.set_ai_enabled(False)
    for key in ["a", "b"]:
        store.save_paper(
            store.new_paper(key, title="Synthetic " + key, notes="Stored note")
        )
        store.pdf_path(key).write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Deleted artifact", papers=["a"])
    )
    datasets.create_dataset(datasets.DatasetBody(name="Other artifact", papers=["b"]))
    other = (
        config.create_workspace("Synthetic other")["id"]
        if boundary == "workspace"
        else None
    )
    if other:
        with config.use_workspace(other):
            store.save_paper(
                store.new_paper("a", title="Other workspace a", notes="Stored note")
            )
            store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
            datasets.create_dataset(
                datasets.DatasetBody(name="Other workspace artifact", papers=["a"])
            )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held(route):
                if route.request.method != "DELETE":
                    await route.continue_()
                    return
                started.set()
                await release.wait()
                response = await route.fetch()
                await route.fulfill(response=response)

            await page.route("**/api/datasets/" + item["id"] + "?*", held)
            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(item["name"])
                await page.locator('[data-dataset-action="delete"]').click()
                if boundary == "cancel":
                    await page.locator("#ask-cancel").click()
                    assert not started.is_set()
                    await page.locator('#papers li[data-paper="a"]').click()
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        item["name"]
                    )
                    assert len(datasets.list_datasets()["datasets"]) == 2
                    return
                await page.locator("#ask-ok").click()
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                if boundary == "other-paper":
                    await page.locator('#papers li[data-paper="b"]').click()
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Other artifact"
                    )
                    await page.evaluate(
                        "window.keptPanel=document.querySelector('.paper-artifacts')"
                    )
                elif boundary == "editor":
                    await page.locator('[data-act="edit-note"][data-paper="a"]').click()
                    await page.locator('textarea[data-note="a"]').fill(
                        "Keep later note writing"
                    )
                    await page.locator('textarea[data-note="a"]').focus()
                    await page.evaluate(
                        "window.keptNote=document.querySelector('textarea[data-note]')"
                    )
                else:
                    await page.locator("#workspace").select_option(other)
                    await expect(page.locator("#workspace")).to_have_value("default")
                    assert (
                        await page.evaluate("workspaceClient.workspaceId") == "default"
                    )
                release.set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                if boundary == "other-paper":
                    assert await page.evaluate(
                        "window.keptPanel===document.querySelector('.paper-artifacts')"
                    )
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Other artifact"
                    )
                elif boundary == "editor":
                    assert await page.evaluate(
                        "window.keptNote===document.querySelector('textarea[data-note]')"
                    )
                    await expect(page.locator('textarea[data-note="a"]')).to_have_value(
                        "Keep later note writing"
                    )
                    await expect(
                        page.locator('textarea[data-note="a"]')
                    ).to_be_focused()
                    assert await page.evaluate("Datasets.forPaper('a').length") == 0
                    await page.locator('[data-act="done-note"][data-paper="a"]').click()
                    await expect(page.locator(".paper-artifacts")).to_have_count(0)
                else:
                    await page.locator("#workspace").select_option(other)
                    await page.wait_for_function(
                        "id=>workspaceClient.workspaceId===id&&!workspaceController.pending",
                        arg=other,
                    )
                    await page.locator('#papers li[data-paper="a"]').click()
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Other workspace artifact"
                    )
                    assert await page.evaluate(
                        "Datasets.forPaper('a').map(row=>row.name)"
                    ) == ["Other workspace artifact"]

    run_browser(scenario())


def revise_artifact(identifier, name):
    from fastapi.testclient import TestClient
    from doxograph import server

    record = next(
        item
        for item in datasets.list_datasets()["datasets"]
        if item["id"] == identifier
    )
    body = {key: record[key] for key in datasets.DatasetBody.model_fields}
    body.update(name=name, revision=record["revision"])
    response = TestClient(server.app, base_url="http://127.0.0.1:8765").put(
        "/api/datasets/" + identifier, json=body
    )
    response.raise_for_status()
    return response.json()


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["reload", "replace"])
@pytest.mark.parametrize("result", ["new", "same", "failure"])
def test_artifact_read_notifications_keep_inspection_semantics(engine, action, result):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Initial artifact", papers=["a"])
    )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held(route):
                started.set()
                await release.wait()
                if result == "failure":
                    await route.fulfill(
                        status=503, json={"detail": "Synthetic read refusal"}
                    )
                else:
                    response = await route.fetch()
                    await route.fulfill(response=response)

            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(item["name"])
                before = item["name"]
                if action == "replace":
                    revise_artifact(item["id"], "First saved artifact")
                    await page.locator('[data-dataset-action="reload"]').click()
                    await expect(page.locator("#dataset-saved-version")).to_be_visible()
                    await page.wait_for_function(
                        "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                    )
                    before = "First saved artifact"
                if result == "new":
                    revise_artifact(item["id"], "Fresh saved artifact")
                await page.route("**/api/datasets", held)
                await page.locator('[data-dataset-action="' + action + '"]').click()
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator(".paper-artifacts")).to_contain_text(before)
                await page.evaluate(
                    "window.keptPanel=document.querySelector('.paper-artifacts')"
                )
                release.set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                if result == "new":
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Fresh saved artifact"
                    )
                    # Reload only inspects; replacement with a newer revision
                    # also keeps the original draft until fresh consent.
                    await expect(page.locator("#dataset-name")).to_have_value(
                        "Initial artifact"
                    )
                else:
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        before
                    )
                    assert await page.evaluate(
                        "window.keptPanel===document.querySelector('.paper-artifacts')"
                    )
                    if result == "failure":
                        await expect(page.locator("#datasets-status")).to_contain_text(
                            "Synthetic read refusal"
                        )
                    elif action == "replace":
                        await expect(page.locator("#dataset-name")).to_have_value(
                            before
                        )

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("foreign", [False, True])
def test_discard_recovery_publishes_only_confirmed_cache_facts(engine, foreign):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            committed, started, release = (
                asyncio.Event(),
                asyncio.Event(),
                asyncio.Event(),
            )
            identifier = None
            deleting = []

            async def lost_create(route):
                nonlocal identifier
                if route.request.method != "POST":
                    await route.continue_()
                    return
                response = await route.fetch()
                identifier = (await response.json())["id"]
                committed.set()
                await route.abort()

            async def recover(route):
                if foreign and route.request.method == "GET":
                    started.set()
                    await release.wait()
                await route.continue_()

            async def remove(route):
                if route.request.method == "DELETE":
                    deleting.append(route.request.url)
                    started.set()
                    await release.wait()
                await route.continue_()

            with _server() as url:
                await page.route("**/api/datasets", lost_create)
                await page.goto(url + "/#view=datasets")
                await expect(page.locator("#dataset-new")).to_be_enabled()
                await page.locator("#dataset-new").click()
                await page.locator(".dataset-link-picker > summary").click()
                await page.locator('input[data-link-paper="a"]').check()
                # No valid create can start until its intended paper is linked.
                await page.locator("#dataset-name").fill("Created artifact")
                await committed.wait()
                await expect(page.locator("#datasets-status")).to_contain_text(
                    "Couldn’t save"
                )
                if foreign:
                    revise_artifact(identifier, "Other writer artifact")

                # Latest route wins; retain the lost-create behavior for POST.
                async def collection(route):
                    if route.request.method == "POST":
                        await lost_create(route)
                    else:
                        await recover(route)

                await page.route("**/api/datasets", collection)
                await page.route("**/api/datasets/*?*", remove)
                await page.locator('[data-dataset-action="cancel"]').click()
                await page.locator("#ask-ok").click()
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                if foreign:
                    await expect(page.locator(".paper-artifacts")).to_have_count(0)
                else:
                    await expect(page.locator("#paperhead")).to_contain_text(
                        "Synthetic a"
                    )
                release.set()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                if foreign:
                    await expect(page.locator(".paper-artifacts")).to_contain_text(
                        "Other writer artifact"
                    )
                    assert deleting == []
                    assert (
                        datasets.list_datasets()["datasets"][0]["name"]
                        == "Other writer artifact"
                    )
                else:
                    await expect(page.locator(".paper-artifacts")).to_have_count(0)
                    assert deleting
                    assert datasets.list_datasets()["datasets"] == []

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_cancel_replacement_keeps_dirty_draft_and_confirmed_artifacts(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper("a", title="Synthetic a", notes="Stored note"))
    store.pdf_path("a").write_bytes(minimal_pdf("Synthetic passage"))
    item = datasets.create_dataset(
        datasets.DatasetBody(name="Initial artifact", papers=["a"])
    )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            started, release = asyncio.Event(), asyncio.Event()

            async def held(route):
                started.set()
                await release.wait()
                response = await route.fetch()
                await route.fulfill(response=response)

            with _server() as url:
                await page.goto(url + "/#view=datasets&dataset=" + item["id"])
                await expect(page.locator("#dataset-name")).to_have_value(item["name"])
                revise_artifact(item["id"], "First saved artifact")
                await page.locator('[data-dataset-action="reload"]').click()
                await expect(page.locator("#dataset-saved-version")).to_be_visible()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await page.locator("#dataset-name").fill("Unsaved local draft")
                await page.route("**/api/datasets", held)
                await page.locator('[data-dataset-action="replace"]').click()
                await started.wait()
                await page.locator('#papers li[data-paper="a"]').click()
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "First saved artifact"
                )
                release.set()
                await expect(page.locator("#ask-title")).to_contain_text(
                    "Discard unsaved changes"
                )
                await page.locator("#ask-cancel").click()
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy')==='false'"
                )
                await expect(page.locator("#dataset-name")).to_have_value(
                    "Unsaved local draft"
                )
                await expect(page.locator(".paper-artifacts")).to_contain_text(
                    "First saved artifact"
                )
                assert await page.evaluate("Datasets.hasUnsavedChanges()")
                assert (
                    datasets.list_datasets()["datasets"][0]["name"]
                    == "First saved artifact"
                )

    run_browser(scenario())
