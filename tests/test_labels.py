"""Labels mark the paper rather than anything it claims.

Topics come out of the model and describe what a claim is about; labels are
put on by hand and describe the paper — that it is open source, that a model
of it is on HuggingFace, that it is on the reading list. Nothing a pass does
may write them, and nothing that reads a claim may go stale because one
changed.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from browser_support import browser_session, run_browser
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


def test_labels_preserve_spaces_and_deduplicate_normalized_whitespace():
    assert store.normalize_labels(["Open Source", " open  source\t", "open-source", "  ", "Hugging Face"]) == [
        "hugging face", "open source", "open-source",
    ]


def test_setting_labels_replaces_what_was_there():
    _paper("doe2026study", labels=["open-source"])
    store.set_labels("doe2026study", ["huggingface"])
    assert store.load_paper("doe2026study")["labels"] == ["huggingface"]
    store.set_labels("doe2026study", [])
    assert store.load_paper("doe2026study")["labels"] == []


def test_setting_a_label_with_a_comma_rejects_it_without_changing_the_paper():
    _paper("doe2026study", labels=["reading list"])
    with pytest.raises(ValueError, match="Labels cannot contain commas"):
        store.set_labels("doe2026study", ["open source", "theory, review"])
    assert store.load_paper("doe2026study")["labels"] == ["reading list"]


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
        response = c.patch("/api/papers/doe2026study", json={"labels": ["Open Source", " open  source ", "open-source"]})
        assert response.status_code == 200
        assert response.json()["labels"] == ["open source", "open-source"]
    assert store.load_paper("doe2026study")["labels"] == ["open source", "open-source"]


def test_query_filters_and_searches_multiword_labels():
    _paper("one")
    _paper("two", labels=["open-source"])
    store.set_labels("one", ["Open Source"])
    with client() as c:
        for params, expected in (({"label": "open source"}, ["one"]),
                                 ({"q": "open source"}, ["one", "two"])):
            response = c.get("/api/query/papers", params=params)
            assert response.status_code == 200
            assert [paper["key"] for paper in response.json()["items"]] == expected


@pytest.mark.parametrize("bulk", [False, True])
def test_api_rejects_comma_in_label_without_writing_any_labels(bulk):
    _paper("one", labels=["reading list"])
    _paper("two", labels=["read later"])
    with client() as c:
        values = ["open source", "theory, review"]
        response = (c.post("/api/notebook/bulk", json={
            "papers": ["one", "two"], "field": "labels", "values": values,
        }) if bulk else c.patch("/api/papers/one", json={"labels": values}))
    assert response.status_code == 422
    assert "Labels cannot contain commas" in response.text
    assert store.load_paper("one")["labels"] == ["reading list"]
    assert store.load_paper("two")["labels"] == ["read later"]


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
    assert capsys.readouterr().out.strip() == "huggingface, open source"

    args = parser.parse_args(["label", "doe2026study"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "huggingface, open source"

    args = parser.parse_args(["label"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.splitlines() == ["   1 huggingface", "   1 open source"]

    args = parser.parse_args(["label", "doe2026study", "--clear"])
    assert args.func(args) == 0
    assert store.load_paper("doe2026study")["labels"] == []


def test_label_command_reports_a_paper_that_is_not_there(capsys):
    args = __main__.build_parser().parse_args(["label", "nobody2026"])
    assert args.func(args) == 1
    assert "no paper nobody2026" in capsys.readouterr().err


def test_label_command_reports_a_comma_in_a_label(capsys):
    _paper("doe2026study", labels=["reading list"])
    args = __main__.build_parser().parse_args(["label", "doe2026study", "theory, review"])
    assert args.func(args) == 1
    assert "Labels cannot contain commas" in capsys.readouterr().err
    assert store.load_paper("doe2026study")["labels"] == ["reading list"]


# --- the export -----------------------------------------------------------

def test_the_export_shows_a_papers_labels_and_searches_on_them():
    _paper("doe2026study", labels=["open source"])
    store.add_claim("doe2026study", {"text": "A holds."})
    html = export.render()
    assert '<span class="label">open source</span>' in html
    # The claim's own row carries them too, so filtering the export by a label
    # finds the claims of the papers that carry it.
    assert "open source" in html.split('data-hay="')[1].split('"')[0]


# --- the browser ----------------------------------------------------------

@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_label_choices_preserve_server_unicode_normalization(engine):
    from playwright.async_api import expect
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("target", "A Labeled Paper", "steering")
    _browser_paper("other", "Another Paper", "steering")
    original = ["a\ufeffb", "\ufeffedge\ufeff", "reading list"]
    store.set_labels("target", original)
    store.set_labels("other", ["not-apollo"])
    whitespace = [chr(code) for code in range(0x3001) if chr(code).isspace()]
    samples = [f"{space} A{space}{space}B {space}" for space in whitespace] + original

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}#paper=target")
                await page.locator('.paperhead .label').first.wait_for()
                assert await page.evaluate("values => values.map(parseLabels)", samples) == [
                    store.normalize_labels([value]) for value in samples
                ]
                await page.get_by_role("button", name="Labels", exact=True).click()
                choices = page.get_by_role("group", name="Existing labels")
                await expect(choices.locator('[aria-pressed="true"]')).to_have_count(3)
                await choices.get_by_role("button", name="not-apollo", exact=True).click()
                await expect(page.locator('#ask-input')).to_have_value(
                    ', '.join(store.normalize_labels(original) + ['not-apollo'])
                )
                # Saving must also preserve U+FEFF at label boundaries.
                await page.locator('#ask-input').fill(
                    ', '.join(original + ['not-apollo', '\x1cNEW\x85LABEL\x1f'])
                )
                await _answer(page, "OK")
                await expect(page.locator('.paperhead .label')).to_have_count(5)
                assert store.load_paper("target")["labels"] == store.normalize_labels(
                    original + ['not-apollo', 'new label']
                )

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_label_colors_render_without_color_mix(engine):
    import re

    from playwright.async_api import expect
    from test_browser import _paper as _browser_paper, _server

    _browser_paper("target", "A Labeled Paper", "steering")
    _browser_paper("other", "Another Paper", "steering")
    store.set_labels("target", ["apollo"])
    store.set_labels("other", ["not-apollo"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()

            async def old_webview_styles(route):
                response = await route.fetch()
                # Model the unsupported CSS function by dropping its declarations.
                css = re.sub(r'[^{};]+:[^{};]*color-mix\([^{};]*;', '', await response.text())
                await route.fulfill(response=response, body=css)

            await page.route('**/app.css?*', old_webview_styles)
            with _server() as url:
                await page.goto(f"{url}#paper=target")
                chip = page.locator('.paperhead .label')
                await chip.wait_for()
                for mode in ("light", "dark"):
                    await page.evaluate("appearance => applyThemeSettings({appearance, colors: 'slate'})", mode)
                    await expect(chip).to_have_css("border-top-width", "1px")
                    await expect(chip).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
                    await page.get_by_role("button", name="Labels", exact=True).click()
                    choice = page.get_by_role("group", name="Existing labels").get_by_role("button", name="apollo", exact=True)
                    selected = await choice.evaluate("el => getComputedStyle(el).backgroundColor")
                    await choice.click()
                    await expect(choice).not_to_have_css("background-color", selected)
                    await expect(choice).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
                    await page.keyboard.press("Escape")

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_label_choices_toggle_the_draft_and_keep_their_colors(engine):
    from playwright.async_api import expect
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("target", "Training Monitors for Black Box Scheming Detection", "steering")
    _browser_paper("other", "Another Paper", "steering")
    store.set_labels("target", ["apollo"])
    store.set_labels("other", ["apollo", "not-apollo", "reading list", '<b>research</b>'])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}#paper=target")
                await page.locator('.paperhead .label').wait_for()
                hue = await page.locator('.paperhead .label').evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                )
                assert hue
                assert await page.locator('#labels [data-label="apollo"]').evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                ) == hue

                await page.get_by_role("button", name="Labels", exact=True).click()
                choices = page.get_by_role("group", name="Existing labels")
                apollo = choices.get_by_role("button", name="apollo", exact=True)
                other = choices.get_by_role("button", name="not-apollo", exact=True)
                reading = choices.get_by_role("button", name="reading list", exact=True)
                await expect(apollo).to_have_attribute("aria-pressed", "true")
                assert await apollo.evaluate("el => el.style.getPropertyValue('--label-hue')") == hue
                assert await other.evaluate("el => el.style.getPropertyValue('--label-hue')") != hue
                assert await choices.locator('b').count() == 0
                await expect(choices.get_by_role("button", name='<b>research</b>', exact=True)).to_be_visible()

                await other.click()
                await expect(other).to_have_attribute("aria-pressed", "true")
                await expect(page.locator('#ask-input')).to_have_value('apollo, not-apollo')
                # Keyboard activation keeps focus and toggles without submitting.
                await reading.focus()
                await page.keyboard.press("Space")
                await expect(reading).to_be_focused()
                await expect(reading).to_have_attribute("aria-pressed", "true")
                await apollo.click()
                await expect(apollo).to_have_attribute("aria-pressed", "false")
                await expect(page.locator('#ask-input')).to_have_value('not-apollo, reading list')
                assert store.load_paper("target")["labels"] == ["apollo"]

                # Typing and clicking share one normalized, deduplicated draft.
                await page.locator('#ask-input').fill(' Apollo , APOLLO, New  Label')
                await expect(apollo).to_have_attribute("aria-pressed", "true")
                await expect(other).to_have_attribute("aria-pressed", "false")
                await other.click()
                await expect(page.locator('#ask-input')).to_have_value('apollo, new label, not-apollo')
                await _answer(page, "OK")
                await expect(page.locator('.paperhead .label')).to_have_text(['apollo', 'new label', 'not-apollo'])
                assert store.load_paper("target")["labels"] == ['apollo', 'new label', 'not-apollo']

                await page.reload()
                chip = page.locator('.paperhead .label[data-label="apollo"]')
                await chip.wait_for()
                assert await chip.evaluate("el => el.style.getPropertyValue('--label-hue')") == hue
                await page.evaluate("applyThemeSettings({appearance: 'dark', colors: 'slate'})")
                assert await chip.evaluate("el => el.style.getPropertyValue('--label-hue')") == hue

                await page.get_by_role("button", name="Labels", exact=True).click()
                await apollo.click()
                await page.keyboard.press("Escape")
                await expect(page.locator('#ask')).to_be_hidden()
                await expect(chip).to_be_visible()
                assert store.load_paper("target")["labels"] == ['apollo', 'new label', 'not-apollo']
                await page.get_by_role("button", name="Labels", exact=True).click()
                await expect(apollo).to_have_attribute("aria-pressed", "true")
                await _answer(page, "Cancel")

                # The shared dialog must clear label choices for other prompts.
                await page.evaluate("void askText('Another question', {value: 'unchanged'})")
                await expect(choices).to_be_hidden()
                await expect(page.locator('#ask-input')).to_have_value('unchanged')
                await _answer(page, "Cancel")

    run_browser(scenario())


@pytest.mark.browser
def test_labelling_a_paper_filters_the_corpus_and_survives_a_reload():
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("open2026model", "An Open Model", "steering")
    _browser_paper("closed2026model", "A Closed Model", "steering")

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                # Nothing is labelled, so the section is not there to be read.
                assert await page.locator("#labels-sec:visible").count() == 0

                await page.locator('#papers [data-paper="open2026model"]').click()
                await page.get_by_role("button", name="Labels").click()
                await _answer(page, "OK", "Open Source, huggingface, reading-list")
                await page.locator('.paperhead .label', has_text="open source").wait_for()

                # Reopening and saving must retain three distinct labels,
                # including both the multiword and existing hyphenated names.
                await page.get_by_role("button", name="Labels").click()
                await page.locator('#ask').wait_for(state="visible")
                assert await page.locator('#ask-input').input_value() == 'huggingface, open source, reading-list'
                await _answer(page, "OK")
                await page.locator('.paperhead .label', has_text="open source").wait_for()
                assert await page.locator('.paperhead .label').all_text_contents() == [
                    'huggingface', 'open source', 'reading-list',
                ]

                # The sidebar arrives with the first label and filters on click.
                await page.locator('#labels [data-label="open source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for(state="detached")
                assert parse_qs(urlparse(page.url).fragment)["label"] == ["open source"]

                await page.reload()
                await page.wait_for_function("typeof S !== 'undefined' && S.workspace")
                await page.locator('#labels [data-label="open source"].active').wait_for()
                assert await page.locator('#papers [data-paper="closed2026model"]').count() == 0

                # Clicking it again is the way back out.
                await page.locator('#labels [data-label="open source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for()

    run_browser(scenario())


@pytest.mark.browser
def test_taking_the_filtered_label_off_the_open_paper_clears_the_filter():
    # Otherwise the header stands on screen with every one of its claims
    # filtered away, and nothing on the page says why.
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("open2026model", "An Open Model", "steering")
    _browser_paper("other2026model", "Another Open Model", "steering")
    # Two papers carry it, so taking it off one leaves the label in use and
    # the filter standing: the one that has to give way is the filter against
    # the paper on screen, not the label itself.
    store.set_labels("open2026model", ["open-source"])
    store.set_labels("other2026model", ["open-source"])

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}#label=open-source&paper=open2026model")
                await page.locator('.claim', has_text="An Open Model").wait_for()

                await page.get_by_role("button", name="Labels").click()
                await _answer(page, "OK", "")
                await page.locator('.paperhead .label').wait_for(state="detached")

                # The paper is still open, and still shows its claims.
                assert await page.locator('.claim').count() == 1
                assert "label=" not in page.url
                # The label is still on the other paper, so the section stays.
                await page.locator('#labels [data-label="open-source"]').wait_for()

    run_browser(scenario())


@pytest.mark.browser
def test_a_citation_opens_a_paper_the_label_filter_excludes():
    # The tensions view is not narrowed by the label filter, so a citation in
    # it can land on a paper the filter leaves out. Arriving at an empty paper
    # would be the filter outliving the instruction that replaced it.
    from test_browser import _paper as _browser_paper, _server

    _browser_paper("open2026model", "An Open Model", "recovery", year=2026)
    _browser_paper("shut2026model", "A Shut Model", "recovery", year=2025)
    store.set_labels("open2026model", ["open-source"])
    store.record_tensions(
        "recovery",
        [{"claims": ["open2026model-c1", "shut2026model-c1"],
          "kind": "contradiction", "note": "They disagree."}],
        {r["id"]: r for r in store.claim_rows()},
    )

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f"{url}#view=tensions&label=open-source")
                cite = page.locator('[data-act="open-paper"][data-paper="shut2026model"]').first
                await cite.click()

                await page.locator('.paperhead h2', has_text="A Shut Model").wait_for()
                assert await page.locator('.claim').count() == 1
                assert "label=" not in page.url

    run_browser(scenario())
