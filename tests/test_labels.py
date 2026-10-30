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


def test_labels_preserve_spaces_and_case_and_deduplicate_normalized_whitespace():
    assert store.normalize_labels(["Open Source", " open  source\t", "open-source", "  ", "Hugging Face"]) == [
        "Hugging Face", "Open Source", "open-source",
    ]


def test_labels_sort_without_regard_to_case_and_keep_the_first_spelling():
    assert store.normalize_labels(["rlhf", "Anthropic", "RLHF", "benchmarks"]) == [
        "Anthropic", "benchmarks", "rlhf",
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


def test_label_counts_count_a_label_once_whatever_its_case():
    _paper("a", labels=["rlhf"])
    _paper("b", labels=["RLHF"])
    _paper("c", labels=["RLHF", "reading list"])
    _paper("d", labels=["Reading List"])
    # The spelling most papers use names it; on a tie the capitals win.
    assert store.label_counts(store.all_papers()) == {"RLHF": 3, "Reading List": 2}


def test_label_counts_prefer_capitals_on_a_tie_in_any_script():
    # Georgian lowercase ა (U+10D0) sorts before capital Ა (U+1C90), so code
    # point order alone would pick the lowercase spelling.
    _paper("a", labels=["ანი"])
    _paper("b", labels=["ᲐᲜᲘ"])
    _paper("c", labels=["Ანი"])
    _paper("d", labels=["rlhf"])
    _paper("e", labels=["Rlhf"])
    _paper("f", labels=["RLHF"])
    _paper("g", labels=["rlhf"])
    assert store.label_counts(store.all_papers()) == {"rlhf": 4, "ᲐᲜᲘ": 3}


# Pairs that `lower` alone would keep apart but full case folding joins.
UNICODE_CASE_PAIRS = [("ΟΔΟΣ", "οδοσ"), ("Σ", "ς"), ("Straße", "STRASSE"), ("ẞ", "ss"), ("ﬁle", "FILE")]


@pytest.mark.parametrize("written, other", UNICODE_CASE_PAIRS)
def test_labels_are_one_label_under_full_unicode_case_folding(written, other):
    assert store.label_key(written) == store.label_key(other)
    assert store.normalize_labels([written, other]) == [written]
    _paper("a", labels=[written])
    _paper("b", labels=[other])
    assert list(store.label_counts(store.all_papers()).values()) == [2]
    assert store.has_label(store.load_paper("a"), other)
    with client() as c:
        found = c.get("/api/query/papers", params={"label": other}).json()["items"]
        assert [paper["key"] for paper in found] == ["a", "b"]
        assert c.post("/api/notebook/bulk", json={
            "papers": ["a", "b"], "field": "labels", "mode": "remove", "values": [other],
        }).json() == {"changed": 2}
        assert store.load_paper("a")["labels"] == store.load_paper("b")["labels"] == []
        assert c.post("/api/notebook/bulk", json={
            "papers": ["a"], "field": "labels", "values": [written],
        }).json() == {"changed": 1}
        assert c.post("/api/notebook/bulk", json={
            "papers": ["a"], "field": "labels", "values": [other],
        }).json() == {"changed": 1}
    assert store.load_paper("a")["labels"] == [other]


def test_label_keys_join_the_same_letters_as_casefold():
    groups: dict[str, set[str]] = {}
    folds: dict[str, set[str]] = {}
    letters = [chr(code) for code in range(0x110000)]
    for letter in letters:
        groups.setdefault(store.label_key(letter), set()).add(letter)
        folds.setdefault(letter.casefold(), set()).add(letter)
    assert [c for c in letters if groups[store.label_key(c)] != folds[c.casefold()]] == []
    # Dotless ı stays apart from I and i, as casefold keeps it.
    assert store.normalize_labels(["I", "ı", "i"]) == ["I", "ı"]
    assert store.label_key("ıΣ") == store.label_key("ıς") != store.label_key("IΣ")


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
        assert response.json()["labels"] == ["Open Source", "open-source"]
    assert store.load_paper("doe2026study")["labels"] == ["Open Source", "open-source"]


def test_query_filters_and_searches_multiword_labels():
    _paper("one")
    _paper("two", labels=["open-source"])
    store.set_labels("one", ["Open Source"])
    with client() as c:
        for params, expected in (({"label": "open source"}, ["one"]),
                                 ({"label": "OPEN SOURCE"}, ["one"]),
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


def test_bulk_label_edits_match_labels_without_regard_to_case():
    _paper("one", labels=["rlhf", "reading list"])
    _paper("two", labels=["Reading List"])
    with client() as c:
        added = c.post("/api/notebook/bulk", json={
            "papers": ["one", "two"], "field": "labels", "values": ["RLHF"],
        })
        assert added.json() == {"changed": 2}
        assert store.load_paper("one")["labels"] == ["reading list", "RLHF"]
        assert store.load_paper("two")["labels"] == ["Reading List", "RLHF"]
        removed = c.post("/api/notebook/bulk", json={
            "papers": ["one", "two"], "field": "labels", "mode": "remove", "values": ["reading list"],
        })
        assert removed.json() == {"changed": 2}
    assert store.load_paper("one")["labels"] == ["RLHF"]
    assert store.load_paper("two")["labels"] == ["RLHF"]


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
    _paper("c", labels=["Straße"])
    with client() as c:
        state = c.get("/api/state").json()
    assert state["label_counts"] == {"open-source": 2, "huggingface": 1, "Straße": 1}
    # The page compares stored labels by the server's keys, not its own.
    assert state["label_keys"] == {"open-source": "open-source", "huggingface": "huggingface", "Straße": "strasse"}
    assert {p["key"]: p["labels"] for p in state["papers"]} == {
        "a": ["open-source"], "b": ["open-source", "huggingface"], "c": ["Straße"],
    }


# --- the command line -----------------------------------------------------

def test_label_command_sets_reads_and_lists(capsys):
    _paper("doe2026study")
    parser = __main__.build_parser()

    args = parser.parse_args(["label", "doe2026study", "Open Source", "huggingface"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "huggingface, Open Source"

    args = parser.parse_args(["label", "doe2026study"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.strip() == "huggingface, Open Source"

    args = parser.parse_args(["label"])
    assert args.func(args) == 0
    assert capsys.readouterr().out.splitlines() == ["   1 huggingface", "   1 Open Source"]

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
                await page.locator('.paper-overview .label').first.wait_for()
                assert await page.evaluate("values => values.map(parseLabels)", samples) == [
                    store.normalize_labels([value]) for value in samples
                ]
                await page.locator(".paper-actions > summary").click()
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
                await expect(page.locator('.paper-overview .label')).to_have_count(5)
                assert store.load_paper("target")["labels"] == store.normalize_labels(
                    original + ['not-apollo', 'NEW LABEL']
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
                chip = page.locator('.paper-overview .label')
                await chip.wait_for()
                for mode in ("light", "dark"):
                    await page.evaluate("appearance => applyThemeSettings({appearance, colors: 'slate'})", mode)
                    await expect(chip).to_have_css("border-top-width", "1px")
                    await expect(chip).not_to_have_css("background-color", "rgba(0, 0, 0, 0)")
                    await page.locator(".paper-actions > summary").click()
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
                await page.locator('.paper-overview .label').wait_for()
                hue = await page.locator('.paper-overview .label').evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                )
                assert hue
                assert await page.locator('#labels [data-label="apollo"]').evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                ) == hue

                await page.locator(".paper-actions > summary").click()
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

                # Typing and clicking share one normalized, deduplicated draft,
                # which keeps the first spelling typed.
                await page.locator('#ask-input').fill(' Apollo , APOLLO, New  Label')
                await expect(apollo).to_have_attribute("aria-pressed", "true")
                await expect(other).to_have_attribute("aria-pressed", "false")
                await other.click()
                await expect(page.locator('#ask-input')).to_have_value('Apollo, New Label, not-apollo')
                await _answer(page, "OK")
                await expect(page.locator('.paper-overview .label')).to_have_text(['Apollo', 'New Label', 'not-apollo'])
                assert store.load_paper("target")["labels"] == ['Apollo', 'New Label', 'not-apollo']

                await page.reload()
                # The editor now offers the paper's spelling, in the same color.
                apollo = choices.get_by_role("button", name="Apollo", exact=True)
                chip = page.locator('.paper-overview .label[data-label="Apollo"]')
                await chip.wait_for()
                assert await chip.evaluate("el => el.style.getPropertyValue('--label-hue')") == hue
                await page.evaluate("applyThemeSettings({appearance: 'dark', colors: 'slate'})")
                assert await chip.evaluate("el => el.style.getPropertyValue('--label-hue')") == hue

                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels", exact=True).click()
                await apollo.click()
                await page.keyboard.press("Escape")
                await expect(page.locator('#ask')).to_be_hidden()
                await expect(chip).to_be_visible()
                assert store.load_paper("target")["labels"] == ['Apollo', 'New Label', 'not-apollo']
                await page.locator(".paper-actions > summary").click()
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
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_sidebar_labels_can_be_edited_without_opening_the_paper(engine):
    from playwright.async_api import expect

    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("open2026model", "An Open Model", "steering")
    _browser_paper("closed2026model", "A Closed Model", "steering")
    store.set_labels("closed2026model", ["keep"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(f"{url}#paper=closed2026model")
                target = page.locator('#papers li[data-paper="open2026model"]')
                menu = page.locator("#ctxmenu")
                chip = target.get_by_role("button", name="Read-Next", exact=True)

                await target.click(button="right")
                await menu.get_by_role("button", name="Add labels…").click()
                await expect(page.locator("#ask-title")).to_have_text("Labels for An Open Model")
                await _answer(page, "OK", "Read-Next, open-source")
                await expect(chip).to_be_visible()
                await expect(page.locator(".paperhead h2")).to_have_text("A Closed Model")
                assert store.load_paper("closed2026model")["labels"] == ["keep"]
                hue = await chip.evaluate("el => el.style.getPropertyValue('--label-hue')")
                assert hue
                assert await page.locator('#labels [data-label="Read-Next"]').evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                ) == hue

                # The ellipsis opens the same editor and preserves existing labels.
                await target.locator(".pmenu").click()
                await menu.get_by_role("button", name="Edit labels…").click()
                await expect(page.locator("#ask-input")).to_have_value("open-source, Read-Next")
                choices = page.get_by_role("group", name="Existing labels")
                await expect(choices.locator('[aria-pressed="true"]')).to_have_count(2)
                assert await choices.get_by_role("button", name="Read-Next", exact=True).evaluate(
                    "el => el.style.getPropertyValue('--label-hue')"
                ) == hue
                await _answer(page, "Cancel", "discard-me")
                await expect(chip).to_be_visible()
                assert store.load_paper("open2026model")["labels"] == ["open-source", "Read-Next"]

                # Filtering by a chip leaves an excluded paper and does not open
                # the chip's paper. Keyboard activation toggles the filter off.
                await chip.click()
                await expect(chip).to_have_attribute("aria-pressed", "true")
                await expect(page.locator('#papers li[data-paper="closed2026model"]')).to_have_count(0)
                assert "label=Read-Next" in page.url and "paper=" not in page.url
                await chip.focus()
                await page.keyboard.press("Enter")
                await expect(chip).to_have_attribute("aria-pressed", "false")
                await expect(page.locator('#papers li[data-paper="closed2026model"]')).to_be_visible()

                await page.reload()
                await expect(chip).to_be_visible()
                await target.click(button="right")
                await menu.get_by_role("button", name="Edit labels…").click()
                await _answer(page, "OK", "")
                await expect(target.locator(".labels")).to_have_count(0)
                await expect(page.locator('#labels [data-label="Read-Next"]')).to_have_count(0)
                assert store.load_paper("open2026model")["labels"] == []
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_paper_menu_keyboard_focus_survives_dismissal_and_label_saves(engine):
    from playwright.async_api import expect

    from doxograph import config
    from pdfs import minimal_pdf
    from test_browser import _answer, _paper as _browser_paper, _poll, _server

    _browser_paper("study", "A study", "steering")
    other = config.create_workspace("Other")
    with config.use_workspace(other["id"]):
        _browser_paper("study", "Another workspace's study", "steering")
        store.set_labels("study", ["keep"])

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                paper = page.locator('#papers li[data-paper="study"]')
                trigger = paper.locator(".pmenu")
                menu = page.locator("#ctxmenu")
                action = menu.get_by_role("button", name="Add labels…")

                await trigger.focus()
                await page.keyboard.press("Enter")
                await expect(action).to_be_focused()
                selected_claim = await page.evaluate("V.selectedId")
                await page.keyboard.press("ArrowDown")
                await expect(menu.get_by_role("button", name="Remove paper")).to_be_focused()
                await page.keyboard.press("Home")
                await expect(action).to_be_focused()
                await page.keyboard.press("End")
                await expect(menu.get_by_role("button", name="Remove paper")).to_be_focused()
                assert await page.evaluate("V.selectedId") == selected_claim
                await page.keyboard.press("Escape")
                await expect(menu).to_be_hidden()
                await expect(trigger).to_be_focused()

                # A poll replaces the trigger while the menu owns focus.
                await page.keyboard.press("Enter")
                await expect(action).to_be_focused()
                await _poll(page)
                await page.keyboard.press("Escape")
                await expect(trigger).to_be_focused()

                await page.keyboard.press("Enter")
                await expect(action).to_be_focused()
                await page.keyboard.press("Enter")
                await expect(page.locator("#ask-input")).to_be_focused()
                await page.keyboard.press("Escape")
                await expect(page.locator("#ask")).to_be_hidden()
                await expect(trigger).to_be_focused()

                await page.keyboard.press("Enter")
                await expect(action).to_be_focused()
                await page.keyboard.press("Enter")
                await _answer(page, "OK", "read-next")
                await expect(paper.locator(".label")).to_have_text("read-next")
                await expect(trigger).to_be_focused()

                # Pointer dismissal leaves focus on the chosen field.
                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Edit labels…")).to_be_focused()
                await page.locator("#q").click()
                await expect(menu).to_be_hidden()
                await expect(page.locator("#q")).to_be_focused()

                # When a PDF is available it is first, and arrows still reach labels.
                store.pdf_path("study").write_bytes(minimal_pdf("A synthetic paper."))
                await _poll(page)
                await trigger.focus()
                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Open the PDF")).to_be_focused()
                await page.keyboard.press("ArrowDown")
                await expect(menu.get_by_role("button", name="Edit labels…")).to_be_focused()
                await page.keyboard.press("ArrowUp")
                await expect(menu.get_by_role("button", name="Open the PDF")).to_be_focused()
                await page.keyboard.press("Escape")
                await expect(trigger).to_be_focused()

                # Layout changes dismiss the menu and return its keyboard focus.
                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Open the PDF")).to_be_focused()
                await page.set_viewport_size({"width": 1120, "height": 600})
                await expect(menu).to_be_hidden()
                await expect(trigger).to_be_focused()

                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Open the PDF")).to_be_focused()
                await page.locator("#side").evaluate("side => { side.scrollTop += 64; }")
                await expect(menu).to_be_hidden()
                await expect(trigger).to_be_focused()
                assert await page.locator("#side").evaluate("side => side.scrollTop") > 0

                # Leaving with a menu open must not focus a same-key paper in
                # the destination workspace or carry any label edits into it.
                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Open the PDF")).to_be_focused()
                await page.evaluate("id => switchWorkspace(id)", other["id"])
                await expect(menu).to_be_hidden()
                await expect(paper.locator(".pt")).to_have_text("Another workspace's study")
                await expect(trigger).not_to_be_focused()
                await expect(paper.locator(".label")).to_have_text("keep")

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_scrolling_the_paper_list_dismisses_its_menu_and_restores_focus(engine):
    from playwright.async_api import expect

    from test_browser import _paper as _browser_paper, _server

    for index in range(12):
        _browser_paper(f"study-{index:02}", f"A study {index:02}", "steering")

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                papers = page.locator("#papers")
                trigger = papers.locator(".pmenu").first
                menu = page.locator("#ctxmenu")
                await trigger.focus()
                await page.keyboard.press("Enter")
                await expect(menu.get_by_role("button", name="Add labels…")).to_be_focused()

                side_scroll = await page.locator("#side").evaluate("side => side.scrollTop")
                await papers.evaluate("list => { list.scrollTop += 64; }")
                await expect(menu).to_be_hidden()
                await expect(trigger).to_be_focused()
                assert await papers.evaluate("list => list.scrollTop") > 0
                assert await page.locator("#side").evaluate("side => side.scrollTop") == side_scroll

                # The scroll that revealed a trigger may still have an event
                # queued when a pointer opens the menu in that same frame.
                await papers.evaluate("list => { list.scrollTop = 0; list.querySelector('.pmenu').click(); }")
                await page.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                await expect(menu.get_by_role("button", name="Add labels…")).to_be_focused()
                await page.keyboard.press("Escape")
                await expect(trigger).to_be_focused()

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
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels").click()
                await _answer(page, "OK", "Open Source, huggingface, reading-list")
                await page.locator('.paper-overview .label', has_text="open source").wait_for()

                # Reopening and saving must retain three distinct labels,
                # including both the multiword and existing hyphenated names.
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels").click()
                await page.locator('#ask').wait_for(state="visible")
                assert await page.locator('#ask-input').input_value() == 'huggingface, Open Source, reading-list'
                await _answer(page, "OK")
                await page.locator('.paper-overview .label', has_text="Open Source").wait_for()
                assert await page.locator('.paper-overview .label').all_text_contents() == [
                    'huggingface', 'Open Source', 'reading-list',
                ]

                # The sidebar arrives with the first label and filters on click.
                await page.locator('#labels [data-label="Open Source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for(state="detached")
                assert parse_qs(urlparse(page.url).fragment)["label"] == ["Open Source"]

                await page.reload()
                await page.wait_for_function("typeof S !== 'undefined' && S.workspace")
                await page.locator('#labels [data-label="Open Source"].active').wait_for()
                assert await page.locator('#papers [data-paper="closed2026model"]').count() == 0

                # Clicking it again is the way back out.
                await page.locator('#labels [data-label="Open Source"]').click()
                await page.locator('#papers [data-paper="closed2026model"]').wait_for()

    run_browser(scenario())


@pytest.mark.browser
def test_labels_spelled_in_different_cases_are_one_label_in_the_page():
    from playwright.async_api import expect
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("upper2026", "An Upper Paper", "steering")
    _browser_paper("lower2026", "A Lower Paper", "steering")
    _browser_paper("none2026", "An Unlabeled Paper", "steering")
    _browser_paper("third2026", "A Third Paper", "steering")
    store.set_labels("upper2026", ["RLHF"])
    store.set_labels("third2026", ["RLHF"])
    store.set_labels("lower2026", ["rlhf"])

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                labels = page.locator('#labels li')
                await expect(labels).to_have_count(1)
                await expect(labels).to_have_attribute("data-label", "RLHF")
                await expect(labels.locator('.n')).to_have_text("3")
                assert await page.evaluate("labelStyle('RLHF') === labelStyle('rlhf')")
                # The page reaches the server's key, so the two agree on which
                # spellings are one label.
                samples = [label for pair in UNICODE_CASE_PAIRS for label in pair] + ["İ", "i\u0307", "ǅ", "Ꭰ", "ꭰ", "I", "ı", "i", "ΑıΣ", "αıς"]
                assert await page.evaluate("values => values.map(labelKey)", samples) == [
                    store.label_key(value) for value in samples
                ]

                # A bookmark in the other case lands on the sidebar's spelling.
                await page.goto(f"{url}#label=rlhf")
                await page.reload()
                await page.locator('#labels [data-label="RLHF"].active').wait_for()
                await page.locator('#papers [data-paper="none2026"]').wait_for(state="detached")
                await page.locator('#papers [data-paper="lower2026"]').wait_for()

                # The map greys only the paper without it, whatever the case.
                await page.locator('#graph-nav [data-view="graph"]').click()
                await page.wait_for_function("window.doxographGraph().nodes.length === 4")
                dimmed = await page.evaluate(
                    "Object.fromEntries(window.doxographGraph().nodes.map(n => [n.key, n.dimmed]))")
                assert dimmed == {"upper2026": False, "third2026": False, "lower2026": False, "none2026": True}
                await page.goto(f"{url}#view=claims&label=RLHF")
                await page.reload()
                await page.locator('#labels [data-label="RLHF"].active').wait_for()

                # The paper's own chip turns the same filter off.
                await page.locator('#papers [data-paper="lower2026"] .label').click()
                await page.locator('#papers [data-paper="none2026"]').wait_for()

                # The editor offers the label once, in the paper's spelling,
                # and saving another spelling respells it on this paper only.
                await page.locator('#papers [data-paper="lower2026"]').click()
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels", exact=True).click()
                choices = page.get_by_role("group", name="Existing labels")
                await expect(choices.locator('button')).to_have_count(1)
                await expect(choices.locator('[aria-pressed="true"]')).to_have_text("rlhf")
                await page.locator('#ask-input').fill("RLHF")
                await expect(choices.locator('[aria-pressed="true"]')).to_have_count(1)
                await _answer(page, "OK")
                await page.locator('.paper-overview .label', has_text="RLHF").wait_for()
                assert store.load_paper("lower2026")["labels"] == ["RLHF"]

    run_browser(scenario())


@pytest.mark.browser
def test_the_page_keeps_apart_labels_the_server_keeps_apart():
    # A browser whose Unicode tables are newer or older than Python's can fold
    # two spellings together that the server holds apart. Folding everything
    # into one key stands in for that: the stored labels must still be told
    # apart, so an unchanged save keeps both and a filter picks out one.
    from playwright.async_api import expect
    from test_browser import _answer, _paper as _browser_paper, _server

    _browser_paper("both2026", "A Paper With Both", "steering")
    _browser_paper("beta2026", "A Beta Paper", "steering")
    store.set_labels("both2026", ["Alpha", "Beta"])
    store.set_labels("beta2026", ["Beta"])

    async def scenario():
        async with browser_session("chromium") as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#labels [data-label="Alpha"]').wait_for()
                await page.evaluate("foldLabel = () => 'one key'")
                assert await page.evaluate("labelKey('Gamma') === labelKey('Delta')")
                assert await page.evaluate("labelKey('Alpha') !== labelKey('Beta')")

                await page.locator('#labels [data-label="Alpha"]').click()
                await page.locator('#papers [data-paper="beta2026"]').wait_for(state="detached")
                await page.locator('#papers [data-paper="both2026"]').wait_for()

                await page.locator('#papers [data-paper="both2026"]').click()
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels", exact=True).click()
                await expect(page.locator('#ask-input')).to_have_value("Alpha, Beta")
                choices = page.get_by_role("group", name="Existing labels")
                await expect(choices.locator('[aria-pressed="true"]')).to_have_count(2)
                await _answer(page, "OK")
                await expect(page.locator('.paper-overview .label')).to_have_text(["Alpha", "Beta"])
                assert store.load_paper("both2026")["labels"] == ["Alpha", "Beta"]

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

                await page.locator(".paper-actions > summary").click()
                await page.get_by_role("button", name="Labels").click()
                await _answer(page, "OK", "")
                await page.locator('.paper-overview .label').wait_for(state="detached")

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
