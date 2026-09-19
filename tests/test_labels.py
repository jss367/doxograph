"""Labels mark the paper rather than anything it claims.

Topics come out of the model and describe what a claim is about; labels are
put on by hand and describe the paper — that it is open source, that a model
of it is on HuggingFace, that it is on the reading list. Nothing a pass does
may write them, and nothing that reads a claim may go stale because one
changed.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from doxograph import __main__, export, server, store


def client() -> TestClient:
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def _paper(key: str, title: str = "A Study", labels: list[str] | None = None) -> dict:
    paper = store.new_paper(key, title=title, year=2026)
    if labels is not None:
        paper["labels"] = labels
    store.save_paper(paper)
    return paper


# --- the store ------------------------------------------------------------

def test_a_new_paper_starts_with_no_labels():
    assert store.new_paper("doe2026study")["labels"] == []


def test_a_paper_written_before_labels_existed_reads_as_unlabelled():
    paper = store.new_paper("doe2026study")
    del paper["labels"]
    store.save_paper(paper)
    assert store.summarize(store.load_paper("doe2026study"))["labels"] == []
    assert store.label_counts(store.all_papers()) == {}


def test_labels_are_slugged_and_deduplicated():
    assert store.normalize_labels(["Open Source", "open-source", "  ", "Hugging Face"]) == [
        "hugging-face", "open-source",
    ]


def test_setting_labels_replaces_what_was_there():
    _paper("doe2026study", labels=["open-source"])
    store.set_labels("doe2026study", ["huggingface"])
    assert store.load_paper("doe2026study")["labels"] == ["huggingface"]
    store.set_labels("doe2026study", [])
    assert store.load_paper("doe2026study")["labels"] == []


def test_label_counts_count_papers_commonest_first():
    _paper("a", labels=["open-source", "huggingface"])
    _paper("b", labels=["open-source"])
    _paper("c")
    assert store.label_counts(store.all_papers()) == {"open-source": 2, "huggingface": 1}


def test_claims_carry_their_papers_labels_for_the_search():
    _paper("doe2026study", labels=["open-source"])
    store.add_claim("doe2026study", {"text": "A holds."})
    assert store.claim_rows()[0]["paper_labels"] == ["open-source"]


def test_a_label_does_not_change_what_a_pass_rests_on():
    # Otherwise labelling a paper would stale every synthesis it appears in
    # and pay for the topic to be written again from identical claims.
    _paper("doe2026study")
    store.add_claim("doe2026study", {"text": "A holds.", "tags": ["steering"]})
    before = store.synthesis_basis(store.claim_rows())
    store.set_labels("doe2026study", ["open-source"])
    assert store.synthesis_basis(store.claim_rows()) == before


def test_renaming_a_topic_leaves_labels_alone():
    # They are separate namespaces: a topic and a label may share a name
    # without one dragging the other around.
    _paper("doe2026study", labels=["steering"])
    store.add_claim("doe2026study", {"text": "A holds.", "tags": ["steering"]})
    store.rename_tag("steering", "activation-steering")
    paper = store.load_paper("doe2026study")
    assert paper["claims"][0]["tags"] == ["activation-steering"]
    assert paper["labels"] == ["steering"]


# --- the API --------------------------------------------------------------

def test_patching_a_paper_sets_its_labels():
    _paper("doe2026study")
    with client() as c:
        response = c.patch("/api/papers/doe2026study", json={"labels": ["Open Source", "open-source"]})
        assert response.status_code == 200
        assert response.json()["labels"] == ["open-source"]
    assert store.load_paper("doe2026study")["labels"] == ["open-source"]


def test_a_null_clears_the_labels_rather_than_writing_one():
    # `label_counts` and the page both walk the field; None would leave a paper
    # neither of them can read.
    _paper("doe2026study", labels=["open-source"])
    with client() as c:
        assert c.patch("/api/papers/doe2026study", json={"labels": None}).json()["labels"] == []


def test_patching_another_field_leaves_the_labels_where_they_were():
    _paper("doe2026study", labels=["open-source"])
    with client() as c:
        assert c.patch("/api/papers/doe2026study", json={"title": "Renamed"}).json()["labels"] \
            == ["open-source"]


def test_state_carries_the_labels_and_their_counts():
    _paper("a", labels=["open-source"])
    _paper("b", labels=["open-source", "huggingface"])
    with client() as c:
        state = c.get("/api/state").json()
    assert state["label_counts"] == {"open-source": 2, "huggingface": 1}
    assert {p["key"]: p["labels"] for p in state["papers"]} == {
        "a": ["open-source"], "b": ["open-source", "huggingface"],
    }


# --- the command line -----------------------------------------------------

def test_label_command_sets_reads_and_lists(capsys):
    _paper("doe2026study")
    parser = __main__.build_parser()

    args = parser.parse_args(["label", "doe2026study", "Open Source", "huggingface"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "huggingface open-source"

    args = parser.parse_args(["label", "doe2026study"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "huggingface open-source"

    args = parser.parse_args(["label"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.split() == ["1", "huggingface", "1", "open-source"]

    args = parser.parse_args(["label", "doe2026study", "--clear"])
    assert args.func(args) == 0
    assert store.load_paper("doe2026study")["labels"] == []


def test_label_command_reports_a_paper_that_is_not_there(capsys):
    args = __main__.build_parser().parse_args(["label", "nobody2026"])
    assert args.func(args) == 1
    assert "no paper nobody2026" in capsys.readouterr().err


# --- the export -----------------------------------------------------------

def test_the_export_shows_a_papers_labels_and_searches_on_them():
    _paper("doe2026study", labels=["open-source"])
    store.add_claim("doe2026study", {"text": "A holds."})
    html = export.render()
    assert '<span class="label">open-source</span>' in html
    # The claim's own row carries them too, so filtering the export by a label
    # finds the claims of the papers that carry it.
    assert "open-source" in html.split('data-hay="')[1].split('"')[0]


# --- the browser ----------------------------------------------------------

@pytest.mark.browser
def test_labelling_a_paper_filters_the_corpus_and_survives_a_reload():
    from test_browser import _answer, _paper as _browser_paper, _server
    from playwright.async_api import async_playwright

    _browser_paper("open2026model", "An Open Model", "steering")
    _browser_paper("closed2026model", "A Closed Model", "steering")

    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                # Nothing is labelled, so the section is not there to be read.
                assert await page.locator("#labels-sec:visible").count() == 0

                await page.locator('#papers [data-paper="open2026model"]').click()
                await page.get_by_role("button", name="Labels").click()
                await _answer(page, "OK", "Open-Source, huggingface")
                await page.locator('.paperhead .label', has_text="open-source").wait_for()

                # The sidebar arrives with the first label and filters on click.
                await page.locator('#labels [data-label="open-source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for(state="detached")
                assert "label=open-source" in page.url

                await page.reload()
                await page.wait_for_function("typeof S !== 'undefined' && S.workspace")
                await page.locator('#labels [data-label="open-source"].active').wait_for()
                assert await page.locator('#papers [data-paper="closed2026model"]').count() == 0

                # Clicking it again is the way back out.
                await page.locator('#labels [data-label="open-source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for()

            await browser.close()

    asyncio.run(scenario())
