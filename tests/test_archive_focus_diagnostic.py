import asyncio
import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import store
from pdfs import minimal_pdf
from test_ask_browser import _server
from test_conversation_list_browser import seed_conversations


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("action", ["archive", "restore"])
@pytest.mark.parametrize(
    "outcome", ["success", "failure", "lost-response", "navigate", "reopened"]
)
def test_archive_history_focus(engine, action, outcome):
    store.save_paper(store.new_paper("paper", title="Synthetic reading paper"))
    store.pdf_path("paper").write_bytes(minimal_pdf("Synthetic passage"))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            with _server() as url:
                seed_conversations(url)
                if action == "restore":
                    chat = next(
                        c
                        for c in httpx.get(url + "/api/chats").json()["chats"]
                        if c["id"] == "a" * 32
                    )
                    httpx.patch(
                        url + "/api/chats/" + chat["id"],
                        json={"revision": chat["revision"], "archived": True},
                    ).raise_for_status()
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                history = page.locator("#agent-history")
                summary = history.locator(":scope > summary")
                await summary.click()
                if action == "restore":
                    await page.locator("#agent-archive > summary").click()
                started, release = asyncio.Event(), asyncio.Event()

                async def intercept(route):
                    if route.request.method != "PATCH":
                        await route.continue_()
                        return
                    started.set()
                    if outcome in ["navigate", "reopened"]:
                        await release.wait()
                    if outcome == "failure":
                        await route.fulfill(
                            status=507, json={"detail": "Synthetic disk failure"}
                        )
                    elif outcome == "lost-response":
                        await route.fetch()
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/api/chats/*", intercept)
                button = page.locator('[data-thread-archive="' + "a" * 32 + '"]')
                await button.focus()
                await button.press("Enter")
                await started.wait()
                if outcome == "navigate":
                    await page.locator('#papers li[data-paper="paper"]').click()
                    await page.locator("#btn-reader-filters").focus()
                    release.set()
                elif outcome == "reopened":
                    await summary.press("Escape")
                    await summary.press("Enter")
                    release.set()
                await expect(button).to_be_enabled()
                if outcome == "failure":
                    await expect(history).to_have_attribute("open", "")
                    await expect(page.locator(".chat-thread-error")).to_contain_text(
                        "Synthetic disk failure"
                    )
                    await expect(summary).to_be_focused()
                elif outcome == "navigate":
                    await expect(page.locator("#btn-reader-filters")).to_be_focused()
                    await expect(page.locator("#paperhead")).to_contain_text(
                        "Synthetic reading paper"
                    )
                elif outcome == "reopened":
                    await expect(history).to_have_attribute("open", "")
                    await expect(summary).to_be_focused()
                else:
                    await expect(history).not_to_have_attribute("open", "")
                    await expect(summary).to_be_focused()

    run_browser(scenario())
