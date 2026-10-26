"""End-to-end manual research with AI disabled and no external requests."""
import asyncio
import io
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect
from pypdf import PdfReader, PdfWriter

from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _server, _answer


async def ready(page):
    await page.wait_for_function("document.querySelector('#research-tools').getAttribute('aria-busy') !== 'true'")


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("uuid_available", [True, False])
def test_research_tools_workflow(engine, uuid_available):
    config.set_ai_enabled(False)
    for key, title in [('one','Steering and recovery'), ('two','Recovery under different conditions')]:
        store.save_paper(store.new_paper(key,title=title,authors=['Ada Researcher'],year=2026))
        store.add_claim(key,{'text':f'{title} is measured.', 'tags':['recovery'], 'quote':'Recovery depends on conditions.'})
    store.add_tag('recovery')
    store.add_tag('methods')

    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            if not uuid_available:
                await page.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined})")
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-select-claim]').first.wait_for()
                await page.locator('[data-select-claim]').nth(0).check()
                await page.locator('[data-select-claim]').nth(1).check()
                await page.get_by_role('button',name='Compare & organize',exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                assert await dialog.locator('.comparison-card').count() == 2
                await page.locator('#bulk-values').fill('methods')
                await dialog.get_by_role('button',name='Apply to selection',exact=True).click()
                await ready(page)
                assert all('methods' in c['tags'] for c in httpx.get(url+'/api/state').json()['claims'])
                await dialog.get_by_role('button',name='Evidence boards',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='New board',exact=True).click()
                await _answer(page,'Create board','Why recovery differs')
                await ready(page)
                await dialog.get_by_role('button',name='Add selected claims',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Add heading',exact=True).click()
                await ready(page)
                await dialog.locator('[data-entry-text]').last.fill('A comparison of conditions')
                await dialog.get_by_role('button',name='Save board',exact=True).click()
                await ready(page)
                await dialog.locator('.board-entry').last.get_by_role('button',name='Move entry 3 up').click()
                await ready(page)
                await dialog.get_by_role('button',name='Save board',exact=True).click()
                await ready(page)
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/evidence-board.png')
                data = httpx.get(url+'/api/notebook').json()
                assert data['boards'][0]['entries'][1]['text'] == 'A comparison of conditions'
                async with page.expect_download() as download:
                    await dialog.get_by_role('button',name='Export Markdown',exact=True).click()
                file = await download.value
                assert file.suggested_filename == 'why-recovery-differs.md'
                await ready(page)
                await dialog.get_by_role('button',name='Claim connections',exact=True).click()
                await ready(page)
                await page.locator('#connection-relation').select_option('qualifies')
                await page.locator('#connection-note').fill('The second experiment changes the condition.')
                await dialog.get_by_role('button',name='Create connection',exact=True).click()
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['connections'][0]['relation'] == 'qualifies'
                await dialog.get_by_role('button',name='Reading queue',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Add selected papers',exact=True).click()
                await ready(page)
                await dialog.locator('[data-reading-status]').first.select_option('finished')
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['status'] == 'finished'
                await dialog.get_by_role('button',name='Close research tools').click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await page.locator('#q').fill('Steering')
                await page.get_by_role('button',name='Save this search',exact=True).click()
                await _answer(page,'Save search','Steering evidence')
                await ready(page)
                await dialog.get_by_role('button',name='Close research tools').click()
                await page.reload()
                await page.locator('#q').fill('unrelated')
                await page.locator('.research-tools-nav').get_by_role('button',name='Saved searches').click()
                await ready(page)
                await dialog.get_by_role('button',name='Open search',exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                assert await page.locator('#q').input_value() == 'Steering'
                saved = httpx.get(url+'/api/notebook').json()
                assert len(saved['boards']) == 1
                identifiers = [saved['boards'][0]['id'], saved['connections'][0]['id'], saved['searches'][0]['id']]
                identifiers.extend(entry['id'] for entry in saved['boards'][0]['entries'])
                assert len(identifiers) == len(set(identifiers))
                if not uuid_available:
                    assert all(len(value) == 32 and int(value, 16) >= 0 for value in identifiers)
                assert not errors
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pdf_selection_capture_bookmarks_history_and_conflict(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    store.pdf_path('paper').write_bytes(minimal_pdf(['Recovery depends on conditions.\nWe measured several tasks.','Second page findings.']))

    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: external.append(request.url) if not request.url.startswith(('http://127.0.0.1:', 'blob:')) else None)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.get_by_role('button',name='Read & capture',exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                await page.locator('.pdf-page[data-page="1"] .textLayer span').first.wait_for()
                bounds = await page.locator('.pdf-page[data-page="1"] .textLayer span').first.bounding_box()
                await page.mouse.move(bounds['x'] + .5, bounds['y'] + bounds['height'] / 2)
                await page.mouse.down()
                await page.mouse.move(bounds['x'] + bounds['width'] - .5, bounds['y'] + bounds['height'] / 2, steps=10)
                await page.mouse.up()
                await dialog.get_by_role('button',name='Capture selected passage',exact=True).click()
                await ready(page)
                assert await page.locator('#capture-quote').input_value() == 'Recovery depends on conditions.'
                assert await page.locator('#capture-locator').input_value() == 'p. 1'
                await page.locator('#capture-text').fill('Recovery is conditional.')
                await page.locator('#capture-note').fill('Check the task differences.')
                await dialog.get_by_role('button',name='Save claim',exact=True).click()
                await ready(page)
                claim = httpx.get(url+'/api/state').json()['claims'][0]
                assert claim['quote_verified'] is True and claim['locator'] == 'p. 1'
                bounds = await page.locator('.pdf-page[data-page="1"] .textLayer span').first.bounding_box()
                await page.mouse.move(bounds['x'] + .5, bounds['y'] + bounds['height'] / 2)
                await page.mouse.down()
                await page.mouse.move(bounds['x'] + bounds['width'] - .5, bounds['y'] + bounds['height'] / 2, steps=10)
                await page.mouse.up()
                await page.locator('#highlight-color').select_option('green')
                await dialog.get_by_role('button',name='Highlight selected passage',exact=True).click()
                await ready(page)
                highlight = httpx.get(url+'/api/notebook').json()['highlights'][0]
                assert highlight['text'] == 'Recovery depends on conditions.' and highlight['page'] == 1 and highlight['color'] == 'green'
                assert len(highlight['rects']) == 1
                mark = await page.locator('.pdf-page[data-page="1"] .highlightLayer .highlight-green').bounding_box()
                assert abs(mark['x'] - bounds['x']) < 3 and abs(mark['width'] - bounds['width']) < 3
                await page.locator('[data-highlight-note]').fill('Which conditions?')
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Which conditions?'
                # Note drafts survive zoom and navigation, without changing the saved note.
                await page.locator('[data-highlight-note]').fill('An unsaved thought')
                await page.locator('#reader-zoom').select_option('1.6')
                await ready(page)
                assert await page.locator('[data-highlight-note]').input_value() == 'An unsaved thought'
                await dialog.get_by_role('button',name='Next page',exact=True).click()
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Which conditions?'
                await dialog.get_by_role('button',name='Previous page',exact=True).click()
                await ready(page)
                assert await page.locator('[data-highlight-note]').input_value() == 'An unsaved thought'
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Next page',exact=True).click()
                await ready(page)
                assert not await page.locator('.pdf-page[data-page="2"] .highlightLayer div').count()
                await dialog.get_by_role('button',name='Page 1',exact=True).click()
                await ready(page)
                assert await page.locator('.pdf-page[data-page="1"] .highlightLayer div').count() == 1
                await page.locator('#reader-zoom').select_option('2')
                await ready(page)
                bounds = await page.locator('.pdf-page[data-page="1"] .textLayer span').first.bounding_box()
                mark = await page.locator('.pdf-page[data-page="1"] .highlightLayer div').bounding_box()
                assert abs(mark['x'] - bounds['x']) < 4 and abs(mark['width'] - bounds['width']) < 4
                await dialog.get_by_role('button',name='Next page',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Bookmark this page',exact=True).click()
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['bookmarks'] == [2]
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 2
                Path('.context').mkdir(exist_ok=True)
                await page.screenshot(path='.context/research-reader.png')
                await dialog.get_by_role('button',name='Close research tools').click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await page.reload()
                await page.get_by_role('button',name='Read & capture',exact=True).click()
                await ready(page)
                assert await page.locator('#reader-page').input_value() == '2'
                await dialog.get_by_role('button',name='Close research tools').click()
                await dialog.wait_for(state='hidden', timeout=5000)
                httpx.patch(url+f"/api/papers/paper/claims/{claim['id']}",json={'text':'A stronger conclusion.'})
                await page.get_by_role('button',name='Edit history',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Restore this version').first.click()
                await _answer(page,'Restore version')
                await ready(page)
                assert httpx.get(url+'/api/state').json()['claims'][0]['text'] == 'Recovery is conditional.'
                await dialog.get_by_role('button',name='Close research tools').click()
                await dialog.wait_for(state='hidden', timeout=5000)
                httpx.delete(url+f"/api/papers/paper/claims/{claim['id']}")
                await page.get_by_role('button',name='Edit history',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Restore this version').first.click()
                await _answer(page,'Restore version')
                await ready(page)
                await dialog.get_by_role('button',name='Restore deletion',exact=True).first.click()
                await _answer(page,'Restore deletion')
                await ready(page)
                assert not httpx.get(url+'/api/state').json()['claims']
                await dialog.get_by_role('button',name='Restore this version').first.click()
                await _answer(page,'Restore version')
                await ready(page)
                assert httpx.get(url+'/api/state').json()['claims'][0]['id'] == claim['id']
                await dialog.get_by_role('button',name='Evidence boards',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='New board',exact=True).click()
                await _answer(page,'Create board','A board to keep')
                await ready(page)
                await page.locator('#board-title').fill('Unsaved title')
                # A second writer changes the file while the first window is editing.
                data = httpx.get(url+'/api/notebook').json()
                data['boards'][0]['title'] = 'Another window title'
                assert httpx.put(url+'/api/notebook',json=data).status_code == 200
                await dialog.get_by_role('button',name='Save board',exact=True).click()
                await ready(page)
                assert 'another window' in await page.locator('#research-message').inner_text()
                assert await page.locator('#board-title').input_value() == 'Unsaved title'
                await dialog.get_by_role('button',name='Reload saved version').click()
                await _answer(page,'Discard changes')
                await ready(page)
                assert await page.locator('#board-title').input_value() == 'Another window title'
                assert not errors and not external
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pdf_continuous_scrolling_preserves_capture_and_reading_position(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('scroll', title='Reading across pages'))
    # Different page sizes and a page with no text exercise layout and active-page hints.
    source = PdfReader(io.BytesIO(minimal_pdf([
        f'Findings on page {i}.\nEvidence continues across pages.' if i != 4 else ''
        for i in range(1, 9)
    ])))
    source.pages[1].mediabox.upper_right = (650, 1000)
    writer = PdfWriter()
    for pdf_page in source.pages:
        writer.add_page(pdf_page)
    with store.pdf_path('scroll').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="scroll"]').click()
                await page.get_by_role('button', name='Read & capture', exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                scroll = page.get_by_role('region', name='PDF pages')
                await expect(page.locator('.pdf-page')).to_have_count(8)
                assert await page.locator('.pdf-page canvas').count() < 8
                await expect(dialog.get_by_role('button', name='Previous page', exact=True)).to_be_disabled()

                # Hold one background save open while scrolling to another page.
                saving, finish_save = asyncio.Event(), asyncio.Event()

                async def delay_progress(route):
                    if route.request.method == 'PUT' and not saving.is_set():
                        saving.set()
                        await finish_save.wait()
                    await route.continue_()

                await page.route('**/api/notebook', delay_progress)
                # Real wheel input crosses the page boundary without navigation buttons.
                await scroll.hover()
                await page.mouse.wheel(0, 1100)
                await expect(page.locator('#reader-page')).to_have_value('2')
                await asyncio.wait_for(saving.wait(), timeout=5)
                await expect(page.locator('#capture-text')).to_be_enabled()
                passage = page.locator('.pdf-page[data-page="2"] .textLayer span').first
                await passage.wait_for()
                await passage.evaluate("""span => {
                    const range = document.createRange(); range.selectNodeContents(span);
                    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
                }""")
                await page.wait_for_function("window.getSelection().toString() === 'Findings on page 2.'")
                await page.mouse.wheel(0, 1300)
                await expect(page.locator('#reader-page')).to_have_value('3')
                finish_save.set()
                await page.wait_for_function(
                    "async () => (await (await fetch('/api/notebook')).json()).reading[0].page === 3"
                )
                await page.unroute('**/api/notebook', delay_progress)
                await dialog.get_by_role('button', name='Capture selected passage', exact=True).click()
                await ready(page)
                await expect(page.locator('#capture-quote')).to_have_value('Findings on page 2.')
                await expect(page.locator('#capture-locator')).to_have_value('p. 2')
                await page.locator('#capture-text').fill('Evidence spans several pages.')

                # Zoom and page navigation preserve a claim being written on another page.
                await page.locator('#reader-zoom').select_option('2')
                await ready(page)
                await expect(page.locator('#reader-page')).to_have_value('3')
                await dialog.get_by_role('button', name='Next page', exact=True).click()
                await ready(page)
                await expect(page.locator('#reader-page')).to_have_value('4')
                await expect(page.locator('#reader-hint')).to_contain_text('no selectable text')
                await expect(page.locator('#capture-text')).to_have_value('Evidence spans several pages.')
                await expect(page.locator('#capture-locator')).to_have_value('p. 2')
                await dialog.get_by_role('button', name='Save claim', exact=True).click()
                await ready(page)
                claim = httpx.get(url+'/api/state').json()['claims'][0]
                assert claim['locator'] == 'p. 2' and claim['quote'] == 'Findings on page 2.'

                # Jumping, scrolling backward, and returning to evicted pages all work.
                await page.locator('#reader-page').fill('8')
                await dialog.get_by_role('button', name='Go', exact=True).click()
                await ready(page)
                await expect(page.locator('.pdf-page[data-page="8"] .textLayer span').first).to_have_text('Findings on page 8.')
                await expect(dialog.get_by_role('button', name='Next page', exact=True)).to_be_disabled()
                await page.wait_for_function("document.querySelectorAll('.pdf-page canvas').length < 8")
                await scroll.hover()
                await page.mouse.wheel(0, -1600)
                await expect(page.locator('#reader-page')).to_have_value('7')
                await dialog.get_by_role('button', name='Bookmark this page', exact=True).click()
                await ready(page)
                progress = httpx.get(url+'/api/notebook').json()['reading'][0]
                assert progress['page'] == 7 and progress['bookmarks'] == [7]
                await scroll.evaluate('node => node.scrollTop = 0')
                await expect(page.locator('#reader-page')).to_have_value('1')
                await expect(page.locator('.pdf-page[data-page="1"] .textLayer span').first).to_have_text('Findings on page 1.')
                await dialog.get_by_role('button', name='Close research tools').click()
                await dialog.wait_for(state='hidden')
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 1
                await page.get_by_role('button', name='Read & capture', exact=True).click()
                await ready(page)
                await expect(page.locator('#reader-page')).to_have_value('1')
                if engine == 'chromium':
                    await scroll.hover()
                    await page.mouse.wheel(0, 800)
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/continuous-pdf-scrolling.png')
                await dialog.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                assert not errors
            await browser.close()
    asyncio.run(scenario())
