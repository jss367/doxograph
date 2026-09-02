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


def _paper(key: str, title: str, *tags: str) -> None:
    paper = store.new_paper(key, title=title)
    paper["claims"] = [
        {
            "id": f"{key}-c1",
            "text": f"A claim from {title}.",
            "kind": "finding",
            "strength": "supporting",
            "tags": list(tags),
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
def test_switching_workspaces_hides_other_research_and_survives_reload():
    _paper("mind", "A consciousness paper")
    from doxograph import config

    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper("gait", "An animal locomotion paper")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                papers = page.locator("#papers")
                assert "A consciousness paper" in await papers.inner_text()
                assert "An animal locomotion paper" not in await papers.inner_text()

                await page.locator("#workspace").select_option(label="Animal locomotion")
                await page.get_by_text("An animal locomotion paper", exact=True).wait_for()
                assert "A consciousness paper" not in await papers.inner_text()

                await page.reload()
                await page.get_by_text("An animal locomotion paper", exact=True).wait_for()
                assert await page.locator("#workspace").input_value() == animal["id"]
                assert "A consciousness paper" not in await papers.inner_text()

                async def name_workspace(dialog):
                    await dialog.accept("Embodied cognition")

                page.once("dialog", name_workspace)
                await page.locator("#btn-workspace-add").click()
                await page.locator("#workspace").select_option(label="Embodied cognition")
                await page.get_by_text("Nothing here yet", exact=False).wait_for()
                assert "An animal locomotion paper" not in await papers.inner_text()

            await browser.close()

    asyncio.run(scenario())


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


@pytest.mark.browser
def test_removing_another_paper_from_the_menu_redraws_around_an_open_editor():
    _paper("paper-a", "Paper A")
    _paper("paper-b", "Paper B")

    async def scenario():
        draft_text = "An edit on Paper B that outlives removing Paper A."

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
            with _server() as url:
                await page.goto(url)
                menu = page.locator("#ctxmenu")
                textarea = page.locator('form[data-form="paper-b-c1"] textarea[name="text"]')
                cards_a = page.locator('#content .claim[data-paper="paper-a"]')
                await cards_a.first.wait_for()
                await page.locator('[data-act="edit"][data-claim="paper-b-c1"]').click()
                await textarea.fill(draft_text)

                await page.locator('#papers [data-paper="paper-a"]').click(button="right")
                await menu.get_by_role("button", name="Remove paper").click()
                await page.locator('#papers [data-paper="paper-a"]').wait_for(state="detached")

                # Paper A's cards leave with it even though Paper B's editor is open,
                # and that editor keeps what was typed.
                await cards_a.first.wait_for(state="detached")
                assert await textarea.input_value() == draft_text

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_stale_confirmed_tension_can_be_confirmed_again_without_reopening():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [
        {"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension", "note": "n"},
    ], shown)
    tid = store.tension_rows()[0]["id"]
    store.set_tension_status(tid, "confirmed")
    store.update_claim("paper-a", "paper-a-c1", {"text": "A reworded claim from Paper A."})
    assert store.tension_rows()[0]["stale"] is True

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#tensions-nav [data-view="tensions"]').click()
                card = page.locator(f'.tcard[data-tension="{tid}"]')
                stale = card.locator(".stale")
                await stale.wait_for(state="visible")
                assert await card.locator(".st").text_content() == "confirmed"

                # Still confirmed, so the same-status decision is on offer.
                confirm = card.get_by_role("button", name="Confirm")
                assert await confirm.count() == 1
                await confirm.click()
                await stale.wait_for(state="detached")

                card = page.locator(f'.tcard[data-tension="{tid}"]')
                assert await card.locator(".st").text_content() == "confirmed"
                assert await card.get_by_role("button", name="Confirm").count() == 0
                assert await card.get_by_role("button", name="Reopen").count() == 1
            await browser.close()

    asyncio.run(scenario())
    [tension] = store.tension_rows()
    assert tension["status"] == "confirmed" and tension["stale"] is False


@pytest.mark.browser
def test_a_synthesis_sits_under_its_topic_cites_claims_and_can_be_corrected_by_hand():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    store.record_synthesis("recovery", "Both papers report it [paper-a-c1, paper-b-c1].",
                           {r["id"]: r for r in store.claim_rows()})
    store.update_claim("paper-a", "paper-a-c1", {"text": "A reworded claim from Paper A."})
    assert store.synthesis_rows()[0]["stale"] is True

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                synth = page.locator('.synth[data-topic="recovery"]')
                await synth.wait_for(state="visible")
                # Under the topic heading, above the claims.
                group = page.locator(".group", has=synth)
                assert await group.locator("h3").text_content() is not None
                order = await group.evaluate(
                    "g => [...g.children].map(c => c.className.split(' ')[0])")
                assert order.index("synth") < order.index("claim")
                assert await synth.locator(".stale").count() == 1

                # The citations are two markers; clicking one selects that claim.
                cites = synth.locator(".cite")
                assert await cites.count() == 2
                assert await cites.nth(1).get_attribute("title") == "A claim from Paper B."
                await cites.nth(1).click()
                await page.locator('.claim.sel[data-claim="paper-b-c1"]').wait_for(state="visible")

                # A search that hides the cited claim gives way to the citation:
                # without that the selection would fall back to the first
                # visible card, Paper A's, while the link said Paper B.
                await page.fill("#q", "Paper A")
                await page.locator('.claim[data-claim="paper-b-c1"]').wait_for(state="hidden")
                await page.locator('.claim.sel[data-claim="paper-a-c1"]').wait_for(state="visible")
                await cites.nth(1).click()
                await page.locator('.claim.sel[data-claim="paper-b-c1"]').wait_for(state="visible")
                assert await page.input_value("#q") == ""

                # Correcting it by hand clears the stale mark and records the text.
                # While the save is in flight the editor is frozen, as a claim
                # form is, so nothing typed meanwhile is lost to the redraw.
                release_save = asyncio.Event()

                async def hold_save(route):
                    await release_save.wait()
                    await route.continue_()

                await page.route("**/api/syntheses/recovery", hold_save)
                await synth.get_by_role("button", name="edit").click()
                field = page.locator('textarea[data-synth="recovery"]')
                await field.fill("Corrected [paper-a-c1].")
                await page.get_by_role("button", name="Save").click()
                await page.locator('textarea[data-synth="recovery"]:disabled').wait_for()
                assert await page.get_by_role("button", name="Cancel").is_disabled()
                assert await page.get_by_role("button", name="Save").is_disabled()
                release_save.set()
                synth = page.locator('.synth[data-topic="recovery"]')
                await synth.get_by_text("written by hand", exact=False).wait_for()
                assert await synth.locator(".stale").count() == 0
                assert await synth.locator(".cite").count() == 1
            await browser.close()

    asyncio.run(scenario())
    [row] = store.synthesis_rows()
    assert row["text"] == "Corrected [paper-a-c1]." and row["source"] == "hand" and row["stale"] is False


@pytest.mark.browser
def test_a_synthesis_draft_survives_navigation_and_opening_another_topics_editor():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery", "scaling")
    _paper("paper-c", "Paper C", "scaling")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_synthesis("recovery", "Recovery as written.", shown)
    store.record_synthesis("scaling", "Scaling as written.", shown)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                recovery = page.locator('.synth[data-topic="recovery"]')
                scaling = page.locator('.synth[data-topic="scaling"]')
                fields = page.locator("textarea[data-synth]")
                await recovery.wait_for(state="visible")
                await recovery.get_by_role("button", name="edit").click()
                await page.locator('textarea[data-synth="recovery"]').fill("Recovery draft.")

                # Reading a paper closes the editor: nothing is being edited on
                # screen, so the background poll must be free to run. Coming
                # back to All papers does not reopen it.
                await page.click('#papers [data-paper="paper-a"]')
                await page.locator('.claim[data-claim="paper-b-c1"]').wait_for(state="hidden")
                assert await fields.count() == 0
                await page.click('#papers [data-paper=""]')
                await recovery.wait_for(state="visible")
                assert await fields.count() == 0

                # Another topic's editor opens on its own text, not the draft.
                await scaling.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="scaling"]').input_value() == "Scaling as written."
                await page.locator('textarea[data-synth="scaling"]').fill("Scaling draft.")

                # Opening the first again resumes its draft and parks the
                # second's; each topic keeps its own.
                await recovery.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="recovery"]').input_value() == "Recovery draft."
                assert await fields.count() == 1
                await scaling.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="scaling"]').input_value() == "Scaling draft."

                # Cancel drops only that topic's draft.
                await page.get_by_role("button", name="Cancel").click()
                assert await fields.count() == 0
                await scaling.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="scaling"]').input_value() == "Scaling as written."
                await recovery.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="recovery"]').input_value() == "Recovery draft."
            await browser.close()

    asyncio.run(scenario())
    assert [row["text"] for row in store.synthesis_rows()] == ["Recovery as written.", "Scaling as written."]


