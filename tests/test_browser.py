"""Small real-browser checks for review workflows that depend on DOM state."""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import httpx
import pytest
from playwright.async_api import async_playwright

from doxograph import store


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@contextmanager
def _server():
    port = _free_port()
    env = os.environ.copy()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "doxograph.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/api/state", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("the browser-test server did not start")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _paper(key: str, title: str) -> None:
    paper = store.new_paper(key, title=title)
    paper["claims"] = [
        {
            "id": f"{key}-c1",
            "text": f"A claim from {title}.",
            "kind": "finding",
            "strength": "supporting",
            "tags": [],
            "evidence": "",
            "quote": "",
            "locator": "",
            "ledger_links": [],
            "reviewed": True,
        }
    ]
    paper["claim_seq"] = 1
    store.refresh_status(paper)
    store.save_paper(paper)


@pytest.mark.browser
def test_failed_new_claim_survives_navigation_back_to_its_paper():
    _paper("paper-a", "Paper A")
    _paper("paper-b", "Paper B")

    async def scenario():
        request_started = asyncio.Event()
        release_failure = asyncio.Event()
        draft_text = "A draft that must survive a failed save."

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def fail_after_navigation(route):
                request_started.set()
                await release_failure.wait()
                await route.abort()

            await page.route("**/api/papers/paper-a/claims", fail_after_navigation)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.get_by_role("button", name="Add claim by hand").click()
                await page.locator('form[data-form="__new__"] textarea[name="text"]').fill(draft_text)
                await page.get_by_role("button", name="Save").click()
                await asyncio.wait_for(request_started.wait(), timeout=5)

                await page.locator('#papers [data-paper="paper-b"]').click()
                release_failure.set()
                await page.get_by_text("What you typed is kept", exact=False).wait_for()

                await page.locator('#papers [data-paper="paper-a"]').click()
                held = page.get_by_text("Unsaved new claim", exact=False)
                await held.wait_for()
                assert draft_text in await held.inner_text()

                await page.get_by_role("button", name="Resume").click()
                textarea = page.locator('form[data-form="__new__"] textarea[name="text"]')
                assert await textarea.input_value() == draft_text

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_right_click_menu_removes_a_paper_without_leaving_the_open_one():
    _paper("paper-a", "Paper A")
    _paper("paper-b", "Paper B")

    async def scenario():
        prompts = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def accept(dialog):
                prompts.append(dialog.message)
                await dialog.accept()

            page.on("dialog", accept)
            with _server() as url:
                await page.goto(url)
                menu = page.locator("#ctxmenu")
                paper_a = page.locator('#papers [data-paper="paper-a"]')
                paper_b = page.locator('#papers [data-paper="paper-b"]')
                await paper_b.click()

                # "All papers" cannot be removed, so it gets no menu.
                await page.locator('#papers [data-paper=""]').click(button="right")
                assert await menu.is_hidden()

                # Clicking elsewhere dismisses the menu without touching the paper.
                await paper_a.click(button="right")
                await menu.wait_for(state="visible")
                await page.locator("#main").click()
                await menu.wait_for(state="hidden")
                assert await paper_a.count() == 1

                await paper_a.click(button="right")
                await menu.wait_for(state="visible")
                assert "Paper A" in await menu.inner_text()
                await menu.get_by_role("button", name="Remove paper").click()
                await paper_a.wait_for(state="detached")

                assert prompts == ["Remove Paper A and its claims?"]
                assert await menu.is_hidden()
                assert "active" in (await paper_b.get_attribute("class") or "")
                assert "Paper B" in await page.locator(".paperhead h2").inner_text()

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_escape_closes_the_paper_menu_without_cancelling_an_open_editor():
    _paper("paper-a", "Paper A")
    _paper("paper-b", "Paper B")

    async def scenario():
        draft_text = "An edit that Escape on the menu must not throw away."

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                menu = page.locator("#ctxmenu")
                textarea = page.locator('form[data-form="paper-b-c1"] textarea[name="text"]')
                await page.locator('[data-act="edit"][data-claim="paper-b-c1"]').click()
                await textarea.fill(draft_text)

                # The first Escape only dismisses the menu; the editor keeps its text.
                await page.locator('#papers [data-paper="paper-a"]').click(button="right")
                await menu.wait_for(state="visible")
                await page.keyboard.press("Escape")
                await menu.wait_for(state="hidden")
                assert await textarea.input_value() == draft_text

                # With the menu closed, Escape reaches the editor as before.
                await page.keyboard.press("Escape")
                await textarea.wait_for(state="detached")

            await browser.close()

    asyncio.run(scenario())
