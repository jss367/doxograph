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


async def _answer(page, button: str, text: str | None = None) -> None:
    """Answer the app's own dialog, which stands in for confirm and prompt."""
    dialog = page.locator("#ask")
    await dialog.wait_for(state="visible")
    if text is not None:
        await page.locator("#ask-input").fill(text)
    await dialog.get_by_role("button", name=button, exact=True).click()
    await dialog.wait_for(state="hidden")


def _paper(key: str, title: str, *tags: str, year: int | None = None) -> None:
    paper = store.new_paper(key, title=title, year=year)
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


def _paper_with_proposal(key: str, title: str, proposal: str, updated: str) -> None:
    paper = store.new_paper(key, title=title)
    paper["proposed_tags"] = [{"name": proposal, "description": f"About {proposal}."}]
    paper["updated"] = updated
    store.write_json(store.paper_path(key), paper)


@pytest.mark.browser
def test_failed_job_can_be_dismissed_and_stays_gone_after_reload():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                # An invalid PDF fails locally, without network or model calls.
                response = httpx.post(
                    f"{url}/api/upload?extract_now=false",
                    files={"files": ("broken.pdf", b"not a PDF", "application/pdf")},
                )
                assert response.status_code == 200
                await page.goto(url)
                await page.locator("#jobs .job.error").wait_for()
                dismiss = page.get_by_role("button", name="Dismiss notification for broken.pdf")

                # A failed dismissal must leave the error available to retry.
                async def reject_delete(route):
                    await route.fulfill(status=500, json={"detail": "Try again"})

                await page.route("**/api/jobs/*", reject_delete)
                await dismiss.click()
                await page.locator("#toasts .toast",
                                   has_text="Could not dismiss notification: Try again").wait_for()
                assert await page.locator("#jobs .job.error").count() == 1
                await page.unroute("**/api/jobs/*", reject_delete)

                # A native button also supports keyboard dismissal.
                await dismiss.focus()
                await page.keyboard.press("Enter")
                await page.locator("#jobs .job.error").wait_for(state="detached")
                assert httpx.get(f"{url}/api/jobs").json()["jobs"] == []
                await page.reload()
                await page.wait_for_function("typeof S !== 'undefined' && S.workspace")
                assert await page.locator("#jobs .job").count() == 0

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_proposed_topic_cache_is_isolated_between_workspaces():
    from doxograph import config

    timestamp = "2026-09-02T12:00:00+00:00"
    _paper_with_proposal("shared", "Default paper", "consciousness", timestamp)
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper_with_proposal("shared", "Animal paper", "gait-control", timestamp)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator('.proposed .pn', has_text="consciousness").wait_for()

                await page.locator("#workspace").select_option(label="Animal locomotion")
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator('.proposed .pn', has_text="gait-control").wait_for()
                assert await page.locator('.proposed .pn', has_text="consciousness").count() == 0

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_workspace_switch_waits_for_a_pending_paper_removal():
    _paper("shared", "Default paper")
    from doxograph import config

    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper("shared", "Animal paper")

    async def scenario():
        delete_started = asyncio.Event()
        release_delete = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def delay_delete(route, request):
                if request.method == "DELETE":
                    delete_started.set()
                    await release_delete.wait()
                await route.continue_()

            await page.route("**/api/papers/shared", delay_delete)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.get_by_role("button", name="Remove", exact=True).click()
                await _answer(page, "Remove")
                await asyncio.wait_for(delete_started.wait(), timeout=5)

                await page.locator("#workspace").select_option(label="Animal locomotion")
                assert await page.locator("#workspace").input_value() == "default"
                await page.locator("#toasts .toast",
                                   has_text="Wait for the current change").wait_for()

                release_delete.set()
                await page.locator('#papers [data-paper="shared"]').wait_for(state="detached")
                await page.locator("#workspace").select_option(label="Animal locomotion")
                await page.get_by_text("Animal paper", exact=True).wait_for()

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_second_workspace_selection_waits_for_the_one_already_running():
    """A switch starts by sending the held deletes, and that flush ends in a
    read — which is not counted as a change in flight, so the picker stays live
    across it. A second selection made in that gap used to start its own load
    beside the first, and the two then wrote the workspace in whatever order
    their answers landed: the older one finishing last left the page in the
    corpus the reader had already moved off. The newest selection wins."""
    from doxograph import config

    one, _two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper("gait", "An animal locomotion paper")
    embodied = config.create_workspace("Embodied cognition")
    with config.use_workspace(embodied["id"]):
        _paper("mind", "An embodied cognition paper")

    async def scenario():
        armed = asyncio.Event()
        reading = asyncio.Event()
        release_read = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def arm_on_delete(route, request):
                # Armed as the delete goes out, so the read the flush ends in —
                # the gap this is about — is one of the ones held below.
                if request.method == "DELETE":
                    armed.set()
                await route.continue_()

            async def hold_read(route, request):
                if armed.is_set():
                    reading.set()
                    await release_read.wait()
                await route.continue_()

            await page.route("**/api/papers/*/claims/*", arm_on_delete)
            await page.route("**/api/state", hold_read)
            with _server() as url:
                await page.goto(url)
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.wait_for()
                await card.get_by_role("button", name="delete").click()
                await page.locator("#toasts .toast",
                                   has_text="Deleted the claim.").wait_for()

                # The delete waits in the trash for its notice; leaving the
                # workspace is what sends it, and the read it ends in is held.
                await page.locator("#workspace").select_option(label="Animal locomotion")
                await asyncio.wait_for(reading.wait(), timeout=10)

                # Nothing is counted as in flight here, so the picker takes this.
                await page.locator("#workspace").select_option(label="Embodied cognition")
                release_read.set()

                await page.get_by_text("An embodied cognition paper", exact=True).wait_for()
                assert await page.locator("#workspace").input_value() == embodied["id"]
                assert "An animal locomotion paper" not in await page.locator("#papers").inner_text()

            await browser.close()

    asyncio.run(scenario())


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

                await page.locator("#btn-workspace-add").click()
                await _answer(page, "OK", "Embodied cognition")
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
def test_theme_settings_apply_immediately_and_survive_a_reload():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.get_by_role("button", name="Settings").click()
                settings = page.get_by_role("dialog", name="Settings")
                await settings.get_by_label("Dark").check()
                await settings.get_by_label("Forest").check()

                root = page.locator("html")
                assert await root.get_attribute("data-appearance") == "dark"
                assert await root.get_attribute("data-color-theme") == "forest"
                assert await root.evaluate("el => getComputedStyle(el).getPropertyValue('--bg').trim()") == "#121914"

                await page.reload()
                assert await root.get_attribute("data-appearance") == "dark"
                assert await root.get_attribute("data-color-theme") == "forest"
                await page.get_by_role("button", name="Settings").click()
                assert await settings.get_by_label("Dark").is_checked()
                assert await settings.get_by_label("Forest").is_checked()

                await settings.get_by_label("Dim").check()
                await settings.get_by_label("Graphite").check()
                assert await root.get_attribute("data-appearance") == "dim"
                assert await root.get_attribute("data-color-theme") == "graphite"
                assert await root.evaluate("el => getComputedStyle(el).getPropertyValue('--bg').trim()") == "#28282b"
                assert await root.evaluate("el => el.style.colorScheme") == "dark"

                await settings.get_by_role("button", name="Reset").click()
                assert await root.get_attribute("data-appearance") == "system"
                assert await root.get_attribute("data-color-theme") == "slate"

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_right_click_menu_removes_a_paper_without_leaving_the_open_one():
    _paper("paper-a", "Paper A")
    _paper("paper-b", "Paper B")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
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
                assert await page.locator("#ask-title").text_content() == \
                    "Remove Paper A and its claims?"
                await _answer(page, "Remove")
                await paper_a.wait_for(state="detached")

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
                await _answer(page, "Remove")
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
                # screen, so background content updates must be free to run. Coming
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


@pytest.mark.browser
def test_the_map_joins_papers_by_topic_tension_and_ledger_and_a_click_opens_the_paper():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    _paper("paper-c", "Paper C", "gait")
    store.save_ledger([{"id": "L1", "text": "My own claim about recovery."}])
    store.update_claim("paper-a", "paper-a-c1", {
        "ledger_links": [{"claim": "L1", "relation": "supports", "note": ""}],
    })
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [
        {"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension", "note": "n"},
    ], shown)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1200, "height": 800})
            with _server() as url:
                await page.goto(url)
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.locator(".graph-wrap canvas").wait_for()
                # Let the layout settle before reading positions.
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")

                kinds = {(e["type"], e["a"], e["b"]) for e in graph["edges"]}
                # The tension stands in for the topic edge between the same pair.
                assert ("tension", "p:paper-a", "p:paper-b") in kinds
                assert ("topic", "p:paper-a", "p:paper-b") not in kinds
                assert ("ledger", "p:paper-a", "l:L1") in kinds
                assert await page.locator("[data-graph-count]").text_content() == "3 papers · 2 links"

                # Without the tension layer the shared topic is drawn instead.
                await page.locator('[data-graph-opt="tensions"]').uncheck()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                kinds = {(e["type"], e["a"], e["b"]) for e in graph["edges"]}
                assert ("topic", "p:paper-a", "p:paper-b") in kinds
                assert ("tension", "p:paper-a", "p:paper-b") not in kinds
                await page.locator('[data-graph-opt="tensions"]').check()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                assert not any(e["type"] == "topic" and "p:paper-c" in (e["a"], e["b"]) for e in graph["edges"])
                assert {n["id"] for n in graph["nodes"]} == {"p:paper-a", "p:paper-b", "p:paper-c", "l:L1"}

                # Unticking a layer takes its edges off the map without redrawing it.
                await page.locator('[data-graph-opt="ledger"]').uncheck()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                assert not any(e["type"] == "ledger" for e in graph["edges"])
                assert "l:L1" not in {n["id"] for n in graph["nodes"]}

                # Clicking a paper's node opens that paper's claims.
                node = next(n for n in graph["nodes"] if n["id"] == "p:paper-c")
                box = await page.locator(".graph-wrap canvas").bounding_box()
                sx = box["x"] + box["width"] / 2 + graph["tx"] + node["x"] * graph["zoom"]
                sy = box["y"] + box["height"] / 2 + graph["ty"] + node["y"] * graph["zoom"]
                await page.mouse.move(sx, sy)
                await page.locator(".graph-tip", has_text="Paper C").wait_for(state="visible")
                await page.mouse.click(sx, sy)
                await page.locator(".paperhead h2", has_text="Paper C").wait_for()
                assert await page.locator('#papers [data-paper="paper-c"].active').count() == 1
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_map_repaints_on_a_theme_change_omits_independent_links_and_clamps_the_threshold():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    store.save_ledger([{"id": "L1", "text": "Mine."}, {"id": "L2", "text": "Unrelated."}])
    store.update_claim("paper-a", "paper-a-c1", {"ledger_links": [
        {"claim": "L1", "relation": "supports", "note": ""},
        {"claim": "L2", "relation": "independent", "note": ""},
    ]})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1200, "height": 800})
            with _server() as url:
                await page.goto(url)
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.locator(".graph-wrap canvas").wait_for()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                await page.wait_for_timeout(200)
                before = await page.evaluate("document.querySelector('.graph-wrap canvas').toDataURL()")
                # Switch to dark with the graph settled; the canvas must repaint by itself.
                await page.locator("#btn-settings").click()
                await page.locator('#settings-menu input[name="appearance"][value="dark"]').check()
                await page.wait_for_timeout(200)
                after = await page.evaluate("document.querySelector('.graph-wrap canvas').toDataURL()")
                assert after != before, "canvas kept the old theme"
                # ...and what it painted is what a fresh draw paints.
                forced = await page.evaluate("graphDraw(); document.querySelector('.graph-wrap canvas').toDataURL()")
                assert forced == after

                graph = await page.evaluate("window.doxographGraph()")
                ids = {n["id"] for n in graph["nodes"]}
                assert "l:L1" in ids and "l:L2" not in ids, ids
                assert not any(e.get("relation") == "independent" for e in graph["edges"])
                # A threshold above what the corpus can reach is clamped, not honoured.
                await page.evaluate("V.graph.minShared = 99; renderGraph();")
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")
                assert any(e["type"] == "topic" for e in graph["edges"]), graph["edges"]
                label = await page.locator("[data-graph-min]").text_content()
                assert label == "1", label
            await browser.close()

    asyncio.run(scenario())

