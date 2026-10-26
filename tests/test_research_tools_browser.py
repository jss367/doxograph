"""End-to-end manual research with AI disabled and no external requests."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright

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


async def select_first_line(page, number=1):
    """Drag across the first line of text on page `number` of the open paper."""
    span = page.locator(f'#reader .pdf-page[data-page="{number}"] .textLayer span').first
    await span.wait_for()
    bounds = await span.bounding_box()
    await page.mouse.move(bounds['x'] + .5, bounds['y'] + bounds['height'] / 2)
    await page.mouse.down()
    await page.mouse.move(bounds['x'] + bounds['width'] - .5, bounds['y'] + bounds['height'] / 2, steps=10)
    await page.mouse.up()
    await page.locator('#passage-tools').wait_for(state='visible')
    return bounds


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_paper_reads_beside_its_notes(engine):
    """Selecting a paper with a PDF shows the paper itself beside a rail of the
    reader's own note, highlights, and the claims; passages become highlights
    or claims from the toolbar under the selection."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    store.add_claim('paper', {'text':'Findings continue.', 'tags':[], 'quote':'Second page findings.', 'locator':'p. 2'})
    store.pdf_path('paper').write_bytes(minimal_pdf(['Recovery depends on conditions.\nWe measured several tasks.','Second page findings.','Third page.']))
    store.save_paper(store.new_paper('plain',title='A paper without a PDF',authors=['Bo Writer'],year=2025))

    async def scenario():
        async with async_playwright() as p:
            browser = await getattr(p, engine).launch()
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: external.append(request.url) if not request.url.startswith(('http://127.0.0.1:', 'blob:')) else None)
            with _server() as url:
                await page.goto(url)
                # The library view has no reader; a paper without a PDF has none either.
                await page.locator('#content .claim').first.wait_for()
                assert await page.locator('#reader').is_hidden()
                await page.locator('#papers li[data-paper="plain"]').click()
                await page.locator('.paperhead h2', has_text='A paper without a PDF').wait_for()
                assert await page.locator('#reader').is_hidden()
                assert await page.locator('#panes.reading').count() == 0
                # A paper with a PDF opens beside its rail: note first, then highlights, then claims.
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#panes.reading').wait_for()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                assert await page.locator('#reader .pdf-page').count() == 3
                assert await page.locator('#reader-total').inner_text() == 'of 3'
                rail = page.locator('#content')
                assert await rail.locator('.note .who').first.text_content() == 'my note'
                assert await rail.get_by_role('button', name='Write a note').count() == 1
                assert await page.locator('.paperhead').get_by_role('button', name='Write a note').count() == 0
                assert await page.locator('.paperhead').get_by_role('button', name='Read & capture').count() == 0
                assert await rail.locator('.highlights').count() == 1
                assert await rail.locator('.claim').count() == 1
                # The note is written in the rail and stays there.
                await rail.get_by_role('button', name='Write a note').click()
                await rail.locator('textarea[data-note="paper"]').fill('Read this for the task differences.')
                await rail.get_by_role('button', name='Save', exact=True).click()
                await rail.locator('.note p', has_text='task differences').wait_for()
                assert httpx.get(url+'/api/state').json()['papers'][0]['notes'] == 'Read this for the task differences.'
                # A selected passage becomes a new claim, quote and page filled in.
                bounds = await select_first_line(page)
                await page.get_by_role('button', name='Capture as claim', exact=True).click()
                form = page.locator('form[data-form]')
                await form.wait_for()
                assert await form.locator('[name="quote"]').input_value() == 'Recovery depends on conditions.'
                assert await form.locator('[name="locator"]').input_value() == 'p. 1'
                await form.locator('[name="text"]').fill('Recovery is conditional.')
                await form.locator('[name="note"]').fill('Check the task differences.')
                await form.get_by_role('button', name='Save', exact=True).click()
                await page.locator('#content .claim', has_text='Recovery is conditional.').wait_for()
                claim = next(c for c in httpx.get(url+'/api/state').json()['claims'] if c['text'] == 'Recovery is conditional.')
                assert claim['quote_verified'] is True and claim['locator'] == 'p. 1' and claim['note'] == 'Check the task differences.'
                # A selected passage becomes a highlight in the chosen color, drawn over the text.
                bounds = await select_first_line(page)
                await page.get_by_role('button', name='Highlight in green', exact=True).click()
                await ready(page)
                await rail.locator('.highlight-entry').wait_for()
                highlight = httpx.get(url+'/api/notebook').json()['highlights'][0]
                assert highlight['text'] == 'Recovery depends on conditions.' and highlight['page'] == 1 and highlight['color'] == 'green'
                assert len(highlight['rects']) == 1
                mark = page.locator('#reader .pdf-page[data-page="1"] .highlightLayer .highlight-green')
                await mark.wait_for()
                box = await mark.bounding_box()
                assert abs(box['x'] - bounds['x']) < 3 and abs(box['width'] - bounds['width']) < 3
                assert await page.locator('#passage-tools').is_hidden()
                # The highlight takes a note of its own, in the rail.
                await rail.get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('Which conditions?')
                await rail.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                await rail.locator('.highlight-entry .note p', has_text='Which conditions?').wait_for()
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Which conditions?'
                # It stays in place at another zoom.
                await page.locator('#reader-zoom').select_option('2')
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await ready(page)
                bounds = await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.bounding_box()
                box = await page.locator('#reader .pdf-page[data-page="1"] .highlightLayer div').bounding_box()
                assert abs(box['x'] - bounds['x']) < 4 and abs(box['width'] - bounds['width']) < 4
                await page.locator('#reader-zoom').select_option('fit')
                await ready(page)
                # A claim's PDF link scrolls the paper open beside it rather than opening a tab.
                await rail.locator('.claim', has_text='Findings continue.').locator('.pdflink').click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                assert len(page.context.pages) == 1
                # Bookmarking a page puts the paper on the reading queue at that page.
                await page.get_by_role('button', name='Bookmark this page', exact=True).click()
                await ready(page)
                await page.get_by_role('button', name='Remove bookmark', exact=True).wait_for()
                reading = httpx.get(url+'/api/notebook').json()['reading'][0]
                assert reading['bookmarks'] == [2] and reading['page'] == 2 and reading['status'] == 'reading'
                await page.get_by_role('button', name='Next', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await page.get_by_role('button', name='Previous', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                Path('.context').mkdir(exist_ok=True)
                await page.screenshot(path='.context/research-reader.png')
                # The rail's highlight scrolls the paper back to its page.
                await rail.locator('.highlight-entry blockquote').click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '1'")
                # The PDF can be put away, and the choice is remembered in this browser.
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await page.locator('#reader').wait_for(state='hidden')
                assert await page.locator('#panes.reading').count() == 0
                assert await rail.locator('.highlight-entry').count() == 1
                await page.reload()
                await page.locator('.paperhead h2', has_text='A study of recovery').wait_for()
                assert await page.locator('#reader').is_hidden()
                await page.get_by_role('button', name='Show PDF', exact=True).click()
                await page.locator('#panes.reading').wait_for()
                # Reopened, the paper resumes where the reading queue left it.
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '2'")
                # The reading queue's own button opens a page of the paper.
                await page.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                await dialog.get_by_role('button', name='Bookmark: page 2', exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                # Removing the highlight asks, because it carries a note.
                await rail.get_by_role('button', name='Remove', exact=True).click()
                await _answer(page, 'Remove highlight')
                await ready(page)
                await rail.locator('.highlight-entry').wait_for(state='hidden')
                assert not httpx.get(url+'/api/notebook').json()['highlights']
                assert not errors and not external
            await browser.close()
    asyncio.run(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_history_restore_and_notebook_conflict(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    claim = store.add_claim('paper', {'text':'Recovery is conditional.', 'tags':[], 'quote':'Recovery depends on conditions.', 'locator':'p. 1', 'note':'Check the task differences.'})

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
                await page.locator('.paperhead h2', has_text='A study of recovery').wait_for()
                dialog = page.locator('#research-tools')
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
