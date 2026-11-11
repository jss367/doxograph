import asyncio
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server
from test_browser import _answer


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("case", ["held-editable", "held-late-edit", "topic-back"])
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
                if case == "topic-back":
                    await field.fill("Submitted synthetic context")
                    await page.locator('[data-open-topic="recovery"]').click()
                    await page.wait_for_function(
                        "V.view==='claims'&&V.tag==='recovery'"
                    )
                    await page.go_back()
                    await expect(form).to_be_visible()
                    await expect(field).to_have_value("Submitted synthetic context")
                    assert await page.evaluate("window.doxographSaveDrafts()") is True
                    assert store.load_context().strip() == "Submitted synthetic context"
                    return
                started, release = asyncio.Event(), asyncio.Event()
                writes = []

                async def hold(route):
                    if route.request.method == "PUT":
                        writes.append(route.request.post_data_json['text'])
                        started.set()
                        await release.wait()
                    await route.continue_()

                await page.route("**/api/context", hold)
                await field.fill("Submitted synthetic context")
                await form.get_by_role("button", name="Done", exact=True).click()
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await expect(field).to_be_enabled()
                    assert await page.evaluate("writingInFlight('research', '')") is True
                    assert await page.evaluate("canLeaveWorkspace()") is False
                    await form.evaluate("form => { window.heldResearchForm = form; }")
                    await page.locator("#btn-workspace").click()
                    assert await form.evaluate("form => form === window.heldResearchForm")
                    await expect(field).to_be_enabled()
                    if case == "held-late-edit":
                        await field.fill("Later edit during save")
                    assert writes == ["Submitted synthetic context"]
                finally:
                    release.set()
                await page.wait_for_function("!writingInFlight('research', '') && V.view === 'claims'")
                expected = "Later edit during save" if case == "held-late-edit" else "Submitted synthetic context"
                assert writes == (["Submitted synthetic context", expected] if case == "held-late-edit" else [expected])
                assert store.load_context().strip() == expected
                assert await page.evaluate("canLeaveWorkspace()") is True
                await page.locator("#btn-workspace").click()
                await expect(field).to_be_enabled()
                await expect(field).to_have_value(expected)

    run_browser(asyncio.wait_for(scenario(), 25))


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

    run_browser(asyncio.wait_for(scenario(), 25))


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
                await field.fill("Submitted across navigation")
                await (
                    page.locator("#research-form")
                    .get_by_role("button", name="Done", exact=True)
                    .click()
                )
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await page.locator("#btn-findings").click()
                    await page.locator("#btn-workspace").click()
                    await expect(field).to_have_value("Submitted across navigation")
                    await expect(field).to_be_enabled()
                    await expect(page.locator('#research-form button[type="submit"]')).to_be_enabled()
                    assert await page.evaluate("writingInFlight('research', '')") is True
                    assert await page.evaluate("canLeaveWorkspace()") is False
                finally:
                    release.set()
                await page.wait_for_function("!writingInFlight('research', '')")
                # The old Done belongs to the form we navigated away from.
                # Its completion must neither close this new form nor prompt here.
                assert await page.evaluate("V.view") == 'research'
                await expect(page.locator('#ask')).not_to_be_visible()
                await expect(field).to_be_enabled()
                await expect(field).to_have_value("Submitted across navigation")
                done = page.locator('#research-form button[type="submit"]')
                if failure:
                    await expect(page.locator('[data-writing-status="research"]')).to_contain_text(
                        "Synthetic context failure"
                    )
                    assert store.load_context().strip() == ""
                    assert await page.evaluate("hasQuitWriting()") is True
                    await done.click()
                    await _answer(page, 'Cancel')
                    await expect(field).to_have_value("Submitted across navigation")
                    assert await page.evaluate("hasQuitWriting()") is True
                    await done.click()
                    await _answer(page, 'Discard changes')
                else:
                    assert store.load_context().strip() == "Submitted across navigation"
                    await expect(page.locator('[data-writing-status="research"]')).to_have_text('Saved')
                    await done.click()
                await page.wait_for_function("V.view === 'claims'")
                assert await page.evaluate("hasQuitWriting()") is False
                assert await page.evaluate("canLeaveWorkspace()") is True
                await page.locator("#btn-workspace").click()
                await expect(field).to_be_enabled()
                await expect(field).to_have_value("" if failure else "Submitted across navigation")
                assert await page.evaluate("window.doxographSaveDrafts()") is True

    run_browser(asyncio.wait_for(scenario(), 25))