@pytest.mark.browser
def test_the_research_context_and_ledger_are_edited_in_the_app():
    _paper("paper-a", "Paper A", "recovery")
    store.save_ledger([{"id": "L1", "text": "Recovery is path-dependent."}])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                nav = page.locator('#research-nav [data-view="research"]')
                await nav.get_by_text("1 claims of my own", exact=False).wait_for()
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                assert await form.locator('[name="ledger-text"]').input_value() == "Recovery is path-dependent."

                # Opening the form and leaving without typing leaves no draft
                # behind, so a change made elsewhere is what comes back.
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for(state="visible")
                store.save_context("Written from the shell meanwhile.")
                await page.wait_for_timeout(3000)   # a poll picks it up
                assert "unsaved edits" not in (await nav.text_content())
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                assert await form.locator('[name="context"]').input_value() == "Written from the shell meanwhile."
                # A change made elsewhere while the form is open, untouched,
                # does not turn the form into a draft either.
                store.save_context("Changed again while the form was open.")
                await page.wait_for_timeout(3000)   # a poll replaces S under the open form
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for(state="visible")
                assert "unsaved edits" not in (await nav.text_content())
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                assert await form.locator('[name="context"]').input_value() == "Changed again while the form was open."

                await form.locator('[name="context"]').fill("Steering vectors and what recovers from them.")
                await form.get_by_role("button", name="Add a claim").click()
                rows = form.locator("[data-ledger-row]")
                assert await rows.count() == 2
                assert await rows.nth(1).locator('[name="ledger-id"]').input_value() == "L2"
                await rows.nth(1).locator('[name="ledger-text"]').fill("Steering is reversible.")
                # A poll while the form is open must not redraw it: wait past
                # one tick and check the typed text is still there. Nor must a
                # click on the sidebar item that is already active.
                await page.wait_for_timeout(3000)
                await nav.click()
                assert await rows.nth(1).locator('[name="ledger-text"]').input_value() == "Steering is reversible."
                # Nor must leaving for a paper and coming back: the draft is
                # kept, and the sidebar says so meanwhile.
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for(state="visible")
                await nav.get_by_text("unsaved edits", exact=False).wait_for()
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                rows = form.locator("[data-ledger-row]")
                assert await rows.nth(1).locator('[name="ledger-text"]').input_value() == "Steering is reversible."
                assert await form.locator('[name="context"]').input_value() == "Steering vectors and what recovers from them."
                await form.get_by_role("button", name="Save").click()

                # Back on the claims, and the sidebar counts the new state.
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for(state="visible")
                await nav.get_by_text("context written · 2 claims of my own").wait_for()
                assert "unsaved edits" not in (await nav.text_content())   # the saved form left no draft
                # The editor offers the new ledger claim as a link target.
                await page.locator('.claim[data-claim="paper-a-c1"] button[data-act="edit"]').click()
                options = page.locator('form[data-form] select[name="link-claim"] option')
                assert "L2" in " ".join(await options.all_text_contents())
            await browser.close()

    asyncio.run(scenario())
    assert store.load_context() == "Steering vectors and what recovers from them."
    assert store.load_ledger() == [
        {"id": "L1", "text": "Recovery is path-dependent."},
        {"id": "L2", "text": "Steering is reversible."},
    ]


@pytest.mark.browser
def test_a_refused_research_save_keeps_what_was_typed_and_writes_nothing():
    _paper("paper-a", "Paper A", "recovery")
    store.save_ledger([{"id": "L1", "text": "Recovery is path-dependent."},
                       {"id": "L3", "text": "Steering is reversible."}])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                nav = page.locator('#research-nav [data-view="research"]')
                await nav.get_by_text("2 claims of my own", exact=False).wait_for()
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                await form.locator('[name="context"]').fill("A context that must not be saved.")
                await form.get_by_role("button", name="Add a claim").click()
                rows = form.locator("[data-ledger-row]")
                # The new row takes the first free id, not the row count.
                assert await rows.nth(2).locator('[name="ledger-id"]').input_value() == "L2"
                await rows.nth(2).locator('[name="ledger-id"]').fill("L1")
                await rows.nth(2).locator('[name="ledger-text"]').fill("Typed, then refused.")
                await form.get_by_role("button", name="Save").click()
                await page.locator("#content .warn").get_by_text("used twice", exact=False).wait_for()
                # The form is as typed, not redrawn from what was saved before,
                # and it is editable again now that the save has settled.
                assert await form.locator('[name="context"]').input_value() == "A context that must not be saved."
                assert await form.locator('[name="context"]').is_enabled()
                assert "saving" not in (await form.get_attribute("class"))
                assert await rows.count() == 3
                assert await rows.nth(2).locator('[name="ledger-text"]').input_value() == "Typed, then refused."
            await browser.close()

    asyncio.run(scenario())
    assert store.load_context() == ""
    assert store.load_ledger() == [{"id": "L1", "text": "Recovery is path-dependent."},
                                   {"id": "L3", "text": "Steering is reversible."}]


@pytest.mark.browser
def test_agreements_show_with_a_paper_count_and_can_be_confirmed():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    _paper("paper-c", "Paper C", "recovery")
    store.record_agreements("recovery", [{"claims": ["paper-a-c1", "paper-b-c1", "paper-c-c1"],
                                          "note": "All three report it."}],
                            {r["id"]: r for r in store.claim_rows()})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                # The claim card says how many other papers agree, and the
                # marker opens the agreements view focused on that claim.
                marker = page.locator('.claim[data-claim="paper-a-c1"] .amark')
                await marker.wait_for(state="visible")
                assert "2 other papers agree" in (await marker.text_content())
                await marker.click()
                card = page.locator('.tcard[data-agreement="a1"]')
                await card.wait_for(state="visible")
                assert await card.locator(".kind.agreement").text_content() == "3 papers"
                assert await card.locator(".tgroup .claim").count() == 3
                assert await card.locator(".tnote").text_content() == "All three report it."
                await card.get_by_role("button", name="Confirm").click()
                await page.locator('.tcard.confirmed[data-agreement="a1"]').wait_for(state="visible")
                await page.locator('#agreements-nav', has_text="1 confirmed").wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert store.agreement_rows()[0]["status"] == "confirmed"


@pytest.mark.browser
def test_saving_the_research_form_writes_only_the_fields_that_were_edited():
    _paper("paper-a", "Paper A", "recovery")
    store.save_context("Original context.")
    store.save_ledger([{"id": "L1", "text": "Recovery is path-dependent."}])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                nav = page.locator('#research-nav [data-view="research"]')
                await nav.wait_for(state="visible")
                await nav.click()
                form = page.locator("#research-form")
                await form.wait_for(state="visible")
                # The context changes elsewhere while the form is open; only
                # the ledger is edited here, so only the ledger is written.
                store.save_context("Changed from the shell while the form was open.")
                await page.wait_for_timeout(3000)
                await form.locator('[name="ledger-text"]').fill("Recovery is path-dependent, at every scale.")
                await form.get_by_role("button", name="Save").click()
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for(state="visible")
                await nav.get_by_text("1 claims of my own", exact=False).wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert store.load_context() == "Changed from the shell while the form was open."
    assert store.load_ledger() == [{"id": "L1", "text": "Recovery is path-dependent, at every scale."}]


@pytest.mark.browser
def test_paper_sort_orders_the_list_and_survives_a_reload():
    def paper(key, title, added, year=None):
        row = store.new_paper(key, title=title)
        row["added"] = added
        row["year"] = year
        store.write_json(store.paper_path(key), row)

    paper("old", "Zebra crossings", "2026-01-05T09:00:00+00:00", 2001)
    paper("mid", "Apple orchards", "2026-03-05T09:00:00+00:00", 2020)
    paper("new", "Mango groves", "2026-06-05T09:00:00+00:00", None)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                rows = page.locator("#papers li[data-paper]:not([data-paper=''])")
                await rows.first.wait_for()

                async def keys():
                    return await rows.evaluate_all("els => els.map((el) => el.dataset.paper)")

                assert await keys() == ["new", "mid", "old"]
                assert "2026-06-05" in await rows.first.inner_text()

                sort = page.get_by_label("Sort papers")
                await sort.select_option("added-asc")
                assert await keys() == ["old", "mid", "new"]

                await sort.select_option("year-desc")
                assert await keys() == ["mid", "old", "new"]
                assert "2026-03-05" not in await rows.first.inner_text()

                await sort.select_option("year-asc")
                assert await keys() == ["old", "mid", "new"]

                await sort.select_option("title")
                assert await keys() == ["mid", "new", "old"]

                await page.reload()
                await rows.first.wait_for()
                assert await sort.input_value() == "title"
                assert await keys() == ["mid", "new", "old"]

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_analysis_setting_survives_reload_and_disables_actions():
    from playwright.async_api import expect
    from doxograph import config

    _paper("example", "Example paper", "Memory")
    config.create_workspace("Other")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="example"]').click()
                await page.get_by_role("button", name="Settings", exact=True).click()
                toggle = page.get_by_label("Enable AI analysis")
                await expect(toggle).to_be_checked()
                await toggle.uncheck()
                await expect(page.locator('#ai-settings-status')).to_have_text("AI analysis disabled.")
                for selector in ['#btn-retag', '#btn-tensions', '#btn-agreements', '#btn-synth',
                                 '#auto-extract', '[data-act="reextract"]', '[data-act="retag-one"]']:
                    await expect(page.locator(selector)).to_be_disabled()
                await expect(page.locator('#auto-extract')).not_to_be_checked()
                await expect(page.locator('[data-act="add-claim"]')).to_be_enabled()
                await expect(page.locator('#btn-add')).to_be_enabled()
                await expect(page.locator('#btn-export')).to_be_enabled()

                await page.reload()
                await page.get_by_role("button", name="Settings", exact=True).click()
                await expect(toggle).to_be_enabled()
                await expect(toggle).not_to_be_checked()
                await page.get_by_role("button", name="Close settings").click()
                for view in ["tensions", "agreements"]:
                    await page.locator(f'#{view}-nav [data-view="{view}"]').click()
                    await expect(page.locator(f'[data-act="find-{view}"]')).to_be_disabled()
                await page.locator('#workspace').select_option(label="Other")
                await expect(page.locator('#btn-retag')).to_be_disabled()
                await page.get_by_role("button", name="Settings", exact=True).click()
                await expect(toggle).not_to_be_checked()
                await toggle.check()
                await expect(page.locator('#ai-settings-status')).to_have_text("AI analysis enabled.")
                await expect(page.locator('#btn-retag')).to_be_enabled()
                await expect(page.locator('#auto-extract')).to_be_checked()
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("view", ["tensions", "agreements"])
def test_analysis_controls_follow_settings_changed_in_another_tab(view):
    from playwright.async_api import expect

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            other_page = await context.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'#{view}-nav [data-view="{view}"]').click()
                await page.get_by_role("button", name="Settings", exact=True).click()
                toggle = page.get_by_label("Enable AI analysis")
                await expect(toggle).to_be_checked()

                await other_page.goto(url)
                await other_page.get_by_role("button", name="Settings", exact=True).click()
                other_toggle = other_page.get_by_label("Enable AI analysis")
                for enabled in [False, True]:
                    await other_toggle.set_checked(enabled)
                    status = "enabled" if enabled else "disabled"
                    await expect(other_page.locator('#ai-settings-status')).to_have_text(
                        f"AI analysis {status}."
                    )
                    # Leave the first tab untouched: only its normal state poll
                    # can update the open menu and the rendered action buttons.
                    await expect(toggle).to_be_checked(checked=enabled, timeout=10000)
                    for selector in ['#btn-retag', '#btn-tensions', '#btn-agreements',
                                     '#btn-synth', '#auto-extract', f'[data-act="find-{view}"]']:
                        await expect(page.locator(selector)).to_be_enabled(enabled=enabled)
                    await expect(page.locator('#auto-extract')).to_be_checked(checked=enabled)
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("editor", ["claim", "synthesis"])
def test_analysis_settings_poll_preserves_an_open_editor(editor):
    from playwright.async_api import expect

    _paper("paper-a", "Paper A", "recovery")
    store.record_synthesis("recovery", "Recovery as written.",
                           {row["id"]: row for row in store.claim_rows()})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            other_page = await context.new_page()
            with _server() as url:
                await page.goto(url)
                if editor == "claim":
                    await page.locator('#papers [data-paper="paper-a"]').click()
                    await page.locator('[data-act="edit"][data-claim="paper-a-c1"]').click()
                    field = page.locator('form[data-form="paper-a-c1"] textarea[name="text"]')
                    action = page.locator('[data-act="reextract"]')
                else:
                    await page.locator('.synth[data-topic="recovery"] [data-act="edit-synth"]').click()
                    field = page.locator('textarea[data-synth="recovery"]')
                    action = page.locator('#btn-synth')
                await field.fill("Unsaved draft that polling must preserve.")
                await field.evaluate("el => el.setSelectionRange(8, 13)")
                original_field = await field.element_handle()
                await page.get_by_role("button", name="Settings", exact=True).click()
                toggle = page.get_by_label("Enable AI analysis")

                await other_page.goto(url)
                await other_page.get_by_role("button", name="Settings", exact=True).click()
                for enabled in [False, True]:
                    await other_page.get_by_label("Enable AI analysis").set_checked(enabled)
                    await expect(toggle).to_be_checked(checked=enabled, timeout=10000)
                    await expect(action).to_be_enabled(enabled=enabled)
                    await expect(page.locator('#auto-extract')).to_be_checked(checked=enabled)
                    await expect(field).to_have_value("Unsaved draft that polling must preserve.")
                    assert await original_field.evaluate("el => el.isConnected")
                    assert await field.evaluate("el => [el.selectionStart, el.selectionEnd]") == [8, 13]
            await browser.close()

    asyncio.run(scenario())


