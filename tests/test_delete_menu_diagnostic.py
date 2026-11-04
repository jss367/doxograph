import asyncio
import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success", "failure", "cancel", "lost-response", "navigate", "reopened"]
)
def test_delete_menu_lifecycle(engine, archived, outcome):
    config.set_ai_enabled(True)
    config.set_ai_provider("codex")
    store.save_paper(store.new_paper("paper", title="Synthetic reading paper"))
    store.pdf_path("paper").write_bytes(minimal_pdf("Synthetic passage"))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": 1440, "height": 1000})
            with _server() as url:
                chat = httpx.put(
                    url + "/api/chats/" + "a" * 32,
                    json={
                        "agent": "codex",
                        "revision": 0,
                        "operation": "a" * 32,
                        "messages": [
                            {"role": "user", "content": "Only saved question"},
                            {
                                "role": "assistant",
                                "content": "Synthetic answer",
                                "assistant": "ChatGPT",
                            },
                        ],
                    },
                ).json()
                if archived:
                    chat = httpx.patch(
                        url + "/api/chats/" + "a" * 32,
                        json={"revision": chat["revision"], "archived": True},
                    ).json()
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                history = page.locator("#agent-history")
                summary = history.locator(":scope > summary")
                await summary.click()
                if archived:
                    await page.locator("#agent-archive > summary").click()
                    await page.locator("#agent-archived [data-thread-open]").focus()
                    await page.locator("#agent-archived [data-thread-open]").press(
                        "Enter"
                    )
                    await summary.press("Enter")
                started, release = asyncio.Event(), asyncio.Event()
                requests = []

                async def intercept(route):
                    if route.request.method != "DELETE":
                        await route.continue_()
                        return
                    requests.append(route.request.url)
                    started.set()
                    if outcome in ["navigate", "reopened"]:
                        await release.wait()
                    if outcome == "failure":
                        await route.fulfill(
                            status=507, json={"detail": "Synthetic delete failure"}
                        )
                    elif outcome == "lost-response":
                        await route.fetch()
                        await route.abort()
                    else:
                        await route.continue_()

                await page.route("**/api/chats/*", intercept)
                delete = page.locator("#agent-delete")
                await delete.focus()
                await delete.press("Enter")
                await expect(page.locator("#ask")).to_be_visible()
                await page.locator(
                    "#ask-cancel" if outcome == "cancel" else "#ask-ok"
                ).press("Enter")
                await expect(page.locator("#ask")).to_be_hidden()
                if outcome == "cancel":
                    assert not requests
                    await expect(history).to_have_attribute("open", "")
                    assert await page.evaluate(
                        "()=>{const e=document.activeElement;return document.querySelector('#agent-history').contains(e) && e.getClientRects().length>0}"
                    )
                    return
                await started.wait()
                if outcome == "navigate":
                    await page.locator('#papers li[data-paper="paper"]').click()
                    await page.locator("#btn-reader-filters").focus()
                    release.set()
                elif outcome == "reopened":
                    await summary.press("Escape")
                    await summary.press("Enter")
                    release.set()
                if outcome in ["failure", "lost-response"]:
                    await expect(page.locator("#agent-status")).to_contain_text(
                        "Could not delete"
                    )
                    await expect(history).to_have_attribute("open", "")
                    await expect(summary).to_be_focused()
                    assert await page.locator("#agent-delete").is_enabled()
                else:
                    await expect(delete).to_be_hidden()
                    if outcome == "navigate":
                        await expect(
                            page.locator("#btn-reader-filters")
                        ).to_be_focused()
                    elif outcome == "reopened":
                        await expect(history).to_have_attribute("open", "")
                        await expect(summary).to_be_focused()
                    else:
                        await expect(history).not_to_have_attribute("open", "")
                        await expect(summary).to_be_focused()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("archived", [False, True])
def test_new_conversation_dismisses_history_and_focuses_composer(engine, archived):
    config.set_ai_enabled(True)
    config.set_ai_provider("codex")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                chat = httpx.put(
                    url + "/api/chats/" + "a" * 32,
                    json={
                        "agent": "codex",
                        "revision": 0,
                        "operation": "a" * 32,
                        "messages": [
                            {"role": "user", "content": "Saved question"},
                            {
                                "role": "assistant",
                                "content": "Synthetic answer",
                                "assistant": "ChatGPT",
                            },
                        ],
                    },
                ).json()
                if archived:
                    httpx.patch(
                        url + "/api/chats/" + "a" * 32,
                        json={"revision": chat["revision"], "archived": True},
                    ).raise_for_status()
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                await page.locator("#agent-history > summary").click()
                if archived:
                    await page.locator("#agent-archive > summary").click()
                    await page.locator("#agent-archived [data-thread-open]").click()
                    await page.locator("#agent-history > summary").click()
                await page.locator("#agent-clear").focus()
                await page.locator("#agent-clear").press("Enter")
                await expect(page.locator("#agent-history")).not_to_have_attribute(
                    "open", ""
                )
                await expect(page.locator("#agent-question")).to_be_focused()
                await expect(page.locator("#agent-messages")).to_be_empty()

    run_browser(scenario())
