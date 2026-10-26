"""Review real claims beside local PDFs, including drafts and failed saves."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _answer, _server


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_focused_review_pdf_edit_and_filtered_progress(engine):
    config.set_ai_enabled(False)
    store.add_tag('recovery')
    store.add_tag('methods')
    store.save_paper(store.new_paper('paper', title='Recovery under different conditions',
                                     authors=['Ada Researcher'], year=2026))
    store.pdf_path('paper').write_bytes(minimal_pdf([
        'Introduction to the study.',
        'We measured several tasks.\nRecovery depends on conditions.\nTask choice matters.',
    ]))
    first = store.add_claim('paper', {
        'text': 'Recovery is conditional.', 'quote': 'Recovery depends on conditions.',
        'locator': 'p. 1', 'tags': ['recovery', 'methods'], 'reviewed': False,
        'evidence': 'Multiple tasks were measured.',
    })
    second = store.add_claim('paper', {
        'text': 'Task choice matters.', 'quote': 'Task choice matters.', 'locator': 'p. 2',
        'tags': ['recovery'], 'reviewed': False,
    })
    store.save_paper(store.new_paper('excluded', title='Outside this review'))
    excluded = store.add_claim('excluded', {'text': 'Leave this alone.', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: external.append(request.url)
                    if not request.url.startswith(('http://127.0.0.1:', 'blob:')) else None)
            with _server() as url:
                await page.goto(url)
                await page.locator('#tags [data-tag="recovery"]').click()
                await page.locator('#only-unreviewed').check()
                await page.locator('#btn-focus-review').click()
                dialog = page.locator('#focused-review')
                await expect(page.locator('#focus-progress')).to_have_text('Claim 1 of 2 · 0 reviewed')
                await expect(page.locator('#focus-passage')).to_contain_text('Quote found on page 2')
                await expect(page.locator('#focus-passage')).to_contain_text('locator says p. 1')
                await expect(page.locator('#focus-page')).to_have_value('2')
                await expect(page.locator('#focus-pdf canvas')).to_be_visible()
                await dialog.get_by_role('button', name='Previous page', exact=True).click()
                await expect(page.locator('#focus-page')).to_have_value('1')
                await page.locator('#focus-page').fill('2')
                await page.locator('#focus-page').press('Enter')
                await expect(page.locator('#focus-pdf canvas')).to_have_attribute('aria-label',
                    'Source paper, page 2. The matched passage is available above.')
                await page.locator('#focus-zoom').select_option('1.5')
                await page.wait_for_function("document.querySelector('#focus-pdf canvas').style.width === '918px'")
                await page.locator('#focus-zoom').select_option('fit')
                if engine == 'chromium':
                    await page.wait_for_function("document.querySelector('#focus-pdf canvas').style.width !== '918px'")
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/focused-review.png')

                await dialog.get_by_role('button', name='Mark reviewed & next', exact=True).click()
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 1 reviewed')
                await dialog.get_by_role('button', name='Previous claim', exact=True).click()
                await expect(dialog.locator('.focus-claim h3')).to_have_text(first['text'])
                await dialog.get_by_role('button', name='Mark unreviewed', exact=True).click()
                await expect(page.locator('#focus-progress')).to_have_text('Claim 1 of 2 · 0 reviewed')
                await dialog.get_by_role('button', name='Edit claim', exact=True).click()
                await dialog.get_by_label('Locator', exact=True).fill('p. 2')
                await dialog.get_by_label('Claim', exact=True).fill('Recovery depends on the task.')
                await dialog.locator('textarea[name="note"]').fill('Check the task differences.')
                # Review shortcuts must remain ordinary text while writing.
                await dialog.locator('textarea[name="note"]').press('r')
                assert next(c for c in httpx.get(url + '/api/state').json()['claims']
                            if c['id'] == first['id'])['reviewed'] is False
                await dialog.get_by_role('button', name='Save', exact=True).click()
                await expect(dialog.locator('.focus-claim h3')).to_have_text('Recovery depends on the task.')
                saved = httpx.get(url + '/api/papers/paper').json()['claims'][0]
                assert saved['locator'] == 'p. 2' and saved['quote_verified'] is True
                assert saved['note'].endswith('r')
                await dialog.get_by_role('button', name='Edit claim', exact=True).focus()
                await page.keyboard.press('r')
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 1 reviewed')
                await page.keyboard.press('r')
                await expect(page.locator('#focus-complete')).to_be_visible()
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 2 reviewed')
                await page.keyboard.press('Escape')
                await expect(dialog).not_to_be_visible()
                assert await page.locator('#only-unreviewed').is_checked()
                data = httpx.get(url + '/api/state').json()['claims']
                assert all(c['reviewed'] for c in data if c['id'] in {first['id'], second['id']})
                assert next(c for c in data if c['id'] == excluded['id'])['reviewed'] is False
                assert not errors and not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("editing", [False, True])
def test_focused_review_updates_when_the_pdf_arrives_without_a_claim_change(engine, editing):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A paper whose download is pending'))
    store.add_claim('paper', {'text': 'A manually recorded claim.',
        'quote': 'The source eventually arrives.', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#content .claim').wait_for()
                await page.locator('#btn-focus-review').click()
                dialog = page.locator('#focused-review')
                await expect(page.locator('#focus-passage')).to_contain_text('No PDF is available')
                if editing:
                    await dialog.get_by_role('button', name='Edit claim', exact=True).click()
                    await dialog.get_by_label('Claim', exact=True).fill('Keep the unsaved claim wording.')
                    await dialog.get_by_label('Claim', exact=True).evaluate('el => el.setSelectionRange(8, 8)')
                before = httpx.get(url + '/api/state').json()['claims']
                store.pdf_path('paper').write_bytes(minimal_pdf('The source eventually arrives.'))
                # Let the ordinary background poll notice the PDF, with no claim edit.
                await expect(page.locator('#focus-pdf canvas')).to_be_visible(timeout=15000)
                await expect(page.locator('#focus-passage')).to_contain_text('Quote found on page 1')
                assert httpx.get(url + '/api/state').json()['claims'] == before
                if editing:
                    await expect(dialog.get_by_label('Claim', exact=True)).to_have_value('Keep the unsaved claim wording.')
                    await expect(dialog.get_by_label('Claim', exact=True)).to_be_focused()
                    assert await dialog.get_by_label('Claim', exact=True).evaluate('el => el.selectionStart') == 8
                store.pdf_path('paper').unlink()
                await expect(page.locator('#focus-passage')).to_contain_text('No PDF is available', timeout=15000)
                await expect(page.locator('#focus-pdf')).not_to_be_visible()
                if editing:
                    await expect(dialog.get_by_label('Claim', exact=True)).to_have_value('Keep the unsaved claim wording.')
                    await expect(dialog.get_by_label('Claim', exact=True)).to_be_focused()
                    await dialog.get_by_role('button', name='Cancel', exact=True).click()
                await dialog.get_by_role('button', name='Exit review', exact=True).click()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_focused_review_ignores_late_passages_and_failed_review_stays_put(engine):
    config.set_ai_enabled(False)
    # Review follows newest-added order; do not depend on both seeds being
    # created within the same clock tick to keep the PDF claim first.
    store.save_paper(store.new_paper('a', title='A source with a PDF', added='2026-01-02T00:00:00Z'))
    store.pdf_path('a').write_bytes(minimal_pdf('A passage from the first paper.'))
    first = store.add_claim('a', {'text': 'The first claim.',
        'quote': 'A passage from the first paper.', 'reviewed': False})
    store.save_paper(store.new_paper('b', title='Another source without a PDF', added='2026-01-01T00:00:00Z'))
    second = store.add_claim('b', {'text': 'The second claim.', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                started, release = asyncio.Event(), asyncio.Event()

                async def delay(route):
                    response = await route.fetch()
                    started.set()
                    await release.wait()
                    await route.fulfill(response=response)

                await page.route(f'**/api/papers/a/claims/{first["id"]}/quote-context', delay)
                await page.goto(url)
                await page.locator(f'#content .claim[data-claim="{first["id"]}"]').click()
                await page.locator('#btn-focus-review').click()
                await asyncio.wait_for(started.wait(), timeout=10)
                dialog = page.locator('#focused-review')
                await dialog.get_by_role('button', name='Next claim', exact=True).click()
                release.set()
                await expect(dialog.locator('.focus-claim h3')).to_have_text(second['text'])
                await expect(page.locator('#focus-passage')).to_contain_text('No PDF is available')

                async def reject(route):
                    await route.fulfill(status=503, json={'detail': 'Try the review again'})

                endpoint = f'**/api/papers/b/claims/{second["id"]}'
                await page.route(endpoint, reject)
                await dialog.get_by_role('button', name='Mark reviewed', exact=True).click()
                await expect(page.locator('#focus-error')).to_contain_text('Try the review again')
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 0 reviewed')
                await expect(dialog.get_by_role('button', name='Mark reviewed', exact=True)).to_be_enabled()
                await page.unroute(endpoint, reject)

                # A write that succeeds must still count if reading state back fails.
                await page.route('**/api/state', lambda route: route.fulfill(status=503, json={'detail': 'Read unavailable'}))
                await dialog.get_by_role('button', name='Mark reviewed', exact=True).click()
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 1 reviewed')
                assert httpx.get(url + '/api/papers/b').json()['claims'][0]['reviewed'] is True
                await page.unroute('**/api/state')
                await dialog.get_by_role('button', name='Previous claim', exact=True).click()
                await expect(page.locator('#focus-pdf canvas')).to_be_visible()
                await expect(page.locator('#focus-passage')).to_contain_text(first['quote'])
                await dialog.get_by_role('button', name='Exit review', exact=True).click()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_focused_review_drafts_failed_save_missing_pdf_and_workspace(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Default workspace paper'))
    default_claim = store.add_claim('paper', {'text': 'Keep the default unchanged.', 'reviewed': False})
    other = config.create_workspace('Separate research')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('paper', title='A paper without a PDF'))
        first = store.add_claim('paper', {'text': 'A claim to edit.', 'reviewed': False})
        second = store.add_claim('paper', {'text': 'A second claim.', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1100, 'height': 850})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#workspace').select_option(other['id'])
                await expect(page.locator('#content')).to_contain_text('A claim to edit.')
                await page.locator('#btn-focus-review').click()
                dialog = page.locator('#focused-review')
                await expect(page.locator('#focus-passage')).to_contain_text('No PDF is available')
                await expect(page.locator('#focus-pdf')).not_to_be_visible()
                await dialog.get_by_role('button', name='Edit claim', exact=True).click()
                await dialog.get_by_label('Claim', exact=True).fill('Keep this unsaved draft.')
                await dialog.get_by_role('button', name='Next claim', exact=True).click()
                await expect(dialog.locator('.focus-claim h3')).to_have_text(second['text'])
                await dialog.get_by_role('button', name='Previous claim', exact=True).click()
                await expect(dialog.get_by_label('Claim', exact=True)).to_have_value('Keep this unsaved draft.')
                await page.keyboard.press('Escape')
                await expect(dialog).not_to_be_visible()
                await page.locator('#btn-focus-review').click()
                await expect(dialog.get_by_label('Claim', exact=True)).to_have_value('Keep this unsaved draft.')
                endpoint = f'**/api/papers/paper/claims/{first["id"]}'

                async def reject(route):
                    if route.request.method == 'PATCH':
                        await route.fulfill(status=503, json={'detail': 'Temporary storage failure'})
                    else:
                        await route.continue_()

                await page.route(endpoint, reject)
                await dialog.get_by_role('button', name='Save', exact=True).click()
                await expect(page.locator('#focus-error')).to_contain_text('Temporary storage failure')
                await expect(dialog.get_by_label('Claim', exact=True)).to_have_value('Keep this unsaved draft.')
                await expect(dialog.get_by_role('button', name='Save', exact=True)).to_be_enabled()
                await page.unroute(endpoint, reject)
                await dialog.get_by_role('button', name='Save', exact=True).click()
                await expect(dialog.locator('.focus-claim h3')).to_have_text('Keep this unsaved draft.')
                await dialog.get_by_role('button', name='Mark reviewed & next', exact=True).click()
                await expect(page.locator('#focus-progress')).to_have_text('Claim 2 of 2 · 1 reviewed')
                # A removed claim must not leave a review control targeting its old ID.
                httpx.delete(url + f'/api/papers/paper/claims/{second["id"]}',
                             headers={'X-Doxograph-Workspace': other['id']})
                await page.evaluate('refresh()')
                await expect(page.locator('#focus-progress')).to_have_text('Claim 1 of 1 · 1 reviewed')
                await dialog.get_by_role('button', name='Exit review', exact=True).click()
                original = httpx.get(url + '/api/papers/paper').json()['claims'][0]
                assert original['id'] == default_claim['id'] and original['reviewed'] is False
                assert original['text'] == 'Keep the default unchanged.'
                await page.set_viewport_size({'width': 650, 'height': 850})
                await page.locator('#btn-focus-review').click()
                assert await dialog.evaluate('el => el.scrollWidth <= el.clientWidth')
                await dialog.get_by_role('button', name='Edit claim', exact=True).click()
                assert await dialog.locator('.focus-details').evaluate('el => el.scrollWidth <= el.clientWidth')
                await dialog.get_by_role('button', name='Cancel', exact=True).click()
                await dialog.get_by_role('button', name='Exit review', exact=True).click()
                await page.locator('#q').fill('no matching claims')
                await page.locator('#btn-focus-review').click()
                await expect(dialog).not_to_be_visible()
                await expect(page.locator('#toasts')).to_contain_text('No claims match your filters')
                await page.locator('#q').fill('')
                await page.locator('#btn-focus-review').click()
                await dialog.get_by_role('button', name='Edit claim', exact=True).click()
                await dialog.get_by_label('Claim', exact=True).fill('A draft belonging to the other workspace.')
                await page.go_back()
                await _answer(page, 'Discard and switch')
                await expect(page.locator('#workspace')).to_have_value('default')
                await expect(dialog).not_to_be_visible()
                await page.locator('#btn-focus-review').click()
                await expect(dialog.locator('.focus-claim h3')).to_have_text('Keep the default unchanged.')
                await dialog.get_by_role('button', name='Exit review', exact=True).click()
                assert not errors

    run_browser(scenario())
