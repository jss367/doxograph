import pytest
import httpx
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from test_ask_browser import _server
from test_conversation_list_browser import seed_conversations


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize(
    "case",
    [
        "escape",
        "outside",
        "selection-focus",
        "archived-selection-focus",
        "close-reopen",
    ],
)
def test_history_keyboard_and_dismissal(engine, width, case):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={"width": width, "height": 850})
            with _server() as url:
                seed_conversations(url)
                if case == "archived-selection-focus":
                    chat = next(
                        c
                        for c in httpx.get(url + "/api/chats").json()["chats"]
                        if c["id"] == "c" * 32
                    )
                    httpx.patch(
                        url + "/api/chats/" + chat["id"],
                        json={"revision": chat["revision"], "archived": True},
                    ).raise_for_status()
                await page.goto(url)
                await page.locator("#btn-agent:enabled").click()
                history = page.locator("#agent-history")
                summary = history.locator(":scope > summary")
                await summary.focus()
                await summary.press("Enter")
                await expect(history).to_have_attribute("open", "")
                if case == "archived-selection-focus":
                    await page.locator("#agent-archive > summary").click()
                row = page.locator(
                    "#agent-archived [data-thread-open]"
                    if case == "archived-selection-focus"
                    else "#agent-thread [data-thread-open]"
                ).first
                await row.focus()
                if case == "escape":
                    await row.press("Escape")
                    await expect(history).not_to_have_attribute("open", "")
                    await expect(summary).to_be_focused()
                    await expect(page.locator("#library-agent")).to_be_visible()
                elif case == "outside":
                    await page.locator("#agent-question").click()
                    await expect(history).not_to_have_attribute("open", "")
                    await expect(page.locator("#agent-question")).to_be_focused()
                elif case in ["selection-focus", "archived-selection-focus"]:
                    await row.press("Enter")
                    await expect(history).not_to_have_attribute("open", "")
                    assert await page.evaluate(
                        "()=>{const e=document.activeElement;return e!==document.body && e.getClientRects().length>0}"
                    )
                    state = await page.evaluate(
                        """()=>{const e=document.activeElement;return {html:e.outerHTML,hiddenAncestor:Boolean(e.closest('details:not([open])')),rect:e.getBoundingClientRect().toJSON()}}"""
                    )
                    assert await summary.evaluate("el=>el===document.activeElement"), (
                        state
                    )
                else:
                    await page.locator("#agent-close").click()
                    await expect(history).not_to_have_attribute("open", "")
                    await page.locator("#btn-agent").click()
                    await expect(history).not_to_have_attribute("open", "")
                assert await page.locator("#library-agent").evaluate(
                    "el=>el.scrollWidth<=el.clientWidth"
                )

    run_browser(scenario())
