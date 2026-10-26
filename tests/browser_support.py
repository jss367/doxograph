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
