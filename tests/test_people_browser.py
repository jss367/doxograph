"""Open an author from a paper, keep notes on them, and gather their other spellings."""
import os

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _server


async def ready(page):
    await page.wait_for_function("document.querySelector('#people-sheet').getAttribute('aria-busy') !== 'true'")


async def settled(page):
    await page.wait_for_function("document.querySelector('#people-status').textContent === 'Saved'")


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_author_page_becomes_a_record_that_gathers_other_spellings(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('engine', title='Notes on the engine', authors=['Ada Lovelace', 'Charles Babbage'], year=1843))
    store.save_paper(store.new_paper('letters', title='Letters on numbers', authors=['A. Lovelace'], year=1842))
    store.add_claim('engine', {'text': 'The engine can compose music'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1200, 'height': 900})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                card = page.locator('[data-library-paper="engine"]')
                await card.get_by_role('button', name='Ada Lovelace', exact=True).click()
                view = page.locator('#people-sheet')
                await view.wait_for(state='visible')
                await ready(page)
                assert 'author=Ada+Lovelace' in page.url
                document = page.locator('#person-document')
                await document.get_by_role('heading', name='Ada Lovelace', exact=True).wait_for()
                assert await document.get_by_role('button', name='Notes on the engine', exact=True).count() == 1
                assert await document.get_by_role('button', name='Letters on numbers', exact=True).count() == 0
                assert await document.get_by_role('button', name='Charles Babbage', exact=True).count() == 1
                await document.get_by_text('1 claim', exact=True).click()
                assert await document.get_by_role('button', name='The engine can compose music', exact=True).is_visible()

                # Adding notes keeps the person, and their page follows the record.
                await document.get_by_role('button', name='Add notes on this person', exact=True).click()
                await ready(page)
                await page.wait_for_function("V.person && !V.author")
                await settled(page)
                notes = view.get_by_label('Notes', exact=True)
                assert await notes.evaluate('field => field === document.activeElement')
                await notes.fill('Met at the 1843 workshop.')
                await view.get_by_label('Other names in author lists', exact=True).fill('A. Lovelace')
                await view.get_by_label('Affiliation', exact=True).fill('Analytical Society')
                await document.get_by_role('button', name='Letters on numbers', exact=True).wait_for()
                await settled(page)
                [saved] = httpx.get(url + '/api/people').json()['people']
                assert saved['name'] == 'Ada Lovelace' and saved['other_names'] == ['A. Lovelace']
                assert saved['notes'] == 'Met at the 1843 workshop.' and saved['affiliation'] == 'Analytical Society'
                assert f'person={saved["id"]}' in page.url
                listed = page.locator('#people-list')
                assert await listed.get_by_role('button', name='Ada Lovelace Analytical Society · 2 papers', exact=True).count() == 1
                # Her other spelling is hers now, not an author of its own.
                assert await listed.get_by_role('button', name='A. Lovelace 1 paper', exact=True).count() == 0
                if os.environ.get('DOXOGRAPH_SCREENSHOT'):
                    await page.screenshot(path=os.environ['DOXOGRAPH_SCREENSHOT'])

                # A coauthor opens as an author page; the other spelling opens the record.
                await document.get_by_role('button', name='Charles Babbage', exact=True).click()
                await document.get_by_role('heading', name='Charles Babbage', exact=True).wait_for()
                assert 'author=Charles+Babbage' in page.url
                await page.goto(url + '/#view=claims&paper=letters')
                await page.locator('#paperhead').get_by_role('button', name='A. Lovelace', exact=True).click()
                await ready(page)
                await page.wait_for_function("id => V.person === id", arg=saved['id'])
                assert await view.get_by_label('Notes', exact=True).input_value() == 'Met at the 1843 workshop.'

                # Following a paper leaves the view; Back returns to the record.
                await document.get_by_role('button', name='Notes on the engine', exact=True).click()
                await page.wait_for_function("V.view === 'claims' && V.paper === 'engine'")
                await page.go_back()
                await ready(page)
                await page.wait_for_function("id => V.view === 'people' && V.person === id", arg=saved['id'])
                assert await view.get_by_label('Name', exact=True).input_value() == 'Ada Lovelace'

                # A name another record holds is refused before it is sent.
                await page.get_by_role('button', name='Add person', exact=True).click()
                await ready(page)
                await view.get_by_label('Name', exact=True).fill('a lovelace')
                await page.wait_for_function("document.querySelector('#people-status').textContent.includes('already names Ada Lovelace')")
                assert len(httpx.get(url + '/api/people').json()['people']) == 1
                assert not errors

    run_browser(scenario())
