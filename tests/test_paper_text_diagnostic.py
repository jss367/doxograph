import json
import pytest
from fastapi.testclient import TestClient
from doxograph import extract, server, store


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("field", ["summary", "relevance"])
@pytest.mark.parametrize("action", ["unchanged", "edit", "clear", "revert"])
def test_reread_midcall_text(legacy, field, action, monkeypatch):
    key = "synthetic"
    p = store.new_paper(key, title="Synthetic")
    p.update(summary="Original summary", relevance="Original relevance")
    p["extraction"] = (
        {"model": "old"}
        if legacy
        else {"model": "old", "summary": p["summary"], "relevance": p["relevance"]}
    )
    store.save_paper(p)
    original = p[field]

    class Client:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            client = TestClient(server.app, base_url="http://127.0.0.1:8765")
            if action != "unchanged":
                client.patch(
                    "/api/papers/" + key,
                    json={field: "" if action == "clear" else "Reader edit"},
                ).raise_for_status()
                if action == "revert":
                    client.patch(
                        "/api/papers/" + key, json={field: original}
                    ).raise_for_status()
            payload = {
                "summary": "New summary",
                "relevance": "New relevance",
                "claims": [],
                "proposed_tags": [],
            }
            block = type("B", (), {"type": "text", "text": json.dumps(payload)})()
            return type(
                "R", (), {"content": [block], "stop_reason": "end_turn", "usage": None}
            )()

    monkeypatch.setattr(extract, "client", lambda: Client())
    monkeypatch.setattr(
        extract, "_pdf_block", lambda key: {"type": "text", "text": "synthetic"}
    )
    result = extract.extract_paper(key)
    expected = "Reader edit" if action == "edit" else "New " + field
    assert result[field] == expected


@pytest.mark.parametrize("field", ["summary", "relevance"])
def test_legacy_edit_before_reread(field):
    p = store.new_paper("legacy", title="Synthetic legacy")
    p.update(
        summary="Old summary", relevance="Old relevance", extraction={"model": "old"}
    )
    store.save_paper(p)
    assert field not in json.loads(store.paper_path("legacy").read_text())["extraction"]
    TestClient(server.app, base_url="http://127.0.0.1:8765").patch(
        "/api/papers/legacy", json={field: "Reader edit"}
    ).raise_for_status()
    p = extract.merge_extraction(
        "legacy", {"summary": "New summary", "relevance": "New relevance", "claims": []}
    )
    assert p[field] == "Reader edit"


@pytest.mark.parametrize("field", ["summary", "relevance"])
def test_legacy_roundtrip_does_not_fill_null_defaults(field):
    p = store.new_paper("legacy", title="Synthetic legacy")
    p.update(
        summary="Old summary", relevance="Old relevance", extraction={"model": "old"}
    )
    store.save_paper(p)
    store.save_paper(store.load_paper("legacy"))
    assert field not in json.loads(store.paper_path("legacy").read_text())["extraction"]
    assert (
        extract.merge_extraction(
            "legacy",
            {"summary": "New summary", "relevance": "New relevance", "claims": []},
        )[field]
        == "New " + field
    )


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("field", ["summary", "relevance"])
def test_browser_edit_undo_clear_reread(engine, field):
    from playwright.async_api import expect
    from browser_support import browser_session, run_browser
    from test_ask_browser import _server

    p = store.new_paper("legacy", title="Synthetic legacy")
    p.update(
        summary="Old summary", relevance="Old relevance", extraction={"model": "old"}
    )
    store.save_paper(p)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + "/#paper=legacy")
                section = page.locator(".paper-" + field)

                async def edit(text):
                    await section.locator('[data-act="edit-paper-text"]').click()
                    await page.locator("#ask-lines").fill(text)
                    await page.locator("#ask-lines").press("Control+Enter")
                    await expect(page.locator("#ask")).to_be_hidden()

                await edit("Reader edit")
                await expect(section.locator("p")).to_have_text("Reader edit")
                await page.keyboard.press("Meta+z")
                await expect(section.locator("p")).to_have_text("Old " + field)
                result = extract.merge_extraction(
                    "legacy",
                    {
                        "summary": "New summary",
                        "relevance": "New relevance",
                        "claims": [],
                    },
                )
                assert result[field] == "New " + field
                await page.evaluate("refreshAll()")
                await edit("Another manual edit")
                assert (
                    extract.merge_extraction(
                        "legacy",
                        {
                            "summary": "Third summary",
                            "relevance": "Third relevance",
                            "claims": [],
                        },
                    )[field]
                    == "Another manual edit"
                )
                await page.evaluate("refreshAll()")
                await edit("")
                result = extract.merge_extraction(
                    "legacy",
                    {
                        "summary": "Fourth summary",
                        "relevance": "Fourth relevance",
                        "claims": [],
                    },
                )
                assert result[field] == "Fourth " + field
                await page.evaluate("refreshAll()")
                await expect(section.locator("p")).to_have_text("Fourth " + field)

    run_browser(scenario())


@pytest.mark.parametrize("field", ["summary", "relevance"])
@pytest.mark.parametrize(
    "extraction", [None, {"model": "legacy", "summary": None, "relevance": None}]
)
def test_first_manual_text_on_unread_or_nullable_record_survives(field, extraction):
    paper = store.new_paper("unread", title="Synthetic unread")
    paper["extraction"] = extraction
    store.save_paper(paper)
    client = TestClient(server.app, base_url="http://127.0.0.1:8765")
    for text in ["First manual edit", "Second manual edit"]:
        client.patch("/api/papers/unread", json={field: text}).raise_for_status()
    if extraction is None:
        assert store.load_paper("unread")["extraction"] is None
    paper = extract.merge_extraction(
        "unread",
        {"summary": "Model summary", "relevance": "Model relevance", "claims": []},
    )
    assert paper[field] == "Second manual edit"