@pytest.mark.browser
def test_a_claim_being_edited_keeps_its_text_when_a_synthesis_editor_is_cancelled_or_saved():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_synthesis("recovery", "Recovery as written.", shown)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                synth = page.locator('.synth[data-topic="recovery"]')
                synth_field = page.locator('textarea[data-synth="recovery"]')
                claim_field = page.locator('form[data-form="paper-a-c1"] textarea[name="text"]')
                await synth.wait_for(state="visible")
                await page.locator('[data-act="edit"][data-claim="paper-a-c1"]').click()
                await claim_field.fill("Claim text typed before the synthesis editor opened.")

                # Claim fields are only read back on a redraw, so a synthesis
                # action that redraws must capture them first or the typing
                # since the last redraw is gone.
                await synth.get_by_role("button", name="edit").click()
                await claim_field.fill("Claim text typed while the synthesis editor was open.")
                await synth.get_by_role("button", name="Cancel").click()
                await synth_field.wait_for(state="hidden")
                assert await claim_field.input_value() == "Claim text typed while the synthesis editor was open."

                await synth.get_by_role("button", name="edit").click()
                await synth_field.fill("Recovery corrected.")
                await claim_field.fill("Claim text typed before the synthesis was saved.")
                await synth.get_by_role("button", name="Save").click()
                await synth_field.wait_for(state="hidden")
                await synth.get_by_text("Recovery corrected.").wait_for()
                assert await claim_field.input_value() == "Claim text typed before the synthesis was saved."
            await browser.close()

    asyncio.run(scenario())
    assert [row["text"] for row in store.synthesis_rows()] == ["Recovery corrected."]


