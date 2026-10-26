"""Browser regressions for reader lifecycle invariants."""
import asyncio
import io

import httpx
import pytest
from pypdf import PdfReader, PdfWriter

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _poll, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('removed', ['mark', 'paper'])
def test_removed_mark_keeps_the_entire_changed_note_recoverable(engine, removed):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A paper with notes'))
    note = 'A long draft with details that must remain recoverable. ' * 12

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['highlights'] = [{'id':'mark', 'paper':'paper', 'page':1,
                    'rects':[{'x':.1, 'y':.1, 'w':.3, 'h':.02}],
                    'text':'A passage', 'color':'yellow', 'note':'Saved note'}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.get_by_role('button', name='Edit note', exact=True).click()
                await page.locator('[data-highlight-note]').fill(note)
                if removed == 'mark':
                    await page.get_by_role('button', name='Add claim by hand', exact=True).click()
                    await page.locator('form[data-form] [name="text"]').fill('A separate unsaved claim')
                    notebook = httpx.get(url + '/api/notebook').json()
                    notebook['highlights'] = []
                    assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                else:
                    assert httpx.delete(url + '/api/papers/paper').status_code == 200
                await _poll(page)
                field = page.get_by_role('textbox', name='Note on removed mark', exact=True)
                await field.wait_for()
                assert await field.input_value() == note
                if removed == 'mark':
                    assert await page.locator('form[data-form] [name="text"]').input_value() == 'A separate unsaved claim'
                assert await page.evaluate('ResearchTools.holds()')
                # A second poll cannot consume or truncate recovery text.
                await _poll(page)
                assert await field.input_value() == note
                await page.evaluate("Object.defineProperty(navigator, 'clipboard', {value:{writeText:async text => {window.copiedNote = text;}}})")
                await page.get_by_role('button', name='Copy note', exact=True).click()
                await ready(page)
                assert await page.evaluate('window.copiedNote') == note
                await page.get_by_role('button', name='Discard note', exact=True).click()
                await page.locator('#ask').get_by_role('button', name='Cancel', exact=True).click()
                await ready(page)
                assert await field.input_value() == note
                await page.get_by_role('button', name='Discard note', exact=True).click()
                await page.locator('#ask').get_by_role('button', name='Discard note', exact=True).click()
                await ready(page)
                assert await field.count() == 0
                assert not await page.evaluate('ResearchTools.holds()')
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_resume_cannot_override_navigation_away_and_back(engine):
    config.set_ai_enabled(False)
    for key in ['paper', 'second']:
        store.save_paper(store.new_paper(key, title=key.title()))
    store.pdf_path('paper').write_bytes(minimal_pdf(['First page.', 'Second page.']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['reading'] = [{'paper':'paper', 'status':'queued', 'page':2, 'bookmarks':[]}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="second"]').click()
                await page.get_by_role('button', name='Reading queue', exact=True).click()
                await ready(page)
                held, release = asyncio.Event(), asyncio.Event()
                async def hold_get(route):
                    if route.request.method == 'GET' and not held.is_set():
                        held.set()
                        await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_get)
                await page.locator('#research-tools').get_by_role('button', name='Resume at page 2', exact=True).click()
                await asyncio.wait_for(held.wait(), timeout=10)
                # The initiating view is Second. Navigate away and then Back,
                # returning to the exact same paper/view while Resume waits.
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.go_back()
                await page.locator('.paperhead h2', has_text='Second').wait_for()
                release.set()
                await ready(page)
                assert await page.locator('.paperhead h2').inner_text() == 'Second'
                assert await page.locator('#reader').is_hidden()
                # The scoped queue mutation is still allowed to finish.
                assert httpx.get(url + '/api/notebook').json()['reading'][0]['status'] == 'reading'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_explicit_page_target_survives_its_own_delayed_measurement(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Mixed page heights'))
    source = PdfReader(io.BytesIO(minimal_pdf([f'Page {n}.' for n in range(1, 31)])))
    source.pages[0].mediabox.upper_right = (612, 120)
    writer = PdfWriter()
    for item in source.pages:
        writer.add_page(item)
    with store.pdf_path('paper').open('wb') as stream:
        writer.write(stream)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                assert await page.locator('#reader .pdf-page[data-page="20"].pending').count() == 1
                await page.locator('#reader-page').fill('20')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.locator('#reader .pdf-page[data-page="20"]:not(.pending)').wait_for()
                await ready(page)
                await page.wait_for_timeout(300)
                offset = await page.evaluate('''() => {
                    const scroller = document.querySelector('#pdf-scroll');
                    const target = scroller.querySelector('[data-page="20"]');
                    return scroller.scrollTop - (target.offsetTop - 8);
                }''')
                assert abs(offset) <= 2, offset
                assert await page.locator('#reader-page').input_value() == '20'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('removed', ['mark', 'paper'])
def test_mark_editor_reconciles_clean_removal_and_conflicting_notes(engine, removed):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A paper with notes'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['highlights'] = [{'id':'mark', 'paper':'paper', 'page':1,
                    'rects':[{'x':.1, 'y':.1, 'w':.3, 'h':.02}],
                    'text':'A passage', 'color':'yellow', 'note':'Saved note'}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.get_by_role('button', name='Edit note', exact=True).click()
                # A different writer saves first. A stale editor must not
                # overwrite that note, or falsely claim the draft was saved.
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['highlights'][0]['note'] = 'Written in another window'
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await _poll(page)
                await page.locator('[data-highlight-note]').fill('My changed note')
                await page.get_by_role('button', name='Save note', exact=True).click()
                await ready(page)
                assert await page.locator('[data-highlight-note]').input_value() == 'My changed note'
                assert httpx.get(url + '/api/notebook').json()['highlights'][0]['note'] == 'Written in another window'
                await page.locator('#toasts .toast', has_text='note changed in another window').wait_for()
                await page.locator('#content').get_by_role('button', name='Cancel', exact=True).click()
                await ready(page)
                await page.get_by_role('button', name='Edit note', exact=True).click()
                # A clean editor closes without a recovery warning or phantom
                # unsaved state when the mark is subsequently removed.
                if removed == 'mark':
                    notebook = httpx.get(url + '/api/notebook').json()
                    notebook['highlights'] = []
                    assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                else:
                    assert httpx.delete(url + '/api/papers/paper').status_code == 200
                await _poll(page)
                assert await page.locator('[data-highlight-note]').count() == 0
                assert await page.locator('[data-removed-mark]').count() == 0
                assert not await page.evaluate('ResearchTools.holds()')
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_mark_navigation_finishing_after_a_paper_change_has_no_effect(engine):
    config.set_ai_enabled(False)
    for key in ['paper', 'second']:
        store.save_paper(store.new_paper(key, title=key.title()))
    store.pdf_path('paper').write_bytes(minimal_pdf([f'Page {n}.' for n in range(1, 31)]))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['highlights'] = [{'id':'mark', 'paper':'paper', 'page':20,
                    'rects':[{'x':.1, 'y':.1, 'w':.3, 'h':.02}],
                    'text':'A distant passage', 'color':'yellow', 'note':''}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await page.evaluate('''() => {
                    document.querySelector('[data-tool="highlight-page"]').click();
                    document.querySelector('#papers li[data-paper="second"]').click();
                }''')
                await ready(page)
                await page.locator('.paperhead h2', has_text='Second').wait_for()
                assert await page.locator('#reader').is_hidden()
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('destination', ['hidden', 'second'])
def test_delayed_bookmark_does_not_change_a_replacement_reader(engine, destination):
    config.set_ai_enabled(False)
    for key in ['paper', 'second']:
        store.save_paper(store.new_paper(key, title=key.title()))
        store.pdf_path(key).write_bytes(minimal_pdf([f'Page {n}.' for n in range(1, 5)]))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['reading'] = [{'paper':key, 'status':'reading', 'page':number, 'bookmarks':[]}
                                       for key, number in [('paper', 3), ('second', 1)]]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="3"]:not(.pending)').wait_for()
                held, release = asyncio.Event(), asyncio.Event()
                async def hold_bookmark(route):
                    if route.request.method == 'PUT' and not held.is_set():
                        held.set()
                        await release.wait()
                    await route.continue_()
                await page.route('**/api/notebook', hold_bookmark)
                await page.get_by_role('button', name='Bookmark this page', exact=True).click()
                await asyncio.wait_for(held.wait(), timeout=10)
                if destination == 'hidden':
                    await page.get_by_role('button', name='Hide PDF', exact=True).click()
                    await page.locator('#reader').wait_for(state='hidden')
                else:
                    await page.locator('#papers li[data-paper="second"]').click()
                    await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                    # Scrolling remains live while the bookmark request waits.
                    # Its completion must not claim this newer page is saved.
                    await page.evaluate('''() => {
                        const pane = document.querySelector('#pdf-scroll');
                        pane.scrollTop = pane.querySelector('[data-page="3"]').offsetTop;
                    }''')
                    await page.wait_for_function("document.querySelector('#reader-page').value === '3'")
                release.set()
                await ready(page)
                notebook = httpx.get(url + '/api/notebook').json()
                assert notebook['reading'][0]['bookmarks'] == [3]
                if destination == 'second':
                    for _ in range(60):
                        notebook = httpx.get(url + '/api/notebook').json()
                        if notebook['reading'][1]['page'] == 3:
                            break
                        await asyncio.sleep(.1)
                    assert notebook['reading'][1]['page'] == 3
                    assert await page.locator('#reader-page').input_value() == '3'
                else:
                    assert await page.locator('#reader').is_hidden()
                assert await page.locator('#toasts .toast.warn').count() == 0
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('stage', ['page', 'canvas', 'text', 'text-layer'])
def test_a_persistent_pdf_failure_does_not_retry_without_user_action(engine, stage):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A paper with an unreadable page'))
    store.pdf_path('paper').write_bytes(minimal_pdf([f'Page {n}.' for n in range(1, 31)]))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440, 'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def fail_pdf_stage(route):
                # Use real PDF.js throughout, injecting only a permanent
                # failure at the selected boundary on a distant page.
                module = '''
                    export * from '/vendor/pdf.min.mjs?reader-test-original=1';
                    import * as real from '/vendor/pdf.min.mjs?reader-test-original=1';
                    const stage = STAGE;
                    function fail() {
                        window.readerFailureAttempts = (window.readerFailureAttempts || 0) + 1;
                        // Bound the old implementation too, so a failing test
                        // cannot starve the browser with endless microtasks.
                        if (window.readerFailureAttempts > 4) return new Promise(() => {});
                        return Promise.reject(new Error('Deliberate permanent PDF failure'));
                    }
                    export function getDocument(options) {
                        const task = real.getDocument(options);
                        return {promise:task.promise.then(doc => {
                            const getPage = doc.getPage.bind(doc);
                            doc.getPage = number => {
                                if (number === 20 && stage === 'page') return fail();
                                return getPage(number).then(page => {
                                    if (number === 20 && stage === 'canvas') page.render = () => ({promise:fail()});
                                    if (number === 20 && stage === 'text') page.getTextContent = fail;
                                    return page;
                                });
                            };
                            return doc;
                        })};
                    }
                    export class TextLayer {
                        constructor(options) { this.options = options; this.layer = new real.TextLayer(options); }
                        render() {
                            if (stage === 'text-layer' && this.options.container.closest('[data-page]').dataset.page === '20') return fail();
                            return this.layer.render();
                        }
                    }
                '''.replace('STAGE', repr(stage))
                await route.fulfill(body=module, content_type='text/javascript')
            await page.route('**/vendor/pdf.min.mjs', fail_pdf_stage)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await page.locator('#reader-page').fill('20')
                await page.get_by_role('button', name='Go', exact=True).click()
                await page.locator('#reader-hint', has_text='Deliberate permanent PDF failure').wait_for()
                await page.wait_for_timeout(400)
                assert await page.evaluate('window.readerFailureAttempts') == 1
                # The error remains visible; no partial canvas is retained.
                assert 'Deliberate permanent PDF failure' in await page.locator('#reader-hint').inner_text()
                assert await page.locator('#reader .pdf-page[data-page="20"] canvas').count() == 0
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('stage', ['document', 'first-page'])
def test_pdf_open_failure_can_be_retried_and_cannot_replace_a_newer_view(engine, stage):
    config.set_ai_enabled(False)
    for key in ['paper', 'second']:
        store.save_paper(store.new_paper(key, title=key.title()))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A paper that opens after retry.']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            async def fail_open(route):
                module = '''
                    export * from '/vendor/pdf.min.mjs?reader-test-original=1';
                    import * as real from '/vendor/pdf.min.mjs?reader-test-original=1';
                    const stage = STAGE;
                    export function getDocument(options) {
                        const attempt = window.readerOpenAttempts = (window.readerOpenAttempts || 0) + 1;
                        const held = window.holdReaderOpen;
                        function failure() {
                            if (held) {
                                window.readerOpenHeld = true;
                                return new Promise((_, reject) => {
                                    window.releaseFailedOpen = () => reject(new Error('Delayed PDF open failure'));
                                });
                            }
                            return Promise.reject(new Error('Transient PDF open failure'));
                        }
                        if (stage === 'document' && (attempt === 1 || held)) {
                            return {promise:failure(), destroy:async () => {}};
                        }
                        const task = real.getDocument(options);
                        return {destroy:() => task.destroy(), promise:task.promise.then(doc => {
                            const getPage = doc.getPage.bind(doc);
                            doc.getPage = number => number === 1 && stage === 'first-page' && (attempt === 1 || held)
                                ? failure() : getPage(number);
                            return doc;
                        })};
                    }
                '''.replace('STAGE', repr(stage))
                await route.fulfill(body=module, content_type='text/javascript')
            await page.route('**/vendor/pdf.min.mjs', fail_open)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader-hint', has_text='Transient PDF open failure').wait_for()
                await _poll(page)
                assert await page.evaluate('window.readerOpenAttempts') == 1
                await page.get_by_role('button', name='Retry PDF', exact=True).click()
                await page.locator('#reader .pdf-page[data-page="1"] .textLayer span').first.wait_for()
                await ready(page)
                assert await page.evaluate('window.readerOpenAttempts') == 2
                assert await page.get_by_role('button', name='Retry PDF', exact=True).count() == 0
                # A later failed open belongs to the paper left behind. Its
                # error and retry controls must not appear over the new view.
                await page.locator('#papers li[data-paper="second"]').click()
                await page.evaluate('window.holdReaderOpen = true')
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.wait_for_function('window.readerOpenHeld === true')
                await page.locator('#papers li[data-paper="second"]').click()
                await page.locator('.paperhead h2', has_text='Second').wait_for()
                await page.evaluate('window.releaseFailedOpen()')
                await page.wait_for_timeout(200)
                assert await page.locator('#reader').is_hidden()
                assert await page.get_by_role('button', name='Retry PDF', exact=True).count() == 0
                assert await page.locator('#toasts .toast.warn').count() == 0
                assert not errors
    run_browser(scenario())
