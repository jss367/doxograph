"""Exercise the benchmark catalog in Chromium and native WebKit."""
import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _answer, _server


async def ready(page):
    await page.wait_for_function("document.querySelector('#benchmarks-sheet').getAttribute('aria-busy') !== 'true'")


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_benchmarks_are_made_from_the_names_claims_give_and_link_those_claims(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Monitor study'))
    measured = store.add_claim('study', {'text': 'Monitors reach 0.9 AUROC on SHADE.'})
    attacked = store.add_claim('study', {'text': 'Attacks succeed more often on HarmBench.'})
    store.add_claim('study', {'text': 'A method description.'})
    store.add_facet('benchmark', 'Benchmarks a result is measured on')
    store.update_claim('study', measured['id'], {'facets': {'benchmark': ['CUA-SHADE-Arena', 'HarmBench']}})
    store.update_claim('study', attacked['id'], {'facets': {'benchmark': ['HarmBench']}})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                # A dataset with the same name is a different record, in its own catalog.
                httpx.post(url + '/api/datasets', json={'name': 'HarmBench'}).raise_for_status()
                await page.goto(url)
                await page.locator('#btn-benchmarks').click()
                view = page.locator('#benchmarks-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                assert await page.evaluate("V.view === 'benchmarks'")
                add = view.get_by_role('button', name='Add 2 benchmarks named in claims')
                await expect(add).to_be_visible()
                await add.click()
                await expect(page.locator('#ask-note')).to_have_text('HarmBench, CUA-SHADE-Arena')
                await _answer(page, 'Add 2 benchmarks')
                await ready(page)
                await expect(page.locator('#benchmarks-status')).to_have_text('Added 2 benchmarks.')
                await expect(add).to_be_hidden()
                await expect(page.locator('#benchmarks-list strong')).to_have_text(['CUA-SHADE-Arena', 'HarmBench'])
                assert sorted(b['name'] for b in httpx.get(url + '/api/benchmarks').json()['benchmarks']) == [
                    'CUA-SHADE-Arena', 'HarmBench']
                assert [d['name'] for d in httpx.get(url + '/api/datasets').json()['datasets']] == ['HarmBench']

                await view.locator('#benchmarks-list').get_by_role('button', name='HarmBench').click()
                await ready(page)
                await expect(page.locator('#benchmark-name')).to_have_value('HarmBench')
                research = page.locator('#benchmark-research')
                await expect(research.locator('.catalog-claim-link')).to_have_text([measured['text'], attacked['text']])
                # Benchmarks play no role, and a local source opens no Loupe viewer.
                assert await view.locator('[data-role]').count() == 0
                await page.locator('#benchmark-source').fill('/data/harmbench')
                await expect(view.get_by_role('button', name='Open in Loupe')).to_have_count(0)
                await page.wait_for_function("document.querySelector('#benchmarks-status').textContent === 'Saved'")

                # The paper's panel lists the benchmarks its claims are measured on.
                await page.goto(url + '/#view=claims&paper=study')
                artifacts = page.locator(".paper-artifacts [data-act='open-benchmark']")
                await expect(artifacts).to_have_text(['CUA-SHADE-Arena', 'HarmBench'])
                await artifacts.filter(has_text='HarmBench').click()
                await page.wait_for_function("V.view === 'benchmarks' && V.benchmark !== null")
                await expect(page.locator('#benchmark-name')).to_have_value('HarmBench')
                assert 'benchmark=' in page.url
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_adding_from_claims_shows_records_another_window_made_first(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='Monitor study'))
    claim = store.add_claim('study', {'text': 'Monitors reach 0.9 AUROC.'})
    store.add_facet('benchmark', 'Benchmarks a result is measured on')
    store.update_claim('study', claim['id'], {'facets': {'benchmark': ['CUA-SHADE-Arena', 'HarmBench']}})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1400, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url + '/#view=benchmarks')
                await ready(page)
                add = page.locator('#benchmark-from-facets')
                await expect(add).to_have_text('Add 2 benchmarks named in claims')
                # Another window makes one of them while this one still offers both.
                httpx.post(url + '/api/benchmarks/from-facets', json={'names': ['HarmBench']}).raise_for_status()
                await add.click()
                await _answer(page, 'Add 2 benchmarks')
                await ready(page)
                await expect(page.locator('#benchmarks-status')).to_have_text('Added 1 benchmark.')
                await expect(page.locator('#benchmarks-list strong')).to_have_text(['CUA-SHADE-Arena', 'HarmBench'])
                await expect(add).to_be_hidden()

                # When the other window made all of them, this one says so and shows them.
                store.update_claim('study', claim['id'], {'facets': {'benchmark': ['CUA-SHADE-Arena', 'HarmBench', 'MMLU']}})
                await page.evaluate('refreshAll()')
                await expect(add).to_have_text('Add a benchmark named in claims')
                httpx.post(url + '/api/benchmarks/from-facets', json={'names': ['MMLU']}).raise_for_status()
                await add.click()
                await _answer(page, 'Add a benchmark')
                await ready(page)
                await expect(page.locator('#benchmarks-status')).to_have_text('Every benchmark your claims name already has a record.')
                await expect(page.locator('#benchmarks-list strong')).to_have_text(['CUA-SHADE-Arena', 'HarmBench', 'MMLU'])
                await expect(add).to_be_hidden()
                assert not errors

    run_browser(scenario())