def _paper_without_claims(key: str, title: str) -> None:
    paper = store.new_paper(key, title=title)
    store.refresh_status(paper)
    store.save_paper(paper)


@pytest.mark.browser
def test_the_query_filters_the_paper_list_and_says_how_many_match():
    _paper("han2026reports", "Introspection in language models", "introspection")
    _paper("ling2025gait", "Quadruped gait control", "locomotion")
    _paper_without_claims("wu2026silent", "Introspection without any claims yet")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="ling2025gait"]').wait_for()
                meta = page.locator('#papers [data-paper=""] .pm')

                await page.locator("#q").fill("introspection")
                # A paper matching only through its title, with nothing
                # extracted from it yet, is exactly what a title search wants.
                await page.locator('#papers [data-paper="wu2026silent"]').wait_for()
                assert await page.locator('#papers [data-paper="han2026reports"]').count() == 1
                assert await page.locator('#papers [data-paper="ling2025gait"]').count() == 0
                assert await meta.inner_text() == "2 of 3 match"

                # The selected paper stays listed however the query narrows, or
                # there would be no way back to the rest of the corpus. It is
                # still not a match, and must not be counted as one.
                await page.locator("#q").fill("")
                await page.locator('#papers [data-paper="ling2025gait"]').click()
                await page.locator("#q").fill("introspection")
                assert await page.locator('#papers [data-paper="ling2025gait"]').count() == 1
                assert await meta.inner_text() == "2 of 3 match"

                await page.locator("#q").fill("")
                assert await page.locator("#papers li").count() == 4

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_paper_found_by_its_key_or_year_shows_that_paper_s_claims():
    """A sidebar hit that opens onto "no claims match" is worse than no hit."""
    _paper("han2026reports", "Introspection in language models", "introspection")
    _paper("survey", "A survey of everything", "locomotion", year=2019)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="survey"]').wait_for()

                await page.locator("#q").fill("han2026reports")
                assert await page.locator('#papers [data-paper="survey"]').count() == 0
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

                # A year is not in every key, so it has to reach the claims on
                # its own for the sidebar and the list to agree about it.
                await page.locator("#q").fill("2019")
                await page.locator('#papers [data-paper="survey"]').wait_for()
                assert await page.locator('#papers [data-paper="han2026reports"]').count() == 0
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_claim_citation_carries_its_paper_title():
    _paper("introspect", "Introspection in language models", "introspection")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                cite = page.locator('.claim .cmeta [data-act="open-paper"]').first
                await cite.wait_for()
                assert await cite.get_attribute("title") == "Introspection in language models"

            await browser.close()

    asyncio.run(scenario())


def _paper_with_claims(key: str, title: str, texts: list[str], **fields) -> list[str]:
    store.save_paper(store.new_paper(key, title=title))
    return [store.add_claim(key, {"text": text, **fields})["id"] for text in texts]


def _reviewed(key: str) -> dict[str, bool]:
    return {c["id"]: c["reviewed"] for c in store.load_paper(key)["claims"]}


@pytest.mark.browser
def test_reviewing_by_keyboard_advances_and_n_jumps_to_the_next_unreviewed():
    """Review is the app's main work: r marks and moves on, so a paper goes by
    under one key rather than two."""
    one, two, three = _paper_with_claims(
        "doe2026study", "A study", ["One.", "Two.", "Three."], reviewed=False)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim.sel[data-claim="{one}"]').wait_for()

                await page.keyboard.press("r")
                await page.locator(f'.claim.sel[data-claim="{two}"]').wait_for()
                assert (await page.locator(f'.claim[data-claim="{one}"] [data-act="review"]'
                                           ).text_content()).strip() == "reviewed"

                await page.keyboard.press("r")
                await page.locator(f'.claim.sel[data-claim="{three}"]').wait_for()

                # Taking a review back is a correction, made where it is: the
                # selection stays on the claim being corrected.
                await page.keyboard.press("k")
                await page.locator(f'.claim.sel[data-claim="{two}"]').wait_for()
                await page.keyboard.press("r")
                await page.locator(f'.claim.sel.unreviewed[data-claim="{two}"]').wait_for()

                # n goes to the next one nobody has reviewed, and wraps.
                await page.keyboard.press("n")
                await page.locator(f'.claim.sel[data-claim="{three}"]').wait_for()
                await page.keyboard.press("n")
                await page.locator(f'.claim.sel[data-claim="{two}"]').wait_for()

                # With nothing left unreviewed, n says so and stays put.
                await page.keyboard.press("r")
                await page.locator(f'.claim.sel[data-claim="{three}"]').wait_for()
                await page.locator(f'.claim[data-claim="{two}"]:not(.unreviewed)').wait_for()
                await page.keyboard.press("r")
                await page.locator(f'.claim[data-claim="{three}"]:not(.unreviewed)').wait_for()
                await page.keyboard.press("n")
                await page.locator("#toasts .toast", has_text="has been reviewed").wait_for()
                assert await page.locator(f'.claim.sel[data-claim="{three}"]').count() == 1
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: True, two: True, three: True}


@pytest.mark.browser
def test_reviewing_leaves_a_claim_picked_while_the_review_was_in_flight():
    """r moves on when the review lands, but a claim chosen while the request
    was still running is the later decision: advancing on top of it would pull
    the selection back to where the review started."""
    one, two, three = _paper_with_claims(
        "doe2026study", "A study", ["One.", "Two.", "Three."], reviewed=False)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_patch(route, request):
                if request.method == "PATCH":
                    started.set()
                    await release.wait()
                await route.continue_()

            await page.route(f"**/api/papers/doe2026study/claims/{one}", hold_patch)
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim.sel[data-claim="{one}"]').wait_for()

                await page.keyboard.press("r")
                await asyncio.wait_for(started.wait(), timeout=5)
                await page.keyboard.press("j")
                await page.keyboard.press("j")
                await page.locator(f'.claim.sel[data-claim="{three}"]').wait_for()

                release.set()
                await page.locator(f'.claim[data-claim="{one}"]:not(.unreviewed)').wait_for()
                # The auto-advance would have landed on the second claim.
                await page.wait_for_timeout(300)
                assert await page.locator(f'.claim.sel[data-claim="{three}"]').count() == 1
                assert await page.locator(f'.claim.sel[data-claim="{two}"]').count() == 0
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: True, two: False, three: False}


@pytest.mark.browser
def test_marking_a_whole_paper_reviewed_can_be_undone():
    one, two, three = _paper_with_claims(
        "doe2026study", "A study", ["One.", "Two.", "Three."], reviewed=False)
    store.update_claim("doe2026study", two, {"reviewed": True})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                button = page.get_by_role("button", name="Mark 2 reviewed")
                await button.click()
                notice = page.locator("#toasts .toast", has_text="2 claims marked reviewed")
                await notice.wait_for()
                await button.wait_for(state="detached")
                assert _reviewed("doe2026study") == {one: True, two: True, three: True}

                # Undo takes back this decision only: the claim reviewed before
                # it was not part of what was just done.
                await notice.get_by_role("button", name="Undo").click()
                await page.get_by_role("button", name="Mark 2 reviewed").wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: False, two: True, three: False}


