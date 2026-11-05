"""Reuse browser processes, with fresh contexts and servers for each scenario.

Playwright objects belong to their event loop. All browser scenarios therefore
run on one session-owned loop instead of creating a new loop with asyncio.run.
Each pytest process (including a CI shard) owns its own runtime.
"""

import asyncio
from contextlib import asynccontextmanager
from contextvars import copy_context

from playwright.async_api import async_playwright


class BrowserRuntime:
    def __init__(self):
        self.runner = asyncio.Runner()
        self.playwright = None
        self.browsers = {}

    def run(self, scenario):
        return self.runner.run(scenario, context=copy_context())

    async def browser(self, engine):
        if self.playwright is None:
            self.playwright = await async_playwright().start()
        browser = self.browsers.get(engine)
        if browser is None or not browser.is_connected():
            browser = await getattr(self.playwright, engine).launch()
            self.browsers[engine] = browser
        return browser

    async def shutdown(self):
        try:
            await asyncio.gather(*(browser.close() for browser in self.browsers.values()))
        finally:
            if self.playwright is not None:
                await self.playwright.stop()

    def close(self):
        try:
            if self.playwright is not None:
                self.runner.run(self.shutdown())
        finally:
            self.runner.close()


runtime = BrowserRuntime()
run_browser = runtime.run

# An init script for a page whose test works on a PDF's claims: they sit in a
# drawer under the PDF, closed until opened, and the browser remembers which.
CLAIMS_OPEN = "localStorage.setItem('doxograph-claims-drawer', 'open')"


@asynccontextmanager
async def browser_session(engine="chromium"):
    browser = await runtime.browser(engine)
    try:
        yield browser
    finally:
        # Browser.new_page creates a fresh context by default. Close all of a
        # scenario's contexts even when it fails, including explicit contexts
        # used for multiple tabs. Cookies, storage, routes, and pages never
        # carry over to the next test.
        await asyncio.gather(*(context.close() for context in browser.contexts))


async def open_ask_conversations(page):
    """Drop down the Ask conversation list, which stays closed over the transcript until asked for."""
    if not await page.locator('#agent-history').evaluate('details => details.open'):
        await page.locator('#agent-history > summary').click()


async def close_ask_conversations(page):
    """Put the Ask conversation list away, as a click elsewhere would, so it no longer covers the transcript."""
    if await page.locator('#agent-history').evaluate('details => details.open'):
        await page.locator('#agent-history > summary').click()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def inert_tools(monkeypatch):
    """No model is reached from browser tests that only drive the page."""
    from doxograph import agent, chat, claude_code, codex_cli, config, extract
    config.set_ai_enabled(False)

    def blocked(*args, **kwargs):
        pytest.fail('Unexpected provider in tools history controls')

    for module, name in ((agent, 'answer'), (chat, 'answer'), (claude_code, '_run'),
                         (codex_cli, '_run'), (extract, 'client')):
        monkeypatch.setattr(module, name, blocked)
