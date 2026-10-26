"""End-to-end manual research with AI disabled and no external requests."""
import asyncio
import time
from pathlib import Path

import httpx
import pytest
from browser_support import browser_session, run_browser

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
        async with browser_session(engine) as browser:
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

    run_browser(scenario())


async def saved_page(url, number, timeout=6.0):
    """Wait for the reading queue to record `number` as the page the paper is on."""
    deadline = time.monotonic() + timeout
    reading = []
    while time.monotonic() < deadline:
        reading = httpx.get(url+'/api/notebook').json()['reading']
        if reading and reading[0]['page'] == number: return
        await asyncio.sleep(.1)
    raise AssertionError(f'page {number} was not saved; queue reads {reading}')


async def queue_status(url, status, timeout=6.0):
    """Wait for the reading queue's first entry to reach `status`: the dialog
    closes before the queue is written."""
    deadline = time.monotonic() + timeout
    reading = []
    while time.monotonic() < deadline:
        reading = httpx.get(url+'/api/notebook').json()['reading']
        if reading and reading[0]['status'] == status: return
        await asyncio.sleep(.1)
    raise AssertionError(f'the queue did not reach {status}; it reads {reading}')


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
    store.save_paper(store.new_paper('second',title='A second paper',authors=['Cy Author'],year=2024))
    store.pdf_path('second').write_bytes(minimal_pdf(['Another first page.','Another second page.']))
    other = config.create_workspace('Elsewhere')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('elsewhere',title='A paper elsewhere'))

    async def scenario():
        async with browser_session(engine) as browser:
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
                assert next(p for p in httpx.get(url+'/api/state').json()['papers'] if p['key'] == 'paper')['notes'] == 'Read this for the task differences.'
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
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await saved_page(url, 3)
                # Back to the page the paper opened on: the reader compares with
                # what it saved, not with a notebook the poll has not refreshed.
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '1'")
                await saved_page(url, 1)
                # A save that fails is tried once more on its own.
                failures = []
                async def fail_once(route):
                    if route.request.method == 'PUT' and not failures:
                        failures.append(route.request.url); await route.fulfill(status=503, body='down')
                    else: await route.continue_()
                await page.route('**/api/notebook', fail_once)
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await saved_page(url, 2)
                assert failures
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await saved_page(url, 3)
                await page.unroute('**/api/notebook')
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await saved_page(url, 2)
                # A save held long enough for a move away and back: the page the
                # reader is on is saved after it, not left at the page it had left.
                held, release, pages = [], asyncio.Event(), []
                async def hold_first(route):
                    if route.request.method == 'PUT':
                        pages.append(next(r['page'] for r in route.request.post_data_json['reading'] if r['paper'] == 'paper'))
                        if not held: held.append(route); await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_first)
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                deadline = time.monotonic() + 6
                while not held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert held and pages == [3]
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await page.wait_for_timeout(300)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 2
                release.set()
                await saved_page(url, 2)
                await page.wait_for_timeout(300)
                assert pages == [3, 2] and httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 2
                await page.unroute('**/api/notebook')
                # A highlight note being written survives a redraw and is a draft
                # until saved; Cancel keeps the note on file.
                await rail.get_by_role('button', name='Edit note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('An unsaved thought')
                await page.locator('#reader-zoom').select_option('1.6')
                await ready(page)
                assert await rail.locator('[data-highlight-note]').input_value() == 'An unsaved thought'
                assert await page.evaluate("ResearchTools.holds()")
                # Capturing a claim from it takes the note as it is being written.
                await rail.get_by_role('button', name='Capture as claim', exact=True).click()
                form = page.locator('form[data-form]')
                await form.wait_for()
                assert await form.locator('[name="note"]').input_value() == 'An unsaved thought'
                assert await form.locator('[name="quote"]').input_value() == 'Recovery depends on conditions.'
                await form.get_by_role('button', name='Cancel', exact=True).click()
                await form.wait_for(state='hidden')
                assert await rail.locator('[data-highlight-note]').input_value() == 'An unsaved thought'
                await rail.get_by_role('button', name='Cancel', exact=True).click()
                assert await rail.locator('[data-highlight-note]').count() == 0
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Which conditions?'
                await page.locator('#reader-zoom').select_option('fit')
                await ready(page)
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
                # Put away within the debounce, the page just reached is still saved.
                await saved_page(url, 1)
                await page.reload()
                await page.locator('.paperhead h2', has_text='A study of recovery').wait_for()
                assert await page.locator('#reader').is_hidden()
                await page.get_by_role('button', name='Show PDF', exact=True).click()
                await page.locator('#panes.reading').wait_for()
                # Reopened, the paper resumes where the reading queue left it.
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '1'")
                # The reading queue's own button opens a page of the paper, and a
                # paper only queued is being read once it is resumed.
                await page.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                await dialog.locator('[data-reading-status]').first.select_option('queued')
                await ready(page)
                await dialog.get_by_role('button', name='Bookmark: page 2', exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await ready(page)   # the dialog closes before the queue is written
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await queue_status(url, 'reading')
                await saved_page(url, 2)
                await page.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                await dialog.locator('[data-reading-status]').first.select_option('finished')
                await ready(page)
                await dialog.get_by_role('button', name='Resume at page 2', exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await ready(page)
                await page.wait_for_timeout(300)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['status'] == 'finished'
                # Switching workspaces with a highlight note being written asks
                # first; declining keeps the editor and the workspace.
                await rail.get_by_role('button', name='Edit note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('Not yet saved')
                await page.locator('#workspace').select_option(other['id'])
                await _answer(page, 'Cancel')
                assert await page.locator('#workspace').input_value() == 'default'
                assert await rail.locator('[data-highlight-note]').input_value() == 'Not yet saved'
                await page.locator('#workspace').select_option(other['id'])
                await _answer(page, 'Discard note')
                await page.locator('#papers li[data-paper="elsewhere"]').wait_for()
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Which conditions?'
                await page.locator('#workspace').select_option('default')
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#panes.reading').wait_for()
                assert await rail.locator('[data-highlight-note]').count() == 0
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '2'")
                # Leaving the workspace before a page is saved saves it on the
                # way out, to the workspace the paper is in, not the new one.
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await page.locator('#workspace').select_option(other['id'])
                await page.locator('#papers li[data-paper="elsewhere"]').wait_for()
                await saved_page(url, 3)
                assert not httpx.get(url+'/api/notebook', headers={'X-Doxograph-Workspace': other['id']}).json()['reading']
                await page.locator('#workspace').select_option('default')
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#panes.reading').wait_for()
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '3'")
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await saved_page(url, 2)
                # A page asked for one paper belongs to it: resuming this paper
                # and then opening another before its PDF arrives leaves the
                # other on its own first page.
                await page.locator('#papers li[data-paper="second"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                pdf_held, pdf_release = [], asyncio.Event()
                async def hold_pdf(route):
                    pdf_held.append(route); await pdf_release.wait(); await route.continue_()
                await page.route('**/pdf/paper?*', hold_pdf)
                await page.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                await dialog.get_by_role('button', name='Resume at page 2', exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await page.locator('.paperhead h2', has_text='A study of recovery').wait_for()
                deadline = time.monotonic() + 6
                while not pdf_held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert pdf_held
                await page.locator('#papers li[data-paper="second"]').click()
                await page.locator('.paperhead h2', has_text='A second paper').wait_for()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                pdf_release.set()
                await page.wait_for_timeout(500)
                assert await page.locator('#reader-page').input_value() == '1'
                assert await page.locator('#reader-total').inner_text() == 'of 2'
                await page.unroute('**/pdf/paper?*')
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#panes.reading').wait_for()
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '2'")
                # Removing the highlight asks, because it carries a note.
                await rail.get_by_role('button', name='Remove', exact=True).click()
                await _answer(page, 'Remove highlight')
                await ready(page)
                await rail.locator('.highlight-entry').wait_for(state='hidden')
                assert not httpx.get(url+'/api/notebook').json()['highlights']
                assert not errors and not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reader_keeps_pages_notes_and_progress_under_pressure(engine):
    """What happens at the edges: a page drawn after it was scrolled out of
    reach is released; a half-written highlight note is not dropped for
    another without asking, nor by a slow or failed save; and a page reached
    just before the reader is put away is still saved."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('long',title='A long paper',authors=['Ada Researcher'],year=2026))
    store.pdf_path('long').write_bytes(minimal_pdf([f'Page {n} of the long paper.' for n in range(1, 25)]))
    store.save_paper(store.new_paper('other',title='The other paper',authors=['Bo Writer'],year=2025))
    store.pdf_path('other').write_bytes(minimal_pdf(['The other paper.']))
    rect = {'x':.1,'y':.1,'w':.3,'h':.02}

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: external.append(request.url) if not request.url.startswith(('http://127.0.0.1:', 'blob:')) else None)
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'long','status':'reading','page':1,'bookmarks':[]}]
                data['highlights'] = [
                    {'id':'first','paper':'long','page':1,'rects':[rect],'text':'Page 1 of the long paper.','color':'yellow','note':'Note A'},
                    {'id':'second','paper':'long','page':2,'rects':[rect],'text':'Page 2 of the long paper.','color':'green','note':''}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="long"]').click()
                # Scrolled to the end as soon as the pages are laid out, while the
                # first ones are still being drawn: once the end is drawn, the
                # start has been released rather than kept because its drawing
                # finished after it had left reach.
                await page.wait_for_function("document.querySelectorAll('#reader .pdf-page').length === 24")
                await page.evaluate("document.querySelector('#pdf-scroll').scrollTo({top: document.querySelector('#pdf-scroll').scrollHeight})")
                await page.locator('#reader .pdf-page[data-page="24"] .textLayer span').first.wait_for()
                await page.wait_for_function("document.querySelector('#reader-page').value === '24'")
                await page.wait_for_function("""(() => {
                    const first = document.querySelector('#reader .pdf-page[data-page="1"]');
                    return first.classList.contains('pending') && !first.querySelector('canvas');
                })()""")
                drawn = await page.evaluate("[...document.querySelectorAll('#reader .pdf-page:not(.pending)')].map(p => Number(p.dataset.page))")
                assert drawn and all(n >= 18 for n in drawn), drawn
                await saved_page(url, 24)
                rail = page.locator('#content')
                # Editing another highlight's note while one is being written asks
                # first; Cancel keeps the first editor and its text.
                await rail.locator('[data-highlight="first"]').get_by_role('button', name='Edit note', exact=True).click()
                await rail.locator('[data-highlight-note="first"]').fill('Note A, revised')
                await rail.locator('[data-highlight="second"]').get_by_role('button', name='Add note', exact=True).click()
                await _answer(page, 'Cancel')
                assert await rail.locator('[data-highlight-note="first"]').input_value() == 'Note A, revised'
                assert await rail.locator('[data-highlight-note="second"]').count() == 0
                await rail.locator('[data-highlight="second"]').get_by_role('button', name='Add note', exact=True).click()
                await _answer(page, 'Discard note')
                await rail.locator('[data-highlight-note="second"]').wait_for()
                assert await rail.locator('[data-highlight-note="first"]').count() == 0
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['note'] == 'Note A'
                # Typing while a slow save runs is kept: the save lands what was
                # there when Save was clicked, and the editor stays open with the rest.
                held, release = [], asyncio.Event()
                async def hold_first(route):
                    if route.request.method == 'PUT' and not held: held.append(route); await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_first)
                await rail.locator('[data-highlight-note="second"]').fill('Note B')
                await rail.get_by_role('button', name='Save note', exact=True).click()
                deadline = time.monotonic() + 6
                while not held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert held
                assert await rail.get_by_role('button', name='Save note', exact=True).is_disabled()
                await rail.locator('[data-highlight-note="second"]').press('End')
                await rail.locator('[data-highlight-note="second"]').type(', continued')
                release.set()
                await ready(page)
                await page.locator('#toasts .toast', has_text='still in the editor').wait_for()
                assert await rail.locator('[data-highlight-note="second"]').input_value() == 'Note B, continued'
                assert httpx.get(url+'/api/notebook').json()['highlights'][1]['note'] == 'Note B'
                await page.unroute('**/api/notebook')
                # A save that fails keeps the editor and its text.
                failures = []
                async def fail_once(route):
                    if route.request.method == 'PUT' and not failures:
                        failures.append(route.request.url); await route.fulfill(status=503, content_type='application/json', body='{"detail": "The notebook is down."}')
                    else: await route.continue_()
                await page.route('**/api/notebook', fail_once)
                await rail.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                await page.locator('#toasts .toast', has_text='The notebook is down.').wait_for()
                assert failures
                assert await rail.locator('[data-highlight-note="second"]').input_value() == 'Note B, continued'
                assert httpx.get(url+'/api/notebook').json()['highlights'][1]['note'] == 'Note B'
                await rail.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                await rail.locator('[data-highlight-note="second"]').wait_for(state='detached')
                assert httpx.get(url+'/api/notebook').json()['highlights'][1]['note'] == 'Note B, continued'
                await page.unroute('**/api/notebook')
                # A page reached just before the PDF is put away, or another
                # paper opened, is saved on the way out.
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '23'")
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await page.locator('#reader').wait_for(state='hidden')
                await saved_page(url, 23)
                await page.get_by_role('button', name='Show PDF', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page') && document.querySelector('#reader-page').value === '23'")
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '22'")
                await page.locator('#papers li[data-paper="other"]').click()
                await page.locator('.paperhead h2', has_text='The other paper').wait_for()
                await saved_page(url, 22)
                assert not errors and not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_history_restore_and_notebook_conflict(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    claim = store.add_claim('paper', {'text':'Recovery is conditional.', 'tags':[], 'quote':'Recovery depends on conditions.', 'locator':'p. 1', 'note':'Check the task differences.'})

    async def scenario():
        async with browser_session(engine) as browser:
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

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_figure_marks_questions_and_annotated_pdf(engine):
    """A box dragged on the page is a figure mark with a color of its own; a
    mark's note can be an open question; the Marks list opens the paper on the
    mark and answers the question; the annotated export leaves the PDF alone."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    original = minimal_pdf(['Recovery depends on conditions.','Second page findings.'])
    store.pdf_path('paper').write_bytes(original)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000}, accept_downloads=True)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await page.get_by_role('button', name='Next page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                second = page.locator('#reader .pdf-page[data-page="2"]')
                await second.locator('.textLayer span').first.wait_for()
                # Drag a box over the page; the text under it is not selected.
                await page.get_by_role('button', name='Mark a figure', exact=True).click()
                await page.get_by_role('button', name='Cancel figure mark', exact=True).wait_for()
                box, pane = await second.bounding_box(), await page.locator('#pdf-scroll').bounding_box()
                top = max(box['y'], pane['y']) + 20
                start, end = (box['x'] + box['width'] * .2, top), (box['x'] + box['width'] * .6, top + 150)
                await page.mouse.move(*start)
                await page.mouse.down()
                await page.mouse.move(*end, steps=8)
                await page.mouse.up()
                await page.locator('#passage-tools.for-figure').wait_for(state='visible')
                assert not await page.get_by_role('button', name='Capture as claim', exact=True).is_visible()
                await page.get_by_role('button', name='Highlight in blue', exact=True).click()
                await ready(page)
                await second.locator('.highlightLayer .figure').wait_for()
                [figure] = httpx.get(url+'/api/notebook').json()['highlights']
                assert figure['kind'] == 'figure' and figure['page'] == 2 and figure['color'] == 'blue' and figure['text'] == ''
                [rect] = figure['rects']
                assert abs(rect['x'] - .2) < .01 and abs(rect['w'] - .4) < .01
                assert abs(rect['y'] - (top - box['y']) / box['height']) < .01 and abs(rect['h'] - 150 / box['height']) < .01
                assert await page.get_by_role('button', name='Mark a figure', exact=True).is_visible()
                rail = page.locator('#content')
                entry = rail.locator('.highlight-entry')
                assert await entry.count() == 1
                assert 'Figure or region' in await entry.inner_text()
                assert await entry.get_by_role('button', name='Capture as claim').count() == 0
                # A note that is an open question.
                await entry.get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('Why does recovery dip here?')
                await entry.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                await entry.locator('.note p', has_text='Why does recovery dip here?').wait_for()
                await entry.get_by_label('Open question').check()
                await ready(page)
                await page.wait_for_function("document.querySelector('.highlight-entry.question') !== null")
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['question'] is True
                Path('.context').mkdir(exist_ok=True)
                await page.screenshot(path='.context/figure-mark.png')
                # The export is a copy; the stored PDF is unchanged.
                async with page.expect_download() as download:
                    await rail.get_by_role('button', name='Export annotated PDF', exact=True).click()
                assert (await download.value).suggested_filename == 'paper-annotated.pdf'
                assert store.pdf_path('paper').read_bytes() == original
                # Marks lists open questions and returns to the page the mark is on.
                await page.get_by_role('button', name='Previous page', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '1'")
                await page.get_by_role('button', name='Marks and questions', exact=True).click()
                await ready(page)
                dialog = page.locator('#research-tools')
                await page.locator('#marks-filter').select_option('questions')
                assert 'Why does recovery dip here?' in await dialog.locator('.marks-paper').inner_text()
                await dialog.get_by_role('button', name='Open page 2', exact=True).click()
                await dialog.wait_for(state='hidden', timeout=5000)
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await second.locator('.highlightLayer .figure.focused').wait_for()
                assert await rail.locator('.highlight-entry.focused').count() == 1
                await page.get_by_role('button', name='Marks and questions', exact=True).click()
                await ready(page)
                await page.locator('#marks-filter').select_option('questions')
                await dialog.get_by_role('button', name='Mark answered', exact=True).click()
                await ready(page)
                assert httpx.get(url+'/api/notebook').json()['highlights'][0]['question'] is False
                assert await dialog.locator('.empty').count() == 1
                await page.locator('#marks-filter').select_option('all')
                assert await dialog.locator('.marks-paper .highlight-entry').count() == 1
                await dialog.get_by_role('button', name='Close research tools').click()
                await dialog.wait_for(state='hidden', timeout=5000)
                # Escape leaves figure mode without a mark.
                await page.get_by_role('button', name='Mark a figure', exact=True).click()
                await page.keyboard.press('Escape')
                await page.get_by_role('button', name='Mark a figure', exact=True).wait_for()
                assert len(httpx.get(url+'/api/notebook').json()['highlights']) == 1
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_short_last_page_canceled_figure_removal_draft_and_failed_final_save(engine):
    """Going to a page the scroller cannot reach the top of still makes it the
    current page; canceling a figure drops the box drawn; removing a mark with
    a note being written asks first; the page saved on the way out survives a
    failed first attempt."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study with a short last page',authors=['Ada Researcher'],year=2026))
    store.save_paper(store.new_paper('other',title='The other paper',authors=['Bo Writer'],year=2025))
    store.pdf_path('other').write_bytes(minimal_pdf(['The other paper.']))
    from pypdf import PdfReader, PdfWriter
    import io
    source = PdfReader(io.BytesIO(minimal_pdf(['First page.', 'Second page.', 'Short last page.'])))
    source.pages[2].mediabox.upper_right = (612, 120)
    writer = PdfWriter()
    for pdf_page in source.pages:
        writer.add_page(pdf_page)
    with store.pdf_path('paper').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'paper','status':'reading','page':1,'bookmarks':[]}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                # The last page is too short for its top to reach the scroller's
                # top: Go there anyway, and it is the page bookmarked and saved.
                await page.locator('#reader-page').fill('3')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await page.get_by_role('button', name='Bookmark this page', exact=True).click()
                await ready(page)
                await page.get_by_role('button', name='Remove bookmark', exact=True).wait_for()
                reading = httpx.get(url+'/api/notebook').json()['reading'][0]
                assert reading['bookmarks'] == [3] and reading['page'] == 3
                # Scrolling away lets the reading line decide again.
                await page.evaluate("document.querySelector('#pdf-scroll').scrollTo({top: 0})")
                await page.wait_for_function("document.querySelector('#reader-page').value === '1'")
                # A box drawn and then canceled is no mark, and selecting text works again.
                await page.get_by_role('button', name='Mark a figure', exact=True).click()
                first = page.locator('#reader .pdf-page[data-page="1"]')
                box = await first.bounding_box()
                await page.mouse.move(box['x'] + box['width'] * .3, box['y'] + 40)
                await page.mouse.down()
                await page.mouse.move(box['x'] + box['width'] * .6, box['y'] + 140, steps=6)
                await page.mouse.up()
                await page.locator('#passage-tools.for-figure').wait_for(state='visible')
                await page.get_by_role('button', name='Cancel figure mark', exact=True).click()
                await page.locator('#passage-tools').wait_for(state='hidden')
                assert await page.locator('#passage-tools.for-figure').count() == 0
                await select_first_line(page)
                assert await page.get_by_role('button', name='Capture as claim', exact=True).is_visible()
                await page.get_by_role('button', name='Highlight in yellow', exact=True).click()
                await ready(page)
                marks = httpx.get(url+'/api/notebook').json()['highlights']
                assert len(marks) == 1 and marks[0]['kind'] == 'passage'
                # Removing a mark whose note is only being written asks first.
                rail = page.locator('#content')
                entry = rail.locator('.highlight-entry')
                await entry.get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('Not saved yet')
                await entry.get_by_role('button', name='Remove', exact=True).click()
                await page.locator('#ask').wait_for(state='visible')
                await page.locator('#ask').get_by_role('button', name='Cancel', exact=True).click()
                await page.locator('#ask').wait_for(state='hidden')
                assert len(httpx.get(url+'/api/notebook').json()['highlights']) == 1
                assert await rail.locator('[data-highlight-note]').input_value() == 'Not saved yet'
                await entry.get_by_role('button', name='Cancel', exact=True).click()
                # The page saved on the way out: its first attempt fails, the
                # retry lands, and the queue records the page the reader left.
                failures = []
                async def fail_once(route):
                    if route.request.method == 'PUT' and not failures:
                        failures.append(route.request.url); await route.fulfill(status=503, body='down'); return
                    await route.continue_()
                await page.route('**/api/notebook', fail_once)
                await page.locator('#reader-page').fill('2')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                await page.locator('#papers li[data-paper="other"]').click()
                await page.locator('.paperhead h2', has_text='The other paper').wait_for()
                await saved_page(url, 2)
                assert failures
                await page.unroute('**/api/notebook')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_landscape_pages_fit_hidden_pdf_mark_focus_and_superseded_final_save(engine):
    """Fit width fits each page on its own, so a landscape page in a portrait
    paper does not overflow and marks on it still line up; a mark clicked with
    the PDF put away is the mark the PDF comes back outlined on; a save held
    from a reader torn down cannot overwrite the page a reader opened on the
    same paper since has moved to."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A paper with a wide page',authors=['Ada Researcher'],year=2026))
    store.save_paper(store.new_paper('other',title='The other paper',authors=['Bo Writer'],year=2025))
    store.pdf_path('other').write_bytes(minimal_pdf(['The other paper.']))
    from pypdf import PdfReader, PdfWriter
    import io
    source = PdfReader(io.BytesIO(minimal_pdf(['First page.', 'A wide landscape page.', 'Third page.'])))
    source.pages[1].mediabox.upper_right = (1000, 800)   # wide, and tall enough to keep its line of text on the page
    writer = PdfWriter()
    for pdf_page in source.pages:
        writer.add_page(pdf_page)
    with store.pdf_path('paper').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'paper','status':'reading','page':1,'bookmarks':[]}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await page.locator('#reader .pdf-page[data-page="2"] .textLayer span').first.wait_for()
                # Every page fits the column, the wide one at a smaller scale.
                widths = await page.evaluate("""[...document.querySelectorAll('#reader .pdf-page')].map(p => p.getBoundingClientRect().width)""")
                column = await page.evaluate("document.querySelector('#pdf-scroll').clientWidth")
                assert all(w <= column for w in widths) and abs(widths[0] - widths[1]) < 2
                heights = await page.evaluate("""[...document.querySelectorAll('#reader .pdf-page')].map(p => p.getBoundingClientRect().height)""")
                assert heights[1] < heights[0] * .7
                # A highlight on the wide page lines up with its text.
                await page.locator('#reader-page').fill('2')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                bounds = await select_first_line(page, 2)
                await page.get_by_role('button', name='Highlight in green', exact=True).click()
                await ready(page)
                mark = page.locator('#reader .pdf-page[data-page="2"] .highlightLayer .highlight-green')
                await mark.wait_for()
                box = await mark.bounding_box()
                assert abs(box['x'] - bounds['x']) < 3 and abs(box['width'] - bounds['width']) < 3
                # A second mark, on the first page; both are listed in the rail.
                await page.locator('#reader-page').fill('1')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '1'")
                bounds = await select_first_line(page, 1)
                await page.get_by_role('button', name='Highlight in yellow', exact=True).click()
                await ready(page)
                rail = page.locator('#content')
                await page.wait_for_function("document.querySelectorAll('#content .highlight-entry').length === 2")
                marks = httpx.get(url+'/api/notebook').json()['highlights']
                second = next(h for h in marks if h['page'] == 2)
                # With the PDF put away, clicking a mark brings it back outlined on that mark.
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await page.locator('#reader').wait_for(state='hidden')
                await rail.locator(f'.highlight-entry[data-highlight="{second["id"]}"] blockquote').click()
                await page.locator('#panes.reading').wait_for()
                await page.locator(f'#reader .pdf-page[data-page="2"] .highlightLayer [data-highlight="{second["id"]}"].focused').wait_for()
                assert await page.locator('#reader .highlightLayer .focused').count() == 1
                assert await rail.locator('.highlight-entry.focused').count() == 1
                assert await rail.locator(f'.highlight-entry.focused[data-highlight="{second["id"]}"]').count() == 1
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                # The save of the page left on is held; the paper is reopened and
                # read on to page 3 meanwhile; when the held save lands, page 3 stands.
                held, release, pages = [], asyncio.Event(), []
                async def hold_first(route):
                    if route.request.method == 'PUT':
                        pages.append(next(r['page'] for r in route.request.post_data_json['reading'] if r['paper'] == 'paper'))
                        if not held: held.append(route); await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_first)
                await page.locator('#papers li[data-paper="other"]').click()
                await page.locator('.paperhead h2', has_text='The other paper').wait_for()
                deadline = time.monotonic() + 6
                while not held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert held and pages == [2]
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await page.locator('#reader-page').fill('3')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await page.wait_for_timeout(2000)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 1
                release.set()
                await saved_page(url, 3)
                await page.wait_for_timeout(300)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 3
                await page.unroute('**/api/notebook')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mark_while_loading_switch_during_request_and_draft_on_removed_paper(engine):
    """A mark clicked while the paper is still loading is the mark it opens
    outlined on; a workspace switch waits for a reader request in flight;
    removing a paper warns about a note being written on one of its marks and
    drops the draft with it."""
    config.set_ai_enabled(False)
    other = config.create_workspace('Elsewhere')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('elsewhere', title='Elsewhere paper'))
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    store.pdf_path('paper').write_bytes(minimal_pdf(['Recovery depends on conditions.','Second page findings.']))
    store.save_paper(store.new_paper('second',title='A second paper',authors=['Bo Writer'],year=2025))
    store.pdf_path('second').write_bytes(minimal_pdf(['The second paper.']))
    rect = {'x':.1,'y':.08,'w':.3,'h':.02}

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'paper','status':'reading','page':1,'bookmarks':[]}]
                data['highlights'] = [
                    {'id':'one','paper':'paper','page':1,'rects':[rect],'text':'Recovery depends on conditions.','color':'yellow','note':''},
                    {'id':'two','paper':'paper','page':2,'rects':[rect],'text':'Second page findings.','color':'green','note':''},
                    {'id':'three','paper':'second','page':1,'rects':[rect],'text':'The second paper.','color':'blue','note':''}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                rail = page.locator('#content')
                # The PDF is held while a rail mark is clicked: once it arrives,
                # the reader opens on that mark's page with the mark outlined.
                pdf_held, pdf_release = [], asyncio.Event()
                async def hold_pdf(route):
                    pdf_held.append(route); await pdf_release.wait(); await route.continue_()
                await page.route('**/pdf/paper?*', hold_pdf)
                await page.locator('#papers li[data-paper="paper"]').click()
                await rail.locator('.highlight-entry[data-highlight="two"]').wait_for()
                deadline = time.monotonic() + 6
                while not pdf_held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert pdf_held
                await rail.locator('.highlight-entry[data-highlight="two"] blockquote').click()
                pdf_release.set()
                await page.locator('#reader .pdf-page[data-page="2"] .highlightLayer [data-highlight="two"].focused').wait_for()
                await page.wait_for_function("document.querySelector('#reader-page').value === '2'")
                assert await rail.locator('.highlight-entry.focused[data-highlight="two"]').count() == 1
                await page.unroute('**/pdf/paper?*')
                # A workspace switch while a reader request waits on the notebook
                # is refused, and the picker goes back to the live workspace.
                held, release = [], asyncio.Event()
                async def hold_get(route):
                    if route.request.method == 'GET' and not held: held.append(route); await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_get)
                await page.get_by_role('button', name='Bookmark this page', exact=True).click()
                deadline = time.monotonic() + 6
                while not held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert held
                await page.locator('#workspace').select_option(other['id'])
                await page.locator('#toasts .toast', has_text='Finish the current operation').wait_for()
                assert await page.locator('#workspace').input_value() == 'default'
                release.set()
                await page.get_by_role('button', name='Remove bookmark', exact=True).wait_for()
                await page.unroute('**/api/notebook')
                assert await page.locator('#papers li[data-paper="paper"]').count() == 1
                # Removing the paper while a note is being written on one of its
                # marks says so; the draft goes with the paper, so a note begun
                # on another paper's mark afterwards opens without a question.
                await rail.locator('.highlight-entry[data-highlight="one"]').get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('A thought about recovery')
                # A removal that fails leaves the paper, and the note with it.
                failed = []
                async def fail_delete(route):
                    if route.request.method == 'DELETE' and not failed:
                        failed.append(route.request.url); await route.fulfill(status=503, body='down'); return
                    await route.continue_()
                await page.route('**/api/papers/paper', fail_delete)
                ask = page.locator('#ask')
                await page.get_by_role('button', name='Remove', exact=True).first.click()
                await ask.wait_for(state='visible')
                assert 'note you are writing' in await ask.inner_text()
                await ask.get_by_role('button', name='Remove', exact=True).click()
                await page.locator('#toasts .toast', has_text='Could not').wait_for()
                assert failed and await page.locator('#papers li[data-paper="paper"]').count() == 1
                assert await rail.locator('[data-highlight-note]').input_value() == 'A thought about recovery'
                await page.unroute('**/api/papers/paper')
                await page.get_by_role('button', name='Remove', exact=True).first.click()
                await ask.wait_for(state='visible')
                await ask.get_by_role('button', name='Remove', exact=True).click()
                await page.locator('#papers li[data-paper="paper"]').wait_for(state='detached')
                await page.locator('#papers li[data-paper="second"]').click()
                await rail.locator('.highlight-entry[data-highlight="three"]').wait_for()
                await rail.locator('.highlight-entry[data-highlight="three"]').get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').wait_for()
                assert await ask.count() == 0 or not await ask.is_visible()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_page_above_measured_later_keeps_the_view_in_place(engine):
    """Pages are laid out at the first page's size until each is measured; a
    short page above the one being read, measured after the reader opened on
    that page, must not move the page under the eye."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A paper with a short second page',authors=['Ada Researcher'],year=2026))
    from pypdf import PdfReader, PdfWriter
    import io
    source = PdfReader(io.BytesIO(minimal_pdf(['First page.', 'Short.', 'Third page.', 'Fourth page.'])))
    source.pages[1].mediabox.upper_right = (612, 160)
    writer = PdfWriter()
    for pdf_page in source.pages:
        writer.add_page(pdf_page)
    with store.pdf_path('paper').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'paper','status':'reading','page':3,'bookmarks':[]}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                # Opened on page 3 while page 2 was still the first page's size.
                await page.locator('#reader .pdf-page[data-page="3"] .textLayer span').first.wait_for()
                await page.locator('#reader .pdf-page[data-page="2"]:not(.pending)').wait_for()
                await page.wait_for_timeout(300)
                placed = await page.evaluate("""(() => {
                    const scroller = document.querySelector('#pdf-scroll');
                    const third = document.querySelector('#reader .pdf-page[data-page="3"]');
                    const second = document.querySelector('#reader .pdf-page[data-page="2"]');
                    return {scrollTop: scroller.scrollTop, top: third.offsetTop, secondHeight: second.offsetHeight, firstHeight: document.querySelector('#reader .pdf-page[data-page="1"]').offsetHeight};
                })()""")
                assert placed['secondHeight'] < placed['firstHeight'] * .3
                assert abs(placed['scrollTop'] - (placed['top'] - 8)) <= 2, placed
                assert await page.locator('#reader-page').input_value() == '3'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_removing_a_paper_after_switching_workspaces_leaves_the_other_workspaces_draft(engine):
    """A paper removal whose flush is still refreshing when the reader switches
    workspaces finishes in the workspace it started in; a note being written
    there on a mark of a same-key paper in the new workspace is left alone."""
    config.set_ai_enabled(False)
    other = config.create_workspace('Elsewhere')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('paper', title='The same key elsewhere'))
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))
    claim = store.add_claim('paper', {'text':'A claim to delete.', 'tags':[]})
    rect = {'x':.1,'y':.08,'w':.3,'h':.02}

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                elsewhere = {'X-Doxograph-Workspace': other['id']}
                data = httpx.get(url+'/api/notebook', headers=elsewhere).json()
                data['highlights'] = [{'id':'far','paper':'paper','page':1,'rects':[rect],'text':'Elsewhere.','color':'blue','note':''}]
                assert httpx.put(url+'/api/notebook', json=data, headers=elsewhere).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator(f'.claim[data-claim="{claim["id"]}"]').wait_for()
                # A claim deleted and still in its Undo window.
                await page.locator(f'.claim[data-claim="{claim["id"]}"]').get_by_role('button', name='delete', exact=True).click()
                await page.locator('#toasts .toast', has_text='Deleted').wait_for()
                # Removing the paper flushes that delete first; the refresh that
                # ends the flush is held, and the reader switches workspaces.
                armed, held, release = [], [], asyncio.Event()
                async def hold_refresh(route):
                    request = route.request
                    if request.method == 'DELETE' and '/claims/' in request.url: armed.append(request.url)
                    elif (armed and request.method == 'GET' and '/api/state' in request.url
                            and request.headers.get('x-doxograph-workspace', 'default') == 'default'):
                        held.append(route); await release.wait()
                    await route.continue_()
                await page.route('**/api/**', hold_refresh)
                await page.get_by_role('button', name='Remove', exact=True).first.click()
                ask = page.locator('#ask')
                await ask.wait_for(state='visible')
                await ask.get_by_role('button', name='Remove', exact=True).click()
                deadline = time.monotonic() + 6
                while not held and time.monotonic() < deadline: await asyncio.sleep(.1)
                assert armed and held
                await page.locator('#workspace').select_option(other['id'])
                await page.locator('#papers li[data-paper="paper"]', has_text='The same key elsewhere').wait_for()
                await page.locator('#papers li[data-paper="paper"]').click()
                rail = page.locator('#content')
                await rail.locator('.highlight-entry[data-highlight="far"]').wait_for()
                await rail.locator('.highlight-entry[data-highlight="far"]').get_by_role('button', name='Add note', exact=True).click()
                await rail.locator('[data-highlight-note]').fill('Written elsewhere')
                # The held flush ends; the removal goes on in its own workspace.
                release.set()
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline:
                    if not any(p['key'] == 'paper' for p in httpx.get(url+'/api/state').json()['papers']): break
                    await asyncio.sleep(.1)
                assert not any(p['key'] == 'paper' for p in httpx.get(url+'/api/state').json()['papers'])
                await page.wait_for_timeout(500)
                assert any(p['key'] == 'paper' for p in httpx.get(url+'/api/state', headers=elsewhere).json()['papers'])
                assert await rail.locator('[data-highlight-note]').input_value() == 'Written elsewhere'
                assert await page.locator('#workspace').input_value() == other['id']
                await page.unroute('**/api/**')
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_zoom_keeps_the_page_under_the_eye_across_mixed_page_sizes(engine):
    """Under Fit width every page has a scale of its own, so a zoom change is
    anchored on the page being read and how far into it the view is, not on
    one ratio: a wide page above must not move the passage or the page saved."""
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A paper with a wide page',authors=['Ada Researcher'],year=2026))
    from pypdf import PdfReader, PdfWriter
    import io
    source = PdfReader(io.BytesIO(minimal_pdf(['First page.', 'A wide landscape page.', 'Third page.', 'Fourth page.'])))
    source.pages[1].mediabox.upper_right = (1000, 800)
    writer = PdfWriter()
    for pdf_page in source.pages:
        writer.add_page(pdf_page)
    with store.pdf_path('paper').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                data = httpx.get(url+'/api/notebook').json()
                data['reading'] = [{'paper':'paper','status':'reading','page':1,'bookmarks':[]}]
                assert httpx.put(url+'/api/notebook', json=data).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await page.locator('#reader .pdf-page[data-page="2"]:not(.pending)').wait_for()
                # Part way into page 3, by hand, so nothing is pinned.
                await page.evaluate("""(() => {
                    const scroller = document.querySelector('#pdf-scroll');
                    const third = document.querySelector('#reader .pdf-page[data-page="3"]');
                    scroller.scrollTo({top: third.offsetTop + third.offsetHeight * .4});
                })()""")
                await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                await saved_page(url, 3)
                async def within():
                    return await page.evaluate("""(() => {
                        const scroller = document.querySelector('#pdf-scroll');
                        const third = document.querySelector('#reader .pdf-page[data-page="3"]');
                        return (scroller.scrollTop - third.offsetTop) / third.offsetHeight;
                    })()""")
                for zoom in ['2', 'fit', '1']:
                    await page.locator('#reader-zoom').select_option(zoom)
                    await page.locator('#reader .pdf-page[data-page="3"] .textLayer span').first.wait_for()
                    await page.wait_for_timeout(300)
                    assert abs(await within() - .4) < .03, (zoom, await within())
                    assert await page.locator('#reader-page').input_value() == '3'
                await page.wait_for_timeout(1800)
                assert httpx.get(url+'/api/notebook').json()['reading'][0]['page'] == 3
                assert not errors
    run_browser(scenario())