@pytest.mark.browser
def test_a_deleted_claim_waits_for_its_notice_and_can_be_brought_back():
    """Undo is the delete never being sent, so the claim comes back as itself:
    same id, and the tensions and syntheses citing it intact."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.wait_for()
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()
                await card.wait_for(state="detached")
                # Off the page, but nothing has been sent yet.
                assert len(store.load_paper("doe2026study")["claims"]) == 2

                await notice.get_by_role("button", name="Undo").click()
                await card.wait_for()
                assert len(store.load_paper("doe2026study")["claims"]) == 2

                # Dismissing the notice sends it rather than dropping it.
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()
                await notice.get_by_role("button", name="Dismiss: Deleted the claim.").click()
                await page.wait_for_function(
                    "() => S.claims.length === 1", timeout=10000)
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two]


@pytest.mark.browser
def test_the_url_carries_the_view_through_a_reload_and_the_back_button():
    _paper("doe2026study", "A study", "recovery")
    _paper("ling2025gait", "Quadruped gait", "locomotion")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.locator(".paperhead h2", has_text="A study").wait_for()
                assert "paper=doe2026study" in page.url

                await page.locator('#tags [data-tag="locomotion"]').click()
                assert "tag=locomotion" in page.url

                # A reload lands where the URL says, filters and all.
                await page.reload()
                await page.locator('#tags [data-tag="locomotion"].active').wait_for()
                assert await page.locator('#papers [data-paper="doe2026study"].active').count() == 1

                # Typing replaces the entry rather than adding one per letter,
                # so Back walks the navigation and not the keystrokes.
                await page.fill("#q", "gait")
                await page.wait_for_function("() => location.hash.includes('q=gait')")
                await page.go_back()
                await page.wait_for_function("() => !location.hash.includes('q=gait')")
                assert await page.input_value("#q") == ""
                assert await page.locator('#tags [data-tag="locomotion"].active').count() == 0
                assert "paper=doe2026study" in page.url

                await page.go_back()
                await page.locator('#papers [data-paper=""].active').wait_for()
                assert "paper=" not in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_slash_reaches_the_search_box_and_question_mark_shows_the_shortcuts():
    _paper("doe2026study", "A study", "recovery")
    _paper("ling2025gait", "Quadruped gait", "locomotion")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()

                await page.keyboard.press("/")
                assert await page.evaluate("document.activeElement.id") == "q"
                await page.keyboard.type("gait")
                await page.locator('#papers [data-paper="doe2026study"]').wait_for(state="detached")

                # Escape in the box belongs to the box: it clears the query and
                # does not reach the editor below.
                await page.keyboard.press("Escape")
                assert await page.input_value("#q") == ""
                await page.locator('#papers [data-paper="doe2026study"]').wait_for()

                # From another view it goes back to the claims, which is what
                # the box filters.
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.locator(".graph-wrap canvas").wait_for()
                await page.keyboard.press("/")
                await page.locator("#content .claim").first.wait_for()
                assert await page.evaluate("document.activeElement.id") == "q"

                await page.keyboard.press("Escape")   # out of the box first
                await page.keyboard.press("?")
                help_sheet = page.locator("#help")
                await help_sheet.wait_for(state="visible")
                assert "mark the claim reviewed" in await help_sheet.inner_text()
                await page.keyboard.press("Escape")
                await help_sheet.wait_for(state="hidden")
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_claim_links_to_its_own_page_of_the_pdf_and_the_paper_menu_has_a_button():
    from pdfs import minimal_pdf

    _paper_with_claims("doe2026study", "A study", ["On page three."],
                       locator="p. 3", quote="On page three.")
    _paper_with_claims("ling2025gait", "Quadruped gait", ["In table two."], locator="Table 2")
    store.pdf_path("doe2026study").write_bytes(minimal_pdf("On page three."))

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                link = page.locator('.claim[data-claim="doe2026study-c1"] .pdflink')
                await link.wait_for()
                assert await link.text_content() == "PDF p.3"
                assert "#page=3" in await link.get_attribute("href")

                # A section or a table is not a page, and is not followed as one.
                assert await page.locator('.claim[data-claim="ling2025gait-c1"] .pdflink').count() == 0

                # The menu the right-click opens has a button of its own now.
                menu = page.locator("#ctxmenu")
                await page.locator('#papers [data-paper="doe2026study"] .pmenu').click()
                await menu.wait_for(state="visible")
                assert "A study" in await menu.inner_text()
                assert await menu.get_by_role("button", name="Open the PDF").count() == 1
                # ...and a paper with no PDF is not offered one.
                await page.keyboard.press("Escape")
                await page.locator('#papers [data-paper="ling2025gait"] .pmenu').click()
                await menu.wait_for(state="visible")
                assert await menu.get_by_role("button", name="Open the PDF").count() == 0
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_references_that_could_not_be_read_are_named_where_they_were_pasted():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.fill("#refs", "wibble flimflam")
                await page.locator("#btn-add").click()
                warning = page.locator("#ref-warn")
                await warning.wait_for(state="visible")
                text = await warning.inner_text()
                assert "“wibble”" in text and "“flimflam”" in text
                assert await page.input_value("#refs") == "wibble\nflimflam"
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_export_notice_opens_the_file_it_just_wrote():
    _paper("doe2026study", "A study", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context()
            page = await context.new_page()
            with _server() as url:
                await page.goto(url)
                await page.get_by_role("button", name="Export HTML").click()
                notice = page.locator("#toasts .toast", has_text="Exported to")
                await notice.wait_for()
                assert "doxograph.html" in await notice.inner_text()

                async with context.expect_page() as opened:
                    await notice.get_by_role("button", name="Open", exact=True).click()
                exported = await opened.value
                await exported.wait_for_load_state()
                assert "A claim from A study" in await exported.inner_text("body")
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_back_to_another_workspace_restores_that_entrys_paper_and_filters():
    """The workspace reset passes through a default view on its way, and that
    view must not be written into the URL being restored from."""
    from doxograph import config

    _paper("mind", "A consciousness paper", "qualia")
    animal = config.create_workspace("Animal locomotion")
    with config.use_workspace(animal["id"]):
        _paper("gait", "An animal locomotion paper", "locomotion")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="mind"]').click()
                await page.locator(".paperhead h2", has_text="A consciousness paper").wait_for()

                await page.locator("#workspace").select_option(label="Animal locomotion")
                await page.locator('#papers [data-paper="gait"]').click()
                await page.locator(".paperhead h2", has_text="An animal locomotion paper").wait_for()
                assert f"ws={animal['id']}" in page.url and "paper=gait" in page.url

                # One step back is within the workspace: its own entry, before
                # any paper was opened.
                await page.go_back()
                await page.locator('#papers [data-paper=""].active').wait_for()
                assert await page.locator("#workspace").input_value() == animal["id"]

                # The next crosses the boundary, and that entry's paper comes
                # back with it rather than being lost to the workspace reset.
                await page.go_back()
                await page.locator(".paperhead h2", has_text="A consciousness paper").wait_for()
                assert await page.locator("#workspace").input_value() == "default"
                assert "paper=mind" in page.url
                assert "ws=" not in page.url

                await page.go_forward()
                await page.go_forward()
                await page.locator(".paperhead h2", has_text="An animal locomotion paper").wait_for()
                assert await page.locator("#workspace").input_value() == animal["id"]
                assert "paper=gait" in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_bulk_review_reaches_a_claim_whose_editor_is_open():
    """The open form is read back by every redraw, so a bulk review that only
    corrected the stored draft would be undone by the next save."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.locator(f'[data-act="edit"][data-claim="{one}"]').click()
                form = page.locator(f'form[data-form="{one}"]')
                reviewed = form.locator('[name="reviewed"]')
                await reviewed.wait_for()
                assert not await reviewed.is_checked()
                await form.locator('textarea[name="text"]').fill("One, reworded.")

                await page.get_by_role("button", name="Mark 2 reviewed").click()
                await page.locator("#toasts .toast", has_text="2 claims marked reviewed").wait_for()

                # The editor is still open on the typed text, now ticked, and
                # saving it does not put the review back.
                assert await form.locator('textarea[name="text"]').input_value() == "One, reworded."
                await reviewed.wait_for()
                assert await reviewed.is_checked()
                await form.get_by_role("button", name="Save").click()
                await form.wait_for(state="detached")
            await browser.close()

    asyncio.run(scenario())
    reviewed = _reviewed("doe2026study")
    assert reviewed == {one: True, two: True}
    text = {c["id"]: c["text"] for c in store.load_paper("doe2026study")["claims"]}
    assert text[one] == "One, reworded."


