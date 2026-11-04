import asyncio
import re
import httpx
import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("ready", [False, True])
def test_delete_keyboard_waits_for_conversation_readiness(engine, ready):
    config.set_ai_enabled(True)
    config.set_ai_provider("codex")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def hold(route):
                started.set()
                await release.wait()
                await route.continue_()

            await page.route(re.compile(r".*/api/chats(?:\?.*)?$"), hold)
            with _server() as url:
                httpx.put(
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
                ).raise_for_status()
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                await started.wait()
                history = page.locator("#agent-history")
                summary = history.locator(":scope > summary")
                await summary.focus()
                await summary.press("Enter")
                delete = page.locator("#agent-delete")
                await expect(delete).to_be_hidden()
                try:
                    if ready:
                        release.set()
                        await expect(delete).to_be_visible()
                        await expect(delete).to_be_enabled()
                        await delete.focus()
                        await expect(delete).to_be_focused()
                        await delete.press("Enter")
                        await expect(page.locator("#ask")).to_be_visible()
                        await page.locator("#ask-cancel").press("Enter")
                    else:
                        # This is the old sequence: focus() does not make a hidden control
                        # focusable, so Enter reaches the still-focused disclosure summary.
                        await delete.focus()
                        await delete.press("Enter")
                        await expect(page.locator("#ask")).to_be_hidden()
                        await expect(history).not_to_have_attribute("open", "")
                finally:
                    release.set()
                await expect(page.locator("#agent-current")).to_have_text(
                    "Only saved question"
                )

    run_browser(scenario())
