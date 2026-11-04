import asyncio
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("case", ["held-disabled", "held-late-edit", "topic-back"])
def test_workspace_header_lifecycle(engine, case):
    config.set_ai_enabled(False)
    store.add_tag("recovery", "Synthetic topic")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            with _server() as url:
                await page.goto(url + "/#view=research")
                form = page.locator("#research-form")
                field = page.locator("#research-context")
                await expect(form).to_be_visible()
                await field.fill("Submitted synthetic context")
                if case == "topic-back":
                    await page.locator('[data-open-topic="recovery"]').click()
                    await page.wait_for_function(
                        "V.view==='claims'&&V.tag==='recovery'"
                    )
                    await page.go_back()
                    await expect(form).to_be_visible()
                    await expect(field).to_have_value("Submitted synthetic context")
                    return
                started, release = asyncio.Event(), asyncio.Event()

                async def hold(route):
                    if route.request.method == "PUT":
                        started.set()
                        await release.wait()
                    await route.continue_()

                await page.route("**/api/context", hold)
                await form.get_by_role("button", name="Save", exact=True).click()
                await started.wait()
                await expect(field).to_be_disabled()
                try:
                    await page.locator("#btn-workspace").click()
                    if case == "held-disabled":
                        await expect(field).to_be_disabled()
                    else:
                        editable = await field.is_enabled()
                        if editable:
                            await field.fill("Later unacknowledged edit")
                finally:
                    release.set()
                await page.wait_for_function("!V.researchSaving")
                if case == "held-late-edit":
                    await page.locator("#btn-workspace").click()
                    await expect(form).to_be_visible()
                    await expect(field).to_have_value(
                        "Later unacknowledged edit"
                        if editable
                        else "Submitted synthetic context"
                    )
                    assert store.load_context().strip() == "Submitted synthetic context"

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("other", [False, True])
def test_workspace_delete_proxy_matches_real_action(engine, other):
    workspace = (
        config.create_workspace("Synthetic removable")["id"] if other else "default"
    )

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + "/#view=research&ws=" + workspace)
                proxy = (
                    page.locator("#research-form")
                    .locator("..")
                    .locator('[data-reader-command="btn-workspace-delete"]')
                )
                await expect(proxy).to_be_visible()
                real = page.locator("#btn-workspace-delete")
                if not other:
                    await expect(real).to_be_disabled()
                    await expect(proxy).to_be_disabled()
                    assert await proxy.get_attribute(
                        "title"
                    ) == await real.get_attribute("title")
                else:
                    await expect(real).to_be_enabled()
                    await expect(proxy).to_be_enabled()
                    await proxy.click()
                    await expect(page.locator("#ask")).to_be_visible()
                    await page.locator("#ask-cancel").click()
                    assert config.get_workspace(workspace) is not None

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("failure", [False, True])
def test_held_research_save_leaving_returning_and_cancel(engine, failure):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            with _server() as url:
                await page.goto(url + "/#view=research")
                field = page.locator("#research-context")
                await field.fill("Submitted across navigation")
                started, release = asyncio.Event(), asyncio.Event()

                async def hold(route):
                    started.set()
                    await release.wait()
                    if failure:
                        await route.fulfill(
                            status=507, json={"detail": "Synthetic context failure"}
                        )
                    else:
                        await route.continue_()

                await page.route("**/api/context", hold)
                await (
                    page.locator("#research-form")
                    .get_by_role("button", name="Save", exact=True)
                    .click()
                )
                await started.wait()
                try:
                    await page.locator("#btn-findings").click()
                    await page.locator("#btn-workspace").click()
                    await expect(field).to_have_value("Submitted across navigation")
                    assert await page.locator("#research-form").evaluate(
                        'form=>Array.from(form.querySelectorAll("input,textarea,button")).every(el=>el.disabled)'
                    )
                finally:
                    release.set()
                await page.wait_for_function("!V.researchSaving")
                if failure:
                    await expect(field).to_be_enabled()
                    await expect(page.locator("#content .warn")).to_contain_text(
                        "Synthetic context failure"
                    )
                    await expect(field).to_have_value("Submitted across navigation")
                    await page.locator('[data-act="cancel-research"]').click()
                    await page.locator("#btn-workspace").click()
                    await expect(field).to_have_value("")
                else:
                    await page.locator("#btn-workspace").click()
                    await expect(field).to_be_enabled()
                    await expect(field).to_have_value("Submitted across navigation")

    run_browser(scenario())
