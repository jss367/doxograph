"""Successful import notices expire after time the reader can use them."""

import asyncio
import os
from datetime import datetime, timezone

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _poll, _server


def saved_notice(identifier, state="done"):
    key = f"study-{identifier}"
    store.save_paper(store.new_paper(key, title=f"Study {identifier}"))
    # A saved notification from an instance that no longer holds its lease.
    store.write_json(config.data_dir() / ".jobs" / f"{identifier}.json", {
        "version": 2, "pid": os.getpid(), "owner": "0" * 32,
        "job": {"id": identifier, "kind": "import", "state": state,
                "label": key, "key": key, "detail": "",
                "workspace": config.workspace_id(), "started": store.now()},
        "source": {},
    })


async def freeze_clock(page):
    now = datetime(2026, 10, 28, tzinfo=timezone.utc)
    await page.clock.install(time=now)
    await page.clock.pause_at(now)


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_import_timeout_pauses_for_hover_and_focus_and_preserves_failures(engine):
    saved_notice(1)
    saved_notice(2, "error")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            await freeze_clock(page)
            with _server() as url:
                await page.goto(url)
                success = page.locator('#jobs [data-auto-dismiss]')
                await expect(success).to_have_count(1)
                await page.clock.run_for(3000)
                await success.hover()
                await _poll(page)
                await page.clock.run_for(10000)
                await expect(success).to_have_count(1)

                open_paper = success.get_by_role("button", name="Open paper", exact=True)
                await open_paper.focus()
                await page.mouse.move(0, 0)
                # Adding another card must preserve the focused button, too.
                response = httpx.post(url + "/api/upload?extract_now=false", files={
                    "files": ("broken.pdf", b"not a PDF", "application/pdf"),
                })
                assert response.status_code == 200
                # Upload acceptance precedes background PDF validation. With
                # the browser clock frozen, keep polling explicitly until the
                # worker publishes its failure instead of relying on a tick.
                deadline = asyncio.get_running_loop().time() + 10
                while asyncio.get_running_loop().time() < deadline:
                    await _poll(page)
                    if await page.locator('#jobs .job.error').count() == 2:
                        break
                    await asyncio.sleep(0.05)
                await expect(page.locator('#jobs .job.error')).to_have_count(2)
                await expect(open_paper).to_be_focused()
                await page.clock.run_for(10000)
                await expect(success).to_have_count(1)

                await open_paper.evaluate("button => button.blur()")
                # Only the original three seconds count; polling and pauses
                # must neither restart the eight seconds nor consume them.
                await page.clock.run_for(4999)
                await expect(success).to_have_count(1)
                await page.clock.run_for(1)
                await expect(success).to_have_count(0)
                assert all(job["state"] == "error" for job in httpx.get(url + "/api/jobs").json()["jobs"])
                assert store.load_paper("study-1")["title"] == "Study 1"
                await page.reload()
                await expect(page.locator('#jobs .job.error')).to_have_count(2)
                await expect(success).to_have_count(0)
                assert not errors
            # Expiration removes the persisted notice as well as its DOM card.
            with _server() as url:
                assert all(job["state"] == "error" for job in httpx.get(url + "/api/jobs").json()["jobs"])

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_import_timeout_only_counts_visible_cards_in_the_current_workspace(engine):
    for identifier in range(1, 8):
        saved_notice(identifier)
    other = config.create_workspace("Other research")
    with config.use_workspace(other["id"]):
        saved_notice(8)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await freeze_clock(page)
            with _server() as url:
                await page.goto(url)
                notices = page.locator('#jobs [data-auto-dismiss]')
                await expect(notices).to_have_count(6)
                await page.clock.run_for(3000)
                await page.select_option('#workspace', other["id"])
                await expect(notices).to_have_count(1)
                await page.clock.run_for(8000)
                await expect(notices).to_have_count(0)
                assert len(httpx.get(url + "/api/jobs").json()["jobs"]) == 7
                assert httpx.get(url + "/api/jobs", headers={
                    "X-Doxograph-Workspace": other["id"],
                }).json()["jobs"] == []

                await page.select_option('#workspace', config.DEFAULT_WORKSPACE_ID)
                await expect(notices).to_have_count(6)
                await page.clock.run_for(8000)
                await expect(notices).to_have_count(1)
                await page.clock.run_for(7999)
                await expect(notices).to_have_count(1)
                await page.clock.run_for(1)
                await expect(notices).to_have_count(0)
                assert httpx.get(url + "/api/jobs").json()["jobs"] == []
                assert len(store.paper_keys()) == 7

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_import_timeout_cannot_dismiss_old_cards_while_workspace_loads(engine):
    saved_notice(1)
    other = config.create_workspace("Other research")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await freeze_clock(page)
            with _server() as url:
                await page.goto(url)
                notices = page.locator('#jobs [data-auto-dismiss]')
                await expect(notices).to_have_count(1)
                await page.clock.run_for(7000)
                held, release = asyncio.Event(), asyncio.Event()
                deletes = []
                page.on("request", lambda request: deletes.append(request.headers)
                        if request.method == "DELETE" and "/api/jobs/" in request.url else None)

                async def hold_destination(route):
                    if route.request.headers.get("x-doxograph-workspace") == other["id"]:
                        held.set()
                        await release.wait()
                    await route.continue_()

                await page.route("**/api/state", hold_destination)
                try:
                    await page.select_option('#workspace', other["id"])
                    await asyncio.wait_for(held.wait(), timeout=10)
                    await expect(notices).to_have_count(1)
                    # The old DOM is connected after selection until the new
                    # workspace's state arrives. Its timer must not act there.
                    await page.clock.run_for(10000)
                    assert deletes == []
                    await notices.get_by_role("button", name="Dismiss notification for Study 1").click()
                    await page.mouse.move(0, 0)
                    await page.wait_for_function("workspaceClient.pendingMutations === 0")
                    assert deletes == []
                    await expect(page.locator('#toasts')).not_to_contain_text("Job not found")
                    assert len(httpx.get(url + "/api/jobs").json()["jobs"]) == 1
                finally:
                    release.set()
                await expect(notices).to_have_count(0)
                await page.select_option('#workspace', config.DEFAULT_WORKSPACE_ID)
                await expect(notices).to_have_count(1)
                await page.clock.run_for(8000)
                await expect(notices).to_have_count(0)
                assert [headers["x-doxograph-workspace"] for headers in deletes] == [config.DEFAULT_WORKSPACE_ID]
                assert store.load_paper("study-1")["title"] == "Study 1"

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reloading_an_unexpired_import_notice_starts_a_fresh_countdown(engine):
    saved_notice(1)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await freeze_clock(page)
            with _server() as url:
                await page.goto(url)
                notices = page.locator('#jobs [data-auto-dismiss]')
                await expect(notices).to_have_count(1)
                await page.clock.run_for(3000)
                await page.reload()
                await expect(notices).to_have_count(1)
                await page.clock.run_for(7999)
                await expect(notices).to_have_count(1)
                await page.clock.run_for(1)
                await expect(notices).to_have_count(0)
                assert httpx.get(url + "/api/jobs").json()["jobs"] == []

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_import_timeout_pauses_while_page_is_hidden(engine):
    saved_notice(1)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            await freeze_clock(page)
            with _server() as url:
                await page.goto(url)
                notices = page.locator('#jobs [data-auto-dismiss]')
                await expect(notices).to_have_count(1)
                await page.clock.run_for(3000)
                await page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {configurable:true, value:true});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                await page.clock.run_for(10000)
                await expect(notices).to_have_count(1)
                await page.evaluate("""() => {
                    Object.defineProperty(document, 'hidden', {configurable:true, value:false});
                    document.dispatchEvent(new Event('visibilitychange'));
                }""")
                await page.clock.run_for(4999)
                await expect(notices).to_have_count(1)
                await page.clock.run_for(1)
                await expect(notices).to_have_count(0)
                assert httpx.get(url + "/api/jobs").json()["jobs"] == []

    run_browser(scenario())