def test_escape_cancels_only_the_editor_holding_the_cursor_and_a_claim_save_redraws_its_form_away():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_synthesis("recovery", "Recovery as written.", shown)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                synth = page.locator('.synth[data-topic="recovery"]')
                synth_field = page.locator('textarea[data-synth="recovery"]')
                claim_form = page.locator('form[data-form="paper-a-c1"]')
                claim_field = claim_form.locator('textarea[name="text"]')
                await synth.wait_for(state="visible")
                await page.locator('[data-act="edit"][data-claim="paper-a-c1"]').click()
                await claim_field.fill("Claim draft that Escape in the synthesis must keep.")
                await synth.get_by_role("button", name="edit").click()
                await synth_field.fill("Synthesis draft that Escape in the claim must keep.")

                # Escape in the claim textarea closes only the claim editor.
                await claim_field.press("Escape")
                await claim_form.wait_for(state="hidden")
                assert await synth_field.input_value() == "Synthesis draft that Escape in the claim must keep."

                # And Escape in the synthesis textarea closes only the synthesis editor.
                await page.locator('[data-act="edit"][data-claim="paper-a-c1"]').click()
                await claim_field.fill("Claim draft that Escape in the synthesis must keep.")
                await synth_field.press("Escape")
                await synth_field.wait_for(state="hidden")
                assert await claim_field.input_value() == "Claim draft that Escape in the synthesis must keep."

                # Saving the claim while the synthesis editor is open takes the
                # claim's form off the screen and leaves the synthesis draft.
                await synth.get_by_role("button", name="edit").click()
                await synth_field.fill("Synthesis draft kept across a claim save.")
                await claim_field.fill("Claim saved while the synthesis editor was open.")
                await claim_form.get_by_role("button", name="Save").click()
                await claim_form.wait_for(state="hidden")
                await page.get_by_text("Claim saved while the synthesis editor was open.").wait_for()
                assert await synth_field.input_value() == "Synthesis draft kept across a claim save."
            await browser.close()

    asyncio.run(scenario())
    saved = {r["id"]: r for r in store.claim_rows()}
    assert saved["paper-a-c1"]["text"] == "Claim saved while the synthesis editor was open."
    assert [row["text"] for row in store.synthesis_rows()] == ["Recovery as written."]
