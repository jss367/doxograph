"""Discarded reads preserve forced redraws without touching loading workspaces."""

import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_overtaken_full_refresh_renders_latest_claims_and_keeps_editor_draft(engine):
    config.set_ai_enabled(False)
    _paper("one", "A study with an open editor")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            held, release = asyncio.Event(), asyncio.Event()
            armed = True

            async def hold_first_read(route):
                nonlocal armed
                if not armed:
                    await route.continue_()
                    return
                armed = False
                response = await route.fetch()
                held.set()
                await release.wait()
                await route.fulfill(response=response)

            with _server() as url:
                await page.goto(f"{url}/#view=claims&group=0")
                await page.locator('[data-act="edit"][data-claim="one-c1"]').click()
                await page.evaluate("poller.stop()")
                draft = page.locator('form[data-form="one-c1"] [name="text"]')
                await draft.fill("Keep this unsaved wording.")
                added = store.add_claim("one", {"text": "A newly created finding."})
                await page.route("**/api/state", hold_first_read)
                try:
                    await page.evaluate("() => { window.forcedRead = refreshAll(); }")
                    await asyncio.wait_for(held.wait(), timeout=10)
                    # The newer poll wins the snapshot but leaves the editor's DOM alone.
                    await page.evaluate("updateFromPoll()")
                    await expect(page.locator("#content .claim")).to_have_count(1)
                    await expect(draft).to_have_value("Keep this unsaved wording.")
                finally:
                    release.set()
                await page.evaluate("window.forcedRead")
                await expect(page.locator(f'#content .claim[data-claim="{added["id"]}"]')).to_be_visible()
                await expect(page.locator("#content .claim")).to_have_count(2)
                await expect(draft).to_have_value("Keep this unsaved wording.")

    run_browser(scenario())