@pytest.mark.browser
def test_history_restores_a_paper_only_when_it_still_exists_and_parks_a_new_claim():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.locator(".paperhead h2", has_text="Paper A").wait_for()

                # A new claim belongs to the paper it was started on, so Back
                # to another paper parks it instead of offering it there.
                await page.get_by_role("button", name="Add claim by hand").click()
                await page.locator('form[data-form="__new__"] textarea[name="text"]').fill(
                    "A draft that belongs to Paper A.")
                await page.locator('#papers [data-paper="paper-b"]').click()
                await page.locator(".paperhead h2", has_text="Paper B").wait_for()
                await page.go_back()
                await page.locator(".paperhead h2", has_text="Paper A").wait_for()
                assert await page.get_by_text("Unsaved new claim", exact=False).count() == 0

                # Paper A is removed while its entry is still in the history.
                await page.locator('#papers [data-paper="paper-b"]').click()
                await page.locator(".paperhead h2", has_text="Paper B").wait_for()
                await page.locator('#papers [data-paper="paper-a"] .pmenu').click()
                await page.locator("#ctxmenu").get_by_role("button", name="Remove paper").click()
                await _answer(page, "Remove")
                await page.locator('#papers [data-paper="paper-a"]').wait_for(state="detached")

                # Back onto the removed paper falls back to the corpus rather
                # than an empty list under no header. Its key is retired, so
                # there is nothing for the URL to keep pointing at.
                await page.go_back()
                await page.locator('#papers [data-paper=""].active').wait_for()
                assert await page.locator(".paperhead h2").count() == 0
                assert "paper=paper-a" not in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_bulk_review_undo_writes_to_the_workspace_it_was_taken_in():
    """The same paper imported into two workspaces has the same claim ids in
    both, so an undo that followed the picker would unreview the wrong corpus."""
    from doxograph import config

    ids = _paper_with_claims("shared", "A study", ["One.", "Two."], reviewed=False)
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _paper_with_claims("shared", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.get_by_role("button", name="Mark 2 reviewed").click()
                notice = page.locator("#toasts .toast", has_text="2 claims marked reviewed")
                await notice.wait_for()

                # The picker moves while the notice is still up. The switch
                # resets the view, so the paper is opened again over there.
                await page.locator("#workspace").select_option(label="Other")
                await page.locator('#papers [data-paper="shared"]').click()
                await page.get_by_role("button", name="Mark 2 reviewed").wait_for()

                await notice.get_by_role("button", name="Undo").click()
                await page.wait_for_timeout(1500)
            await browser.close()

    asyncio.run(scenario())
    # The discriminating assertion: the undo has to have landed in the
    # workspace the review was taken in, which is the one no longer on screen.
    assert _reviewed("shared") == {i: False for i in ids}
    with config.use_workspace(other["id"]):
        assert _reviewed("shared") == {i: False for i in ids}


@pytest.mark.browser
def test_a_refused_workspace_switch_leaves_the_entry_alone():
    """Keeping your drafts refuses the switch, and the entry then describes a
    corpus the page is not in: none of it can be applied, and the address has
    to go back to saying what is on screen."""
    from doxograph import config

    _paper("shared", "Default paper", "recovery")
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _paper("shared", "Other paper", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator(".paperhead h2", has_text="Default paper").wait_for()

                await page.locator("#workspace").select_option(label="Other")
                await page.locator(".plist", has_text="Other paper").wait_for()

                # A draft in this workspace, then Back across the boundary.
                await page.locator('[data-act="edit"][data-claim="shared-c1"]').click()
                field = page.locator('form[data-form="shared-c1"] textarea[name="text"]')
                await field.fill("A draft worth keeping.")
                await page.go_back()
                await _answer(page, "Cancel")

                # Nothing of the other corpus's entry was applied: the key
                # exists in both, so the wrong paper would have opened silently.
                await page.wait_for_timeout(600)
                assert await page.locator("#workspace").input_value() == other["id"]
                assert await page.locator(".paperhead h2").count() == 0
                assert f"ws={other['id']}" in page.url
                assert "paper=shared" not in page.url
                assert await field.input_value() == "A draft worth keeping."
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_link_naming_a_workspace_writes_there_before_the_registry_loads():
    """A request with no workspace named goes to the Default corpus, which is
    the one place a link naming another must never write."""
    from doxograph import config

    other = config.create_workspace("Other")

    async def scenario():
        held = asyncio.Event()
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_registry(route):
                held.set()
                await release.wait()
                await route.continue_()

            await page.route("**/api/workspaces", hold_registry)
            with _server() as url:
                loading = asyncio.ensure_future(page.goto(f"{url}/#ws={other['id']}"))
                await asyncio.wait_for(held.wait(), timeout=10)
                # The registry has not answered yet, and already anything the
                # page sends carries the workspace the link named.
                assert await page.evaluate("currentWorkspaceId") == other["id"]
                assert await page.evaluate("window.doxographWorkspaceId") == other["id"]
                release.set()
                await loading
                assert await page.locator("#workspace").input_value() == other["id"]
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_held_delete_is_sent_once_and_settles_before_its_paper_is_removed():
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.get_by_role("button", name="delete").click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()

                # Two flushes at once — an export while the notice is up, say —
                # must not send two DELETEs and leave a 404 notice standing.
                await page.evaluate("Promise.all([flushTrash(), flushTrash()])")
                assert await page.locator("#toasts .toast", has_text="Could not delete").count() == 0
                assert len(store.load_paper("doe2026study")["claims"]) == 1

                # And a claim still waiting goes before its paper does, rather
                # than failing afterwards under a paper that is not there.
                await card.wait_for(state="detached")
                await page.locator(f'.claim[data-claim="{two}"] [data-act="del"]').click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()
                await page.get_by_role("button", name="Remove", exact=True).click()
                await _answer(page, "Remove")
                await page.locator('#papers [data-paper="doe2026study"]').wait_for(state="detached")
                # Nothing is left waiting: had the claim's delete survived its
                # paper, this would send it under a paper that is not there and
                # leave a failure notice standing over a deletion that worked.
                assert await page.evaluate("trash.size") == 0
                await page.evaluate("flushTrash()")
                await page.wait_for_timeout(500)
                assert await page.locator("#toasts .toast", has_text="Could not delete").count() == 0
            await browser.close()

    asyncio.run(scenario())
    assert store.all_papers() == []


@pytest.mark.browser
def test_a_bulk_review_leaves_a_claim_that_is_waiting_out_a_delete_alone():
    """The button counts the claims on screen, so it has to name them: a claim
    held in the undo window is not part of what was clicked."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.locator(f'.claim[data-claim="{one}"] [data-act="del"]').click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()

                await page.get_by_role("button", name="Mark 1 reviewed").click()
                await page.locator("#toasts .toast", has_text="1 claim marked reviewed").wait_for()

                # Undoing the delete brings the claim back as it was, not
                # reviewed by a decision taken while it was off the page.
                await notice.get_by_role("button", name="Undo").click()
                await page.locator(f'.claim.unreviewed[data-claim="{one}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: False, two: True}


@pytest.mark.browser
def test_r_does_not_move_on_when_the_review_did_not_happen():
    """A save already in flight holds the toggle off. Advancing anyway would
    leave the claim unreviewed with nothing on screen saying so."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        release = asyncio.Event()
        started = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_patch(route, request):
                if request.method == "PATCH":
                    started.set()
                    await release.wait()
                await route.continue_()

            await page.route(f"**/api/papers/doe2026study/claims/{one}", hold_patch)
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim.sel[data-claim="{one}"]').wait_for()
                await page.locator(f'[data-act="edit"][data-claim="{one}"]').click()
                await page.locator(f'form[data-form="{one}"] textarea[name="text"]').fill("One, edited.")
                await page.locator(f'form[data-form="{one}"]').get_by_role("button", name="Save").click()
                await asyncio.wait_for(started.wait(), timeout=5)

                # The save holds the claim; r cannot review it, so it stays put.
                # Read from the state: while the editor is open the card it
                # would carry the selection class on is a form instead.
                await page.keyboard.press("r")
                await page.wait_for_timeout(300)
                assert await page.evaluate("V.selectedId") == one
                release.set()
                await page.get_by_text("One, edited.").wait_for()

                # Once the save has landed, r works and moves on as usual.
                await page.locator(f'.claim.sel[data-claim="{one}"]').wait_for()
                await page.keyboard.press("r")
                await page.locator(f'.claim.sel[data-claim="{two}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: True, two: False}


@pytest.mark.browser
def test_a_deleted_synthesis_takes_its_parked_draft_with_it():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    store.record_synthesis("recovery", "Recovery as written.",
                           {row["id"]: row for row in store.claim_rows()})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                synth = page.locator('.synth[data-topic="recovery"]')
                await synth.get_by_role("button", name="edit").click()
                await page.locator('textarea[data-synth="recovery"]').fill("A draft that is parked.")

                # Reading a paper parks the draft; coming back and deleting the
                # synthesis has to take it too.
                await page.locator('#papers [data-paper="paper-a"]').click()
                await page.locator('.claim[data-claim="paper-b-c1"]').wait_for(state="hidden")
                await page.locator('#papers [data-paper=""]').click()
                await synth.wait_for(state="visible")
                await synth.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the synthesis")
                await notice.wait_for()

                # Undo brings back what was written, not the draft for the one
                # that was deleted.
                await notice.get_by_role("button", name="Undo").click()
                await synth.wait_for(state="visible")
                await synth.get_by_role("button", name="edit").click()
                assert await page.locator('textarea[data-synth="recovery"]').input_value() \
                    == "Recovery as written."
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_undoing_a_bulk_review_freezes_the_editors_it_is_undoing():
    """The forward action freezes them; the undo has to as well, or a Save
    landing after it puts the review back with nothing left to say so."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_undo(route, request):
                if request.method == "POST" and b'"reviewed":false' in (request.post_data_buffer or b""):
                    started.set()
                    await release.wait()
                await route.continue_()

            await page.route("**/api/papers/doe2026study/review", hold_undo)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.locator(f'[data-act="edit"][data-claim="{one}"]').click()
                form = page.locator(f'form[data-form="{one}"]')
                await form.wait_for()

                await page.get_by_role("button", name="Mark 2 reviewed").click()
                notice = page.locator("#toasts .toast", has_text="2 claims marked reviewed")
                await notice.wait_for()
                assert await form.locator('[name="reviewed"]').is_checked()

                await notice.get_by_role("button", name="Undo").click()
                await asyncio.wait_for(started.wait(), timeout=5)
                assert await form.get_by_role("button", name="Save").is_disabled()
                release.set()

                await page.locator(f'form[data-form="{one}"] [name="reviewed"]:not(:checked)').wait_for()
                await form.get_by_role("button", name="Save").click()
                await form.wait_for(state="detached")
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: False, two: False}


@pytest.mark.browser
def test_removing_a_paper_settles_only_that_papers_deletions():
    """Another paper's claim was promised its own eight seconds; removing this
    one is no reason to take them away."""
    a_one, _a_two = _paper_with_claims("paper-a", "Paper A", ["A one.", "A two."])
    _paper_with_claims("paper-b", "Paper B", ["B one."])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{a_one}"] [data-act="del"]').click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()

                await page.locator('#papers [data-paper="paper-b"] .pmenu').click()
                await page.locator("#ctxmenu").get_by_role("button", name="Remove paper").click()
                await _answer(page, "Remove")
                await page.locator('#papers [data-paper="paper-b"]').wait_for(state="detached")

                # Paper A's claim is still waiting, and still undoable.
                assert await page.evaluate("trash.size") == 1
                assert len(store.load_paper("paper-a")["claims"]) == 2
                await notice.get_by_role("button", name="Undo").click()
                await page.locator(f'.claim[data-claim="{a_one}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert len(store.load_paper("paper-a")["claims"]) == 2


@pytest.mark.browser
def test_moving_forward_withdraws_the_question_back_was_asking():
    """Answering it afterwards would switch the corpus for a move the reader
    has already left behind."""
    from doxograph import config

    _paper("shared", "Default paper", "recovery")
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _paper("shared", "Other paper", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator(".paperhead h2", has_text="Default paper").wait_for()
                await page.locator("#workspace").select_option(label="Other")
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator(".paperhead h2", has_text="Other paper").wait_for()

                await page.locator('[data-act="edit"][data-claim="shared-c1"]').click()
                field = page.locator('form[data-form="shared-c1"] textarea[name="text"]')
                await field.fill("A draft worth keeping.")

                # One step back stays in this workspace and closes the editor,
                # keeping its text as a draft. The next crosses the boundary,
                # so it asks about that draft — and Forward arrives before the
                # question is answered.
                await page.go_back()
                await page.locator('#papers [data-paper=""].active').wait_for()
                await page.go_back()
                await page.locator("#ask").wait_for(state="visible")
                await page.go_forward()
                await page.locator("#ask").wait_for(state="hidden")

                await page.wait_for_timeout(600)
                assert await page.locator("#workspace").input_value() == other["id"]
                assert await page.evaluate("V.drafts['shared-c1'].text") == "A draft worth keeping."
                assert f"ws={other['id']}" in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_status_filter_is_part_of_the_view_the_url_carries():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [
        {"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension", "note": "n"},
    ], shown)
    store.set_tension_status(store.tension_rows()[0]["id"], "dismissed")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#tensions-nav [data-view="tensions"]').click()
                await page.locator("#tension-status").select_option("dismissed")
                await page.locator(".tcard.dismissed").wait_for()
                assert "tstatus=dismissed" in page.url

                await page.reload()
                await page.locator(".tcard.dismissed").wait_for()
                assert await page.locator("#tension-status").input_value() == "dismissed"

                # A status the app does not have is ignored rather than
                # leaving the view filtered to nothing.
                await page.goto(f"{url}/#view=tensions&tstatus=wibble")
                await page.locator(".tcard").wait_for()
                assert await page.locator("#tension-status").input_value() == ""
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_bulk_review_waits_for_a_save_already_on_its_way():
    """Freezing the form cannot recall a PATCH already sent: it would land
    after the bulk write and quietly take the review back off."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_patch(route, request):
                if request.method == "PATCH":
                    started.set()
                    await release.wait()
                await route.continue_()

            await page.route(f"**/api/papers/doe2026study/claims/{one}", hold_patch)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.locator(f'[data-act="edit"][data-claim="{one}"]').click()
                form = page.locator(f'form[data-form="{one}"]')
                await form.locator('textarea[name="text"]').fill("One, edited.")
                await form.get_by_role("button", name="Save").click()
                await asyncio.wait_for(started.wait(), timeout=5)

                await page.get_by_role("button", name="Mark 2 reviewed").click()
                await page.locator("#toasts .toast", has_text="Wait for the change in flight").wait_for()
                release.set()
                await page.get_by_text("One, edited.").wait_for()

                # Once it has landed, the bulk review goes through as usual.
                await page.get_by_role("button", name="Mark 2 reviewed").click()
                await page.locator("#toasts .toast", has_text="2 claims marked reviewed").wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: True, two: True}


@pytest.mark.browser
def test_removing_a_paper_names_the_workspace_it_was_asked_in():
    """Settling the paper's held deletes ends in a read, and nothing counts a
    read as a change in flight, so the picker can move in the gap."""
    from doxograph import config

    _paper_with_claims("shared", "Default paper", ["One."])
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _paper_with_claims("shared", "Other paper", ["One."])

    async def scenario():
        sent_to = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def move_after_claim_delete(route, request):
                await route.continue_()
                if request.method == "DELETE":
                    # The switch lands in the gap the flush's read leaves open.
                    await page.evaluate(f"currentWorkspaceId = '{other['id']}'")

            async def record_paper_delete(route, request):
                if request.method == "DELETE":
                    sent_to.append(request.headers.get("x-doxograph-workspace"))
                await route.continue_()

            await page.route("**/api/papers/shared/claims/*", move_after_claim_delete)
            await page.route("**/api/papers/shared", record_paper_delete)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                await page.locator('.claim[data-claim="shared-c1"] [data-act="del"]').click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()

                await page.get_by_role("button", name="Remove", exact=True).click()
                await _answer(page, "Remove")
                for _ in range(100):
                    if sent_to:
                        break
                    await page.wait_for_timeout(100)
            await browser.close()

        assert sent_to == ["default"]

    asyncio.run(scenario())
    from doxograph import config as cfg
    assert store.all_papers() == []
    with cfg.use_workspace(other["id"]):
        assert [p["key"] for p in store.all_papers()] == ["shared"]


@pytest.mark.browser
def test_a_claim_cannot_be_deleted_while_its_paper_is_being_removed():
    """The paper's cards stay on screen and clickable until its DELETE comes
    back. A claim held for deletion in that window is held after the flush that
    was meant to clear them: its Undo would have nothing to restore, and its
    own request would arrive under a paper the server has already dropped."""
    one, _two = _paper_with_claims("shared", "Shared paper", ["One.", "Two."])

    async def scenario():
        in_flight = asyncio.Event()
        release = asyncio.Event()
        claim_deletes = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_paper_delete(route, request):
                if request.method == "DELETE":
                    in_flight.set()
                    await release.wait()
                await route.continue_()

            async def record_claim_delete(route, request):
                if request.method == "DELETE":
                    claim_deletes.append(request.url)
                await route.continue_()

            await page.route("**/api/papers/shared", hold_paper_delete)
            await page.route("**/api/papers/shared/claims/*", record_claim_delete)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="shared"]').click()
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.wait_for()

                await page.get_by_role("button", name="Remove", exact=True).click()
                await _answer(page, "Remove")
                await asyncio.wait_for(in_flight.wait(), 10)

                # The button is still there to be clicked, and it refuses.
                await card.locator('[data-act="del"]').click()
                await page.locator(
                    "#toasts .toast", has_text="Wait for the change in flight"
                ).wait_for()
                assert await page.locator(
                    "#toasts .toast", has_text="Deleted the claim."
                ).count() == 0
                assert await card.count() == 1

                release.set()
                await page.locator('#papers [data-paper="shared"]').wait_for(state="detached")
                # Nothing was left holding a delete for a claim that has gone
                # with its paper, so no request goes out under it and no
                # failure notice stands over a deletion that did happen.
                await page.wait_for_timeout(200)
                assert await page.locator(
                    "#toasts .toast", has_text="Could not delete"
                ).count() == 0
            await browser.close()

        assert claim_deletes == []

    asyncio.run(scenario())
    assert store.all_papers() == []


@pytest.mark.browser
def test_a_model_pass_runs_in_the_workspace_it_was_asked_in():
    """Settling the held deletes ends in a read, and the picker can move in
    that gap — a pass costs real money in whichever corpus it lands."""
    from doxograph import config

    _paper_with_claims("paper-a", "Paper A", ["One."], tags=["recovery"])
    config.create_workspace("Other")

    async def scenario():
        sent_to = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def move_after_claim_delete(route, request):
                await route.continue_()
                if request.method == "DELETE":
                    await page.evaluate("currentWorkspaceId = workspaces[1].id")

            async def record_pass(route, request):
                sent_to.append(request.headers.get("x-doxograph-workspace"))
                await route.fulfill(status=200, json={"queued": 0})

            await page.route("**/api/papers/paper-a/claims/*", move_after_claim_delete)
            await page.route("**/api/tensions", record_pass)
            with _server() as url:
                await page.goto(url)
                await page.locator('.claim[data-claim="paper-a-c1"] [data-act="del"]').click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()
                await page.locator("#btn-tensions").click()
                for _ in range(100):
                    if sent_to:
                        break
                    await page.wait_for_timeout(100)
            await browser.close()

        assert sent_to == ["default"]

    asyncio.run(scenario())


@pytest.mark.browser
def test_undoing_a_bulk_review_waits_for_a_save_already_on_its_way():
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."], reviewed=False)

    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_patch(route, request):
                if request.method == "PATCH":
                    started.set()
                    await release.wait()
                await route.continue_()

            await page.route(f"**/api/papers/doe2026study/claims/{one}", hold_patch)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="doe2026study"]').click()
                await page.get_by_role("button", name="Mark 2 reviewed").click()
                notice = page.locator("#toasts .toast", has_text="2 claims marked reviewed")
                await notice.wait_for()

                # A form saved after the review, then Undo clicked on top of it.
                await page.locator(f'[data-act="edit"][data-claim="{one}"]').click()
                form = page.locator(f'form[data-form="{one}"]')
                await form.locator('textarea[name="text"]').fill("One, edited.")
                await form.get_by_role("button", name="Save").click()
                await asyncio.wait_for(started.wait(), timeout=5)
                await notice.get_by_role("button", name="Undo").click()
                await page.locator("#toasts .toast", has_text="Wait for the change in flight").wait_for()

                # The offer comes back rather than being spent on a refusal.
                release.set()
                await page.get_by_text("One, edited.").wait_for()
                again = page.locator("#toasts .toast", has_text="2 claims marked reviewed")
                await again.get_by_role("button", name="Undo").click()
                await page.locator(f'.claim.unreviewed[data-claim="{one}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert _reviewed("doe2026study") == {one: False, two: False}


@pytest.mark.browser
def test_back_from_a_paper_opened_on_the_map_returns_to_the_map():
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page(viewport={"width": 1200, "height": 800})
            with _server() as url:
                await page.goto(url)
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.locator(".graph-wrap canvas").wait_for()
                await page.wait_for_function("window.doxographGraph().alpha === 0")
                graph = await page.evaluate("window.doxographGraph()")

                node = next(n for n in graph["nodes"] if n["id"] == "p:paper-b")
                box = await page.locator(".graph-wrap canvas").bounding_box()
                sx = box["x"] + box["width"] / 2 + graph["tx"] + node["x"] * graph["zoom"]
                sy = box["y"] + box["height"] / 2 + graph["ty"] + node["y"] * graph["zoom"]
                await page.mouse.click(sx, sy)
                await page.locator(".paperhead h2", has_text="Paper B").wait_for()

                await page.go_back()
                await page.locator(".graph-wrap canvas").wait_for()
                assert "view=graph" in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_no_undo_is_offered_for_a_delete_that_has_already_been_sent():
    """The entry stays in the trash while its request is in flight, which is
    what keeps the row off the screen — so the trash alone cannot say whether
    there is anything left to undo."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{one}"]').wait_for()

                # Another action flushes while the delete's own redraw, which
                # runs before the notice goes up, is still in flight.
                await page.evaluate("""
                  ([id, path]) => (async () => {
                    const held = deleteLater('claim', id, path, 'the claim');
                    flushTrash();
                    await held;
                  })()
                """, [one, f"/api/papers/doe2026study/claims/{one}"])

                assert await page.locator("#toasts .toast", has_text="Deleted the claim.").count() == 0
                await page.locator(f'.claim[data-claim="{one}"]').wait_for(state="detached")
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two]


@pytest.mark.browser
def test_back_from_the_analysis_views_returns_to_the_claims():
    _paper("paper-a", "Paper A", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                for button, view in [("#btn-tensions", "tensions"), ("#btn-agreements", "agreements")]:
                    await page.locator(button).click()
                    await page.locator(f'#{view}-nav [data-view="{view}"].active').wait_for()
                    assert f"view={view}" in page.url
                    await page.go_back()
                    await page.locator('.claim[data-claim="paper-a-c1"]').wait_for()
                    assert "view=" not in page.url
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_flush_takes_deletes_made_while_it_is_running():
    """The page stays interactive while a flush runs, and the export or model
    pass waiting on it must not read a row deleted in the meantime."""
    one, two, three = _paper_with_claims("doe2026study", "A study", ["One.", "Two.", "Three."])

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{one}"]').wait_for()
                await page.evaluate("""
                  ([a, b]) => (async () => {
                    const path = (id) => `/api/papers/doe2026study/claims/${id}`;
                    deleteLater('claim', a, path(a), 'the claim');
                    const flushing = flushTrash();
                    deleteLater('claim', b, path(b), 'the claim');
                    await flushing;
                  })()
                """, [one, two])
                assert await page.evaluate("trash.size") == 0
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [three]


@pytest.mark.browser
def test_a_flush_leaves_alone_a_delete_made_in_another_workspace():
    """The picker can move while a flush is between rounds, and a delete made
    in the workspace the reader went to is owed its own eight seconds — and
    cannot be allowed to fail the action that started the flush."""
    from doxograph import config

    one, two, three = _paper_with_claims("doe2026study", "A study", ["One.", "Two.", "Three."])
    config.create_workspace("Other")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{one}"]').wait_for()
                settled, waiting = await page.evaluate("""
                  ([a, b]) => (async () => {
                    const path = (id) => `/api/papers/doe2026study/claims/${id}`;
                    deleteLater('claim', a, path(a), 'the claim');
                    const flushing = flushTrash();
                    // The gap the drain leaves: the reader moves on and deletes
                    // something in the corpus they moved to.
                    currentWorkspaceId = workspaces[1].id;
                    deleteLater('claim', b, path(b), 'the claim');
                    return [await flushing, trash.has(`claim:${b}`)];
                  })()
                """, [one, two])
                # The other workspace's delete is still waiting out its notice,
                # and its absence from the flush kept the flush a success.
                assert waiting
                assert settled
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two, three]


@pytest.mark.browser
def test_back_returns_to_the_research_form_after_leaving_it():
    _paper("paper-a", "Paper A", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                for leave in ["Cancel", "Save"]:
                    await page.locator('#research-nav [data-view="research"]').click()
                    await page.locator("#research-form").wait_for()
                    assert "view=research" in page.url
                    await page.locator("#research-form").get_by_role(
                        "button", name=leave, exact=True).click()
                    await page.locator('.claim[data-claim="paper-a-c1"]').wait_for()
                    assert "view=research" not in page.url

                    # Back goes back to the form rather than appearing to do
                    # nothing, which is what replacing its entry looked like.
                    await page.go_back()
                    await page.locator("#research-form").wait_for()
                    await page.locator('#research-nav [data-view="research"].active').wait_for()
                    await page.go_forward()
                    await page.locator('.claim[data-claim="paper-a-c1"]').wait_for()
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_url_naming_a_topic_that_is_gone_falls_back_to_the_corpus():
    """Filtering to a topic the sidebar cannot list empties the claims with
    nothing left to click to get out of it."""
    _paper("paper-a", "Paper A", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#tag=gait")
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for()
                assert "tag=gait" not in page.url

                # And a topic that goes while an entry naming it is in history.
                await page.locator('#tags [data-tag="recovery"]').click()
                await page.locator('#tags [data-tag="recovery"].active').wait_for()
                await page.locator('#papers [data-paper="paper-a"]').click()
                store.rename_tag("recovery", "recovery-rate")
                await page.wait_for_timeout(3000)   # a poll picks the rename up
                await page.go_back()
                await page.locator('.claim[data-claim="paper-a-c1"]').wait_for()
                assert "tag=recovery&" not in page.url and not page.url.endswith("tag=recovery")
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_page_closing_does_not_send_a_delete_that_is_already_on_its_way():
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        deletes = []
        release = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def hold_delete(route, request):
                if request.method == "DELETE":
                    deletes.append(request.url)
                    await release.wait()
                await route.continue_()

            await page.route(f"**/api/papers/doe2026study/claims/{one}", hold_delete)
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{one}"] [data-act="del"]').click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()
                await page.evaluate("void flushTrash()")
                for _ in range(50):
                    if deletes:
                        break
                    await page.wait_for_timeout(100)

                # The page is put away while that request is still in flight.
                await page.evaluate("window.dispatchEvent(new PageTransitionEvent('pagehide'))")
                await page.wait_for_timeout(500)
                assert len(deletes) == 1, deletes

                # Let the one request finish before the browser goes away.
                release.set()
                for _ in range(50):
                    if len(store.load_paper("doe2026study")["claims"]) == 1:
                        break
                    await page.wait_for_timeout(100)
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two]


@pytest.mark.browser
def test_a_reworded_quote_is_shown_beside_the_paper_and_can_take_its_wording():
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "An introduction that says very little about anything at all.",
        "We ran the experiment three times.\n"
        "Recovery under steering is a path-dependent out-\ncome across all three\n"
        "model scales. The effect is smaller at 7B.",
    ]))
    claim = store.add_claim(key, {
        "text": "Steered models recover.", "locator": "p. 1",
        "quote": "Steering recovery is path dependant across all three model scales."})
    assert claim["quote_verified"] is False

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(".claim .qflag").wait_for()
                await page.get_by_role("button", name="in the paper").click()

                passage = page.locator(".qctx .qpassage mark")
                await passage.wait_for()
                assert "path-dependent outcome" in await passage.inner_text()
                # The page it is really on, against the page the model named.
                assert "page 2 of 2" in await page.locator(".qctx .qwhere").inner_text()
                # The badge is styled in capitals, as the not-found flag is.
                assert "LOCATOR SAYS P. 1" in await page.locator(".qctx .qlocator").inner_text()
                assert await page.locator(".qdiff del").all_inner_texts() == ["recovery", "path dependant"]
                assert await page.locator(".qdiff del").all_inner_texts() == ["recovery", "path dependant"]
                assert "path-dependent outcome" in await page.locator(".qdiff ins").last.inner_text()

                await page.get_by_role("button", name="Use the paper's wording").click()
                await page.locator(".claim .qflag").wait_for(state="detached")
                assert await page.locator(".qctx").count() == 0

            await browser.close()

    asyncio.run(scenario())

    saved = store.load_paper(key)["claims"][0]
    assert saved["quote"].startswith("Recovery under steering is a path-dependent outcome")
    assert (saved["quote_verified"], saved["quote_page"]) == (True, 2)


@pytest.mark.browser
def test_the_query_takes_words_in_any_order_and_finds_them_in_the_pdfs():
    from pdfs import minimal_pdf

    _paper("han2026reports", "Introspection in language models", "introspection")
    store.update_claim("han2026reports", "han2026reports-c1",
                       {"text": "Recovery under steering is path dependent."})
    # A paper with nothing extracted from it, whose PDF is the only place the
    # word appears at all.
    store.save_paper(store.new_paper("wu2026silent", title="A silent paper", year=2026))
    store.pdf_path("wu2026silent").write_bytes(minimal_pdf([
        "A silent paper",
        "Nobody has read this one yet, but it discusses sandbagging at length.",
    ]))

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()

                # Two words, in the other order, which no substring search finds.
                await page.locator("#q").fill("steering recovery")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

                # A word that is in no claim at all, only in a PDF.
                await page.locator("#q").fill("sandbagging")
                hit = page.locator('.pdfhits .pdfhit [data-paper="wu2026silent"]')
                await hit.wait_for()
                passage = page.locator(".pdfhits .pp mark").first
                assert await passage.inner_text() == "sandbagging"
                assert "p. 2" in await page.locator(".pdfhits .pp").first.inner_text()

                # The hit opens its paper, as a citation does.
                await hit.click()
                await page.locator("#papers li.active").wait_for()
                assert await page.locator("#papers li.active").get_attribute("data-paper") == "wu2026silent"

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_editing_a_quote_closes_the_passage_worked_out_from_the_old_one():
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "Recovery under steering is a path-dependent outcome across all scales."]))
    claim = store.add_claim(key, {
        "text": "Steered models recover.",
        "quote": "Steering recovery is path dependant across all scales."})
    assert claim["quote_verified"] is False

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(".claim .qflag").wait_for()
                await page.get_by_role("button", name="in the paper").click()
                await page.locator(".qctx .qpassage").wait_for()

                # Correcting the quote by hand makes the passage describe a
                # claim that no longer exists, so it goes rather than offering
                # to write the old suggestion back over the new quote.
                await page.get_by_role("button", name="edit").click()
                await page.locator('textarea[name="quote"]').fill(
                    "Recovery under steering is a path-dependent outcome across all scales.")
                await page.get_by_role("button", name="Save").click()
                await page.locator(".claim .qflag").wait_for(state="detached")
                assert await page.locator(".qctx").count() == 0

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_passage_stands_aside_when_the_claim_changes_underneath_it():
    """Another tab, or the CLI, can edit the claim while the pane is open. The
    suggestion was worked out from the quote as it was, and offering to write
    it back would undo that edit."""
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "Recovery under steering is a path-dependent outcome across all scales."]))
    claim = store.add_claim(key, {
        "text": "Steered models recover.",
        "quote": "Steering recovery is path dependant across all scales."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(".claim .qflag").wait_for()
                await page.get_by_role("button", name="in the paper").click()
                await page.locator(".qctx .qpassage").wait_for()

                # Edited from outside this page entirely.
                store.update_claim(key, claim["id"], {"quote": "Recovery under steering"})
                await page.locator(".qctx", has_text="changed while the passage was open").wait_for()
                assert await page.get_by_role("button", name="Use the paper's wording").count() == 0

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_paper_arriving_after_the_search_still_turns_up_in_it():
    from pdfs import minimal_pdf

    _paper("han2026reports", "Introspection in language models", "introspection")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("sandbagging")
                await page.locator(".pdfhits", has_text="No paper's text holds").wait_for()

                # Imported while the query stands. Filtering the answer against
                # the corpus can drop a hit that has gone but cannot add one
                # that has arrived, so the question is asked again.
                store.save_paper(store.new_paper("wu2026silent", title="A silent paper", year=2026))
                store.pdf_path("wu2026silent").write_bytes(minimal_pdf([
                    "A silent paper", "This one discusses sandbagging at length."]))
                await page.locator('.pdfhits [data-paper="wu2026silent"]').wait_for()

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_query_with_no_words_in_it_matches_nothing():
    _paper("han2026reports", "Introspection in language models", "introspection")
    _paper("ling2025gait", "Quadruped gait control", "locomotion")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                # No paper holds "---" as a phrase and it has no words to look
                # for, so it narrows to nothing rather than to everything.
                await page.locator("#q").fill("---")
                await page.locator("#content .empty").wait_for()
                assert await page.locator("#content .claim").count() == 0
                # The "all papers" row stays; no paper does.
                assert await page.locator('#papers li[data-paper]:not([data-paper=""])').count() == 0

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_opening_a_passage_keeps_what_is_typed_in_another_claims_editor():
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "Recovery under steering is a path-dependent outcome across all scales."]))
    first = store.add_claim(key, {"text": "First claim.", "quote": "Recovery under steering"})
    store.add_claim(key, {"text": "Second claim.", "quote": "a path-dependent outcome"})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{first["id"]}"]').wait_for()
                await page.locator(f'.claim[data-claim="{first["id"]}"]').get_by_role(
                    "button", name="edit").click()
                await page.locator('textarea[name="text"]').fill("Half-typed correction")

                # Opening another card's passage redraws the list under the
                # open editor, which has to keep what is in it.
                await page.locator('.claim:not(.edit-wrap)').first.get_by_role(
                    "button", name="in the paper").click()
                await page.locator(".qctx .qpassage").wait_for()
                assert await page.locator('textarea[name="text"]').input_value() == "Half-typed correction"

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_paper_s_wording_is_not_written_over_an_edit_made_elsewhere():
    """While an editor is open the poll leaves the content alone, so a passage
    can outlive the claim it describes without being redrawn."""
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "Recovery under steering is a path-dependent outcome across all scales."]))
    first = store.add_claim(key, {"text": "First claim.",
                                  "quote": "Steering recovery is path dependant across all scales."})
    store.add_claim(key, {"text": "Second claim.", "quote": "a path-dependent outcome"})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                card = page.locator(f'.claim[data-claim="{first["id"]}"]')
                await card.wait_for()
                await card.get_by_role("button", name="in the paper").click()
                await page.locator(".qctx .qpassage").wait_for()

                # An editor open on another claim freezes the content, so the
                # button stays on screen after the claim behind it moves.
                await page.locator(f'.claim[data-claim="{key}-c2"]').get_by_role(
                    "button", name="edit").click()
                await page.locator('textarea[name="text"]').fill("Half-typed correction")
                store.update_claim(key, first["id"], {"quote": "Recovery under steering"})
                await page.wait_for_timeout(3000)      # a poll or two

                await page.get_by_role("button", name="Use the paper's wording").click()
                await page.locator(".warn", has_text="changed while the passage was open").wait_for()
                assert await page.locator('textarea[name="text"]').input_value() == "Half-typed correction"

            await browser.close()

    asyncio.run(scenario())

    assert store.load_paper(key)["claims"][0]["quote"] == "Recovery under steering"


@pytest.mark.browser
def test_a_passage_opens_under_one_copy_of_a_claim_with_several_topics():
    from pdfs import minimal_pdf

    key = "roe2026steering"
    store.save_paper(store.new_paper(key, title="Steering and recovery", year=2026))
    store.pdf_path(key).write_bytes(minimal_pdf([
        "Recovery under steering is a path-dependent outcome across all scales."]))
    store.add_claim(key, {"text": "Steered models recover.", "reviewed": True,
                          "tags": ["activation-steering", "recovery-rate"],
                          "quote": "Recovery under steering"})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                # Grouped by topic, the claim is drawn under both of its tags.
                await page.locator(".claim").first.wait_for()
                assert await page.locator(".claim").count() == 2

                # Beside the copy that was clicked, and only that one.
                await page.get_by_role("button", name="in the paper").nth(1).click()
                await page.locator(".qctx .qpassage").wait_for()
                assert await page.locator(".qctx").count() == 1
                second = page.locator(".claim").nth(1)
                assert await second.locator(".qctx").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_finds_what_the_server_finds_whatever_the_case():
    """JavaScript lowercases one character to one; the server folds properly.
    A query that found the paper through its PDF must not lose the claim."""
    _paper("weber2026strasse", "Die Straße als Metapher", "introspection")
    store.update_claim("weber2026strasse", "weber2026strasse-c1",
                       {"text": "Die Straße ist lang."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                for query in ("Straße", "STRASSE", "strasse"):
                    await page.locator("#q").fill(query)
                    await page.locator("#content .claim").first.wait_for()
                    assert await page.locator("#content .claim").count() == 1, query
                    assert await page.locator('#papers [data-paper="weber2026strasse"]').count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_pdf_result_can_be_reached_from_the_keyboard():
    from pdfs import minimal_pdf

    _paper("han2026reports", "Introspection in language models", "introspection")
    store.save_paper(store.new_paper("wu2026silent", title="A silent paper", year=2026))
    store.pdf_path("wu2026silent").write_bytes(minimal_pdf([
        "A silent paper", "Nobody has read this one yet, but it discusses sandbagging."]))

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("sandbagging")
                hit = page.locator('.pdfhits .pdfhit [data-paper="wu2026silent"]')
                await hit.wait_for()
                # The sidebar can omit a paper whose claims do not match, so
                # this is the only way to the result — it has to be reachable.
                await hit.focus()
                await page.keyboard.press("Enter")
                await page.locator("#papers li.active").wait_for()
                assert await page.locator("#papers li.active").get_attribute("data-paper") == "wu2026silent"

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_folds_a_greek_iota_subscript_as_the_server_does():
    """`casefold` calls the iota subscript a letter; everyone else calls it a
    mark, and stripping it would hide the claim behind a PDF hit."""
    _paper("pap2026greek", "Περὶ τῆς ᾳδούσης", "introspection")
    store.update_claim("pap2026greek", "pap2026greek-c1", {"text": "Ἡ ᾳδουσα λέγει."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("αιδουσα")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_keeps_a_vowel_sign_the_server_keeps():
    """A vowel sign in Devanagari is a letter of the word, not an accent: the
    page must not fold काल and कल together where the papers do not."""
    _paper("sharma2026kaal", "काल और समय", "introspection")
    store.update_claim("sharma2026kaal", "sharma2026kaal-c1", {"text": "यह काल है।"})
    _paper("verma2026kal", "कल और आज", "introspection")
    store.update_claim("verma2026kal", "verma2026kal-c1", {"text": "यह कल है।"})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("काल")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_drops_an_arabic_vowel_mark_as_the_server_does():
    """Arabic vocalization has a combining class, so the server drops it: a
    query for كتاب has to keep the claim that spells it كِتاب."""
    _paper("nasr2026kitab", "الكِتاب والقارئ", "introspection")
    store.update_claim("nasr2026kitab", "nasr2026kitab-c1", {"text": "هذا كِتاب جيد."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("كتاب")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_word_whose_vowels_are_marks_is_one_term_on_the_page_too():
    _paper("sharma2026kitab", "एक किताब", "introspection")
    store.update_claim("sharma2026kitab", "sharma2026kitab-c1", {"text": "यह एक किताब है।"})
    _paper("verma2026baat", "बात की कहानी", "introspection")
    store.update_claim("verma2026baat", "verma2026baat-c1", {"text": "बात की एक नई कहानी।"})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("किताब")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_folds_a_historic_cyrillic_letter_as_the_server_does():
    """`casefold` maps ᲀ to в and `toLowerCase` leaves it alone, so the search
    found the paper by its text while the page hid the claim."""
    _paper("ivanov2026old", "Стаᲀъ и новъ", "introspection")
    store.update_claim("ivanov2026old", "ivanov2026old-c1", {"text": "Стаᲀъ текстъ."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("ставъ")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_the_page_folds_what_a_decomposition_produces():
    """A compatibility character can decompose into one that folds: 𝛓 comes
    out as ς, which is σ to the server and itself to `toLowerCase`."""
    _paper("pap2026sigma", "On 𝛓 and its uses", "introspection")
    store.update_claim("pap2026sigma", "pap2026sigma-c1", {"text": "The value 𝛓 is fixed."})

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator("#content .claim").first.wait_for()
                await page.locator("#q").fill("σ")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 1

            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_an_export_does_not_run_on_a_delete_that_failed():
    """The export is written from what is on file. A held delete that came back
    an error left the claim there, so exporting would hand back a file holding a
    claim the reader watched disappear."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        exports = []

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def refuse_delete(route, request):
                if request.method == "DELETE":
                    await route.fulfill(status=500, json={"detail": "Disk is full"})
                    return
                await route.continue_()

            async def record_export(route, request):
                exports.append(request.url)
                await route.fulfill(status=200, json={"path": "/tmp/out.md"})

            await page.route(f"**/api/papers/doe2026study/claims/{one}", refuse_delete)
            await page.route("**/api/export", record_export)
            with _server() as url:
                await page.goto(url)
                await page.locator(f'.claim[data-claim="{one}"] [data-act="del"]').click()
                await page.locator("#toasts .toast", has_text="Deleted the claim.").wait_for()

                # The export settles the held deletes first, and that is where
                # the failure turns up.
                await page.locator("#btn-export").click()
                await page.locator("#toasts .toast", has_text="Could not delete the claim").wait_for()
                await page.wait_for_timeout(500)
                assert exports == []

                # The claim is back on screen, because it is back on file.
                await page.locator(f'.claim[data-claim="{one}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [one, two]


@pytest.mark.browser
def test_a_kind_the_url_names_that_does_not_exist_is_dropped():
    """A bookmark, a hand-edited address, or a kind a later version stopped
    using. The select has no option to match it, so it reads blank while every
    claim is filtered away — a corpus that looks empty for no visible reason."""
    _paper_with_claims("doe2026study", "A study", ["One.", "Two."], kind="finding")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#kind=wibble")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#content .claim").count() == 2
                assert await page.locator("#kind").input_value() == ""
                assert "kind=" not in await page.evaluate("location.hash")

                # A kind that does exist is kept.
                await page.goto(f"{url}/#kind=finding")
                await page.locator("#content .claim").first.wait_for()
                assert await page.locator("#kind").input_value() == "finding"
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_url_naming_the_research_view_draws_it_at_boot():
    """The editor guard in `render` skips the content pane whenever the view is
    Research, so a bookmark or a reload of `#view=research` left the main pane
    blank with only the nav marked active."""
    _paper("paper-a", "Paper A", "recovery")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}/#view=research")
                await page.locator("#research-form").wait_for(timeout=5000)
                await page.locator('#research-nav [data-view="research"].active').wait_for()

                # A reload of the same address redraws it too.
                await page.reload()
                await page.locator("#research-form").wait_for(timeout=5000)
            await browser.close()

    asyncio.run(scenario())


@pytest.mark.browser
def test_a_held_claim_leaves_the_analysis_views_until_its_delete_is_undone():
    """A claim waiting out its undo window is off the page, joined copies and
    all. The tension it holds up goes with it, as it would on the server, and
    the agreement drops it and counts the papers that are left. Undo brings
    both back, since the delete was never sent."""
    _paper("paper-a", "Paper A", "recovery")
    _paper("paper-b", "Paper B", "recovery")
    _paper("paper-c", "Paper C", "recovery")
    shown = {r["id"]: r for r in store.claim_rows()}
    store.record_tensions("recovery", [
        {"claims": ["paper-a-c1", "paper-b-c1"], "kind": "tension", "note": "n"},
    ], shown)
    store.record_agreements("recovery", [{"claims": ["paper-a-c1", "paper-b-c1", "paper-c-c1"],
                                          "note": "All three report it."}], shown)
    tid = store.tension_rows()[0]["id"]

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers [data-paper="paper-a"]').click()
                card = page.locator('.claim[data-claim="paper-a-c1"]')
                await card.wait_for()
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()

                # The tension needs both its claims, so it is not shown at all.
                await page.locator('#tensions-nav [data-view="tensions"]').click()
                await page.locator(".paperhead h2", has_text="Where papers disagree").wait_for()
                assert await page.locator(f'.tcard[data-tension="{tid}"]').count() == 0

                # The agreement keeps the members that are left, and says so.
                await page.locator('#agreements-nav [data-view="agreements"]').click()
                group = page.locator('.tcard[data-agreement="a1"]')
                await group.wait_for()
                assert await group.locator(".kind.agreement").text_content() == "2 papers"
                assert await group.locator('[data-tclaim="paper-a-c1"]').count() == 0
                assert await group.locator(".tgroup .claim").count() == 2

                # Undo puts the claim back where it was cited.
                await notice.get_by_role("button", name="Undo").click()
                await group.locator('[data-tclaim="paper-a-c1"]').wait_for()
                assert await group.locator(".kind.agreement").text_content() == "3 papers"
                await page.locator('#tensions-nav [data-view="tensions"]').click()
                await page.locator(f'.tcard[data-tension="{tid}"]').wait_for()
            await browser.close()

    asyncio.run(scenario())
    assert len(store.load_paper("paper-a")["claims"]) == 1


@pytest.mark.browser
def test_a_held_claim_stops_being_counted_in_the_topic_sidebar():
    """The topic list is the server's count of claims per tag. A claim waiting
    out its undo window is off the page, so it is off that count too: a topic it
    shared drops by one, and a topic it held alone goes away rather than
    offering a filter that would draw nothing. Undo puts both back."""
    _paper("paper-a", "Paper A", "shared", "solo")
    _paper("paper-b", "Paper B", "shared")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#tags [data-tag="solo"]').wait_for()
                assert await page.locator('#tags [data-tag="shared"] .n').text_content() == "2"

                await page.locator('#papers [data-paper="paper-a"]').click()
                card = page.locator('.claim[data-claim="paper-a-c1"]')
                await card.wait_for()
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()

                # The topic that claim held on its own is gone, not left at one.
                # Well inside the undo window, so this is the sidebar keeping up
                # with the hold rather than the delete having gone out.
                await page.locator('#tags [data-tag="solo"]').wait_for(
                    state="detached", timeout=3000)
                assert await page.locator('#tags [data-tag="shared"] .n').text_content() == "1"

                await notice.get_by_role("button", name="Undo").click()
                await page.locator('#tags [data-tag="solo"]').wait_for()
                assert await page.locator('#tags [data-tag="shared"] .n').text_content() == "2"
            await browser.close()

    asyncio.run(scenario())
    assert len(store.load_paper("paper-a")["claims"]) == 1


@pytest.mark.browser
def test_a_delete_is_still_offered_and_sent_when_the_redraw_after_it_fails():
    """The redraw that takes the row off the page can fail — a server restarting
    under the click — and the notice raised after it is what carries the timer
    that sends the delete. It goes up either way, so the row is not left hidden
    by a delete that is never sent and that nothing on screen can undo."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        refusing = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def refuse_read(route, request):
                if refusing.is_set():
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/api/state", refuse_read)
            with _server() as url:
                await page.goto(url)
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.wait_for()

                # The read `deleteLater` ends in is refused, so the refresh
                # after the delete throws — but the delete still has to be
                # scheduled and offered.
                refusing.set()
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()
                assert await notice.get_by_role("button", name="Undo").count() == 1
                refusing.clear()

                # The notice runs out and sends the delete it was holding.
                await notice.wait_for(state="detached", timeout=20000)
                await card.wait_for(state="detached", timeout=20000)
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two]


@pytest.mark.browser
def test_a_deleted_row_leaves_the_screen_when_the_redraw_after_it_fails():
    """Holding the delete takes the row out of the state the page keeps, but only
    a draw takes it off the screen — and the draw inside the refresh never runs
    when that refresh's read is refused. The pruned state is drawn before the
    read, so the card is gone while the undo notice stands rather than sitting
    there clickable until the timer sends the delete out from under it."""
    one, two = _paper_with_claims("doe2026study", "A study", ["One.", "Two."])

    async def scenario():
        refusing = asyncio.Event()

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()

            async def refuse_read(route, request):
                if refusing.is_set():
                    await route.abort()
                    return
                await route.continue_()

            await page.route("**/api/state", refuse_read)
            with _server() as url:
                await page.goto(url)
                card = page.locator(f'.claim[data-claim="{one}"]')
                await card.wait_for()

                refusing.set()
                await card.get_by_role("button", name="delete").click()
                notice = page.locator("#toasts .toast", has_text="Deleted the claim.")
                await notice.wait_for()

                # The notice is up, so the redraw either happened or never will:
                # nothing else on the page is waiting to draw this row away.
                assert await card.count() == 0

                # The other claim and the counts are still there, so this is the
                # pruned state drawn and not a page emptied by the refused read.
                assert await page.locator(f'.claim[data-claim="{two}"]').count() == 1
                assert "1 claims" in await page.locator("#stats").text_content()

                refusing.clear()
                await notice.wait_for(state="detached", timeout=20000)
            await browser.close()

    asyncio.run(scenario())
    assert [c["id"] for c in store.load_paper("doe2026study")["claims"]] == [two]
