"""A paper's code links say where its repository is.

They are found in the paper's own abstract or arXiv comment when it is added,
and edited by hand after that. Every one is a web address, because the page
and the export render each one as a link.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from browser_support import browser_session, run_browser
from doxograph import code_links, export, ingest, server, store, workspace_archive


def client() -> TestClient:
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


# --- finding them ---------------------------------------------------------

def test_repositories_are_found_in_the_order_the_text_names_them():
    text = ("Code is at https://github.com/Owner/Repo. Weights and a demo: "
            "gitlab.com/group/project.git and https://www.github.com/owner/repo/tree/main.")
    assert code_links.find(text) == ["https://github.com/Owner/Repo", "https://gitlab.com/group/project",
                                     "https://github.com/owner/repo"]


def test_a_link_to_a_user_or_a_page_is_not_a_repository():
    assert code_links.find("See https://github.com/openai and https://example.com/a/b.") == []


def test_a_new_paper_takes_the_repositories_its_abstract_names():
    paper = store.new_paper("doe2026study", abstract="Our code: https://github.com/doe/study.")
    assert paper["code"] == ["https://github.com/doe/study"]
    assert store.new_paper("doe2026other", abstract="Nothing here.")["code"] == []


def test_a_paper_written_before_code_links_reads_them_from_its_abstract():
    paper = store.new_paper("doe2026study", abstract="Released at github.com/doe/study")
    del paper["code"]
    store.save_paper(paper)
    assert store.load_paper("doe2026study")["code"] == ["https://github.com/doe/study"]


def test_links_the_reader_cleared_stay_cleared():
    paper = store.new_paper("doe2026study", abstract="Released at github.com/doe/study")
    paper["code"] = []
    store.save_paper(paper)
    assert store.load_paper("doe2026study")["code"] == []


def test_an_arxiv_import_reads_the_comment_as_well_as_the_abstract(monkeypatch):
    feed = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2607.07916v1</id>
    <published>2026-07-10T00:00:00Z</published>
    <title>Persona Cartography</title>
    <summary>An abstract.</summary>
    <arxiv:comment>12 pages. Code: https://github.com/lab/cartography</arxiv:comment>
    <author><name>A Researcher</name></author>
  </entry>
</feed>"""

    class Response:
        text = feed

    monkeypatch.setattr(ingest, "query_arxiv", lambda params, c: Response())
    assert ingest.fetch_arxiv("2607.07916", None)["code"] == ["https://github.com/lab/cartography"]


# --- setting them ---------------------------------------------------------

def test_links_are_kept_in_order_once_each_with_https_assumed():
    assert code_links.normalize([" github.com/a/b ", "https://github.com/a/b/", "", "http://x.org/c"]) == [
        "https://github.com/a/b", "https://github.com/a/b/", "http://x.org/c",
    ]


@pytest.mark.parametrize("value", ["javascript:alert(1)", "ftp://x.org/a", "not a link", "https://localhost/a"])
def test_anything_but_a_web_address_is_refused(value):
    with pytest.raises(ValueError, match="not a web address"):
        code_links.normalize([value])


def test_patching_a_paper_sets_and_clears_its_code_links():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with client() as c:
        response = c.patch("/api/papers/doe2026study", json={"code": ["github.com/doe/study"]})
        assert response.status_code == 200
        assert response.json()["code"] == ["https://github.com/doe/study"]
        assert c.patch("/api/papers/doe2026study", json={"code": ["javascript:alert(1)"]}).status_code == 422
        assert store.load_paper("doe2026study")["code"] == ["https://github.com/doe/study"]
        assert c.patch("/api/papers/doe2026study", json={"code": None}).json()["code"] == []


def test_the_listing_the_export_and_the_archive_carry_them():
    paper = store.new_paper("doe2026study", title="A Study", abstract="Code: github.com/doe/study")
    store.save_paper(paper)
    assert store.summarize(store.load_paper("doe2026study"))["code"] == ["https://github.com/doe/study"]
    assert '<a href="https://github.com/doe/study">' in export.render()
    markdown = workspace_archive._paper_markdown("doe2026study", store.load_paper("doe2026study"), [], {})
    assert "Code: https://github.com/doe/study" in markdown


def test_the_export_does_not_link_a_hand_edited_script_address():
    paper = store.new_paper("doe2026study", title="A Study")
    paper["code"] = ["javascript:alert(1)"]
    store.save_paper(paper)
    assert "javascript:" not in export.render()


# --- the browser ----------------------------------------------------------

@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_code_links_are_edited_from_paper_actions_and_shown_in_the_overview(engine):
    from playwright.async_api import expect

    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("doe2026study", "A Study", "steering")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}#paper=doe2026study")
                await page.locator(".paper-overview").wait_for()
                await expect(page.locator(".paper-code")).to_have_count(0)
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Code links", exact=True).click()
                await expect(page.locator("#ask-title")).to_have_text("Code for A Study")
                # A refused link keeps the dialog open with the draft in it.
                await page.locator("#ask-input").fill("javascript:alert(1)")
                await page.locator("#ask").get_by_role("button", name="OK", exact=True).click()
                await expect(page.locator("#ask-note")).to_contain_text("Could not save the code links")
                await expect(page.locator("#ask-input")).to_have_value("javascript:alert(1)")
                await _answer(page, "OK", "github.com/doe/study https://gitlab.com/doe/data")
                links = page.locator(".paper-code a")
                await expect(links).to_have_count(2)
                await expect(links.first).to_have_text("github.com/doe/study")
                await expect(links.first).to_have_attribute("href", "https://github.com/doe/study")
                assert store.load_paper("doe2026study")["code"] == [
                    "https://github.com/doe/study", "https://gitlab.com/doe/data"]

                target = page.locator('#papers li[data-paper="doe2026study"]')
                await target.click(button="right")
                await page.locator("#ctxmenu").get_by_role("button", name="Edit code links…").click()
                await expect(page.locator("#ask-input")).to_have_value(
                    "https://github.com/doe/study\nhttps://gitlab.com/doe/data")
                await _answer(page, "OK", "")
                await expect(page.locator(".paper-code")).to_have_count(0)
                assert store.load_paper("doe2026study")["code"] == []
                assert not errors

    run_browser(scenario())
