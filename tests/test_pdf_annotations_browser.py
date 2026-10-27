"""Saved claims and highlight notes remain discoverable from their PDF passages."""
from pathlib import Path

import httpx
import pytest
from fpdf import FPDF

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _poll, _server
from test_research_tools_browser import ready, select_first_line


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_existing_claim_annotations_follow_the_pdf_and_saved_changes(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Reading notes on recovery'))
    store.pdf_path('paper').write_bytes(minimal_pdf([
        'Recovery depends on conditions.\nWe measured several tasks.\nRepeated passage.\nRepeated passage.',
        'Evidence continues on the second page.',
    ]))
    captured = store.add_claim('paper', {'text':'Test note', 'quote':'Recovery depends on conditions.', 'locator':'p. 1'})
    multiline = store.add_claim('paper', {
        'text':'Compare the tasks', 'quote':'conditions. We measured several tasks.',
        'locator':'p. 1', 'note':'Which conditions matter?',
    })
    repeated = store.add_claim('paper', {'text':'Ambiguous', 'quote':'Repeated passage.', 'locator':'p. 1'})
    missing = store.add_claim('paper', {'text':'Missing', 'quote':'Not in this PDF.', 'locator':'p. 1'})
    second = store.add_claim('paper', {
        'text':'Second page note', 'quote':'Evidence continues on the second page.', 'locator':'p. 2',
    })

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                first_page = page.locator('.pdf-page[data-page="1"]')
                badge = first_page.get_by_role('button', name='Show claim: Test note', exact=True)
                await badge.wait_for()
                assert await badge.inner_text() == 'Claim'
                assert await first_page.locator(f'.claim-passage[data-claim="{multiline["id"]}"]').count() == 2
                for claim in [repeated, missing, second]:
                    assert await first_page.locator(f'.claim-passage[data-claim="{claim["id"]}"]').count() == 0
                # Both overlapping annotations are readable and independently clickable.
                other = first_page.get_by_role('button', name='Show claim: Compare the tasks — Which conditions matter?', exact=True)
                a, b = await badge.bounding_box(), await other.bounding_box()
                assert b['y'] >= a['y'] + a['height']
                assert await other.inner_text() == 'Note'
                await other.click()
                assert await page.locator(f'#content .claim.sel[data-claim="{multiline["id"]}"]').count() == 1

                # Existing captures survive a reload and stay aligned after zoom.
                await page.reload()
                await badge.wait_for()
                await page.locator('#reader-zoom').select_option('2')
                source = first_page.locator(f'.claim-passage[data-claim="{captured["id"]}"]')
                await source.wait_for()
                text = first_page.locator('.textLayer span').first
                mark_box, text_box = await source.bounding_box(), await text.bounding_box()
                assert abs(mark_box['x'] - text_box['x']) < 4
                assert abs(mark_box['width'] - text_box['width']) < 8
                await page.locator('#reader-zoom').select_option('fit')
                await badge.wait_for()

                # Annotation overlays do not intercept a fresh text selection.
                await select_first_line(page)
                await page.get_by_role('button', name='Highlight in yellow', exact=True).click()
                await ready(page)
                note = page.get_by_role('button', name='Show note: A note attached to this passage.', exact=True)
                assert await note.count() == 0
                await page.locator('#content').get_by_role('button', name='Add note', exact=True).click()
                await page.locator('[data-highlight-note]').fill('A note attached to this passage.')
                await page.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                await note.wait_for()
                await note.focus()
                await _poll(page)
                assert await note.evaluate('e => e === document.activeElement')
                await note.press('Enter')
                await ready(page)
                assert await page.locator('.highlight-entry.focused').evaluate('e => e === document.activeElement')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/pdf-note-indicators.png')

                # Removing the note removes its badge while preserving its highlight.
                await page.locator('#content').get_by_role('button', name='Edit note', exact=True).click()
                await page.locator('[data-highlight-note]').fill('')
                await page.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                assert await note.count() == 0
                assert await first_page.locator('.highlight-yellow').count() == 1
                # Quote edits and deletes invalidate the derived PDF marks on refresh.
                path = f'/api/papers/paper/claims/{captured["id"]}'
                assert httpx.patch(url + path, json={'quote':'Evidence continues on the second page.', 'locator':'p. 2'}).status_code == 200
                await _poll(page)
                assert await badge.count() == 0
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.locator('.pdf-page[data-page="2"]').get_by_role('button', name='Show claim: Test note', exact=True).wait_for()
                assert httpx.delete(url + path).status_code == 200
                await _poll(page)
                assert await page.get_by_role('button', name='Show claim: Test note', exact=True).count() == 0
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_claim_annotations_use_the_verifiers_accent_and_case_folding(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A study with accented text'))
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font('Helvetica', size=12)
    pdf.text(20, 30, 'Résumé of the café Straße study.')
    store.pdf_path('paper').write_bytes(bytes(pdf.output()))
    claim = store.add_claim('paper', {
        'text':'Accent-insensitive evidence', 'quote':'Resume of the CAFE STRASSE study.', 'locator':'p. 1',
    })
    assert claim['quote_verified'] is True

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.get_by_role('button', name='Show claim: Accent-insensitive evidence', exact=True).wait_for()
                assert await page.locator('.claim-passage').count() == 1

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_claim_annotations_check_document_ambiguity_and_span_page_breaks(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Evidence across pages'))
    store.pdf_path('paper').write_bytes(minimal_pdf([
        'Shared evidence.\nWe tested under normal conditions.\nThe team measured recovery under',
        'stress and across tasks.\nShared evidence.',
    ]))
    repeated = store.add_claim('paper', {'text':'Repeated evidence', 'quote':'Shared evidence.', 'locator':'p. 2'})
    assert repeated['quote_page'] == 1
    assert store.quote_context('paper', repeated['id'])['repeated'] is True
    crossing = store.add_claim('paper', {
        'text':'Evidence spans two pages', 'quote':'under stress and across tasks.', 'locator':'p. 1',
    })
    assert crossing['quote_verified'] is True

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                first = page.locator('.pdf-page[data-page="1"]')
                first_mark = first.locator(f'.claim-passage[data-claim="{crossing["id"]}"]')
                await first_mark.wait_for()
                # The first-page fragment is "under", which occurs twice on
                # that page. Its position comes from the unique global match.
                mark_box = await first_mark.bounding_box()
                text_box = await first.locator('.textLayer span').last.bounding_box()
                assert abs(mark_box['y'] - text_box['y']) < 3
                assert mark_box['x'] > text_box['x']
                await page.get_by_role('button', name='Next page', exact=True).click()
                second = page.locator('.pdf-page[data-page="2"]')
                second_mark = second.locator(f'.claim-passage[data-claim="{crossing["id"]}"]')
                await second_mark.wait_for()
                await second.get_by_role('button', name='Show claim: Evidence spans two pages', exact=True).click()
                assert await page.locator(f'#content .claim.sel[data-claim="{crossing["id"]}"]').count() == 1
                assert await page.locator(f'.claim-passage[data-claim="{repeated["id"]}"]').count() == 0
                assert await page.get_by_role('button', name='Show claim: Repeated evidence', exact=True).count() == 0
                await page.locator('#reader-zoom').select_option('2')
                await second_mark.wait_for()
                mark_box = await second_mark.bounding_box()
                text_box = await second.locator('.textLayer span').first.bounding_box()
                assert abs(mark_box['x'] - text_box['x']) < 3
                assert abs(mark_box['width'] - text_box['width']) < 4
                assert not errors

    run_browser(scenario())
