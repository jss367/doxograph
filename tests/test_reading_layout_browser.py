"""The reading layout gives space to the source without losing library state."""

import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import CLAIMS_OPEN, browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _poll, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('storage_available', [True, False])
def test_reading_pane_widths_resize_and_survive_narrow_windows(engine, storage_available):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Adjustable reading panes'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            if not storage_available:
                await page.add_init_script('''
                    Storage.prototype.getItem = () => { throw new Error('Unavailable'); };
                    Storage.prototype.setItem = () => { throw new Error('Unavailable'); };
                    Storage.prototype.removeItem = () => { throw new Error('Unavailable'); };
                ''')
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                side, claims = page.locator('#side'), page.locator('#claim-column')
                handle = page.locator('#claims-resize')

                async def width(node):
                    return round((await node.bounding_box())['width'])

                async def drag(selector, dx):
                    box = await page.locator(selector).bounding_box()
                    x, y = box['x'] + box['width'] / 2, box['y'] + 100
                    await page.mouse.move(x, y)
                    await page.mouse.down()
                    await page.mouse.move(x + dx, y, steps=5)
                    await page.mouse.up()

                await drag('#side-resize', -60)
                assert await width(side) == 260
                before = await width(claims)
                await drag('#claims-resize', -80)
                assert await width(claims) == before + 80
                await expect(handle).to_be_focused()
                await page.keyboard.press('ArrowRight')
                assert await width(claims) == before + 68
                await page.keyboard.press('Shift+ArrowLeft')
                wanted = before + 116
                assert await width(claims) == wanted
                await expect(handle).to_have_attribute('aria-valuenow', str(wanted))

                # A constrained window must not replace the requested widths.
                await page.set_viewport_size({'width': 850, 'height': 1000})
                await expect(handle).to_have_attribute('aria-valuemax', '310')
                assert await width(page.locator('#reader')) >= 280
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                await page.set_viewport_size({'width': 390, 'height': 1000})
                await expect(handle).to_be_hidden()
                await expect(page.locator('#side-resize')).to_be_hidden()
                await page.set_viewport_size({'width': 1440, 'height': 1000})
                await expect(claims).to_have_css('width', f'{wanted}px')
                assert await width(side) == 260

                if storage_available:
                    await page.reload()
                    await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                    assert await width(side) == 260
                    assert await width(claims) == wanted

                # Home and a stationary double-click restore the responsive default.
                await handle.focus()
                await page.keyboard.press('Home')
                assert await width(claims) == before
                await page.keyboard.press('ArrowLeft')
                assert await width(claims) == before + 12
                await handle.dblclick()
                assert await width(claims) == before
                await drag('#claims-resize', -30)
                await handle.dispatch_event('dblclick')
                assert await width(claims) == before + 30
                assert not await page.locator('body').evaluate("node => node.classList.contains('resizing-claims')")

                await drag('#claims-resize', 2000)
                assert await width(claims) == 240
                await drag('#claims-resize', -2000)
                assert await width(page.locator('#reader')) == 280
                await handle.focus()
                await page.keyboard.press('Home')
                assert await width(claims) == before

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_reading_panes_toggle_without_losing_the_document_or_drafts(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Reading with fewer distractions'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    claim = store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                # The claims wait in a closed drawer under the PDF.
                await expect(page.locator('#claims-drawer-bar')).to_contain_text('1 claim')
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await expect(page.locator('#claims-drawer-bar [data-act="toggle-claims"]')).to_have_attribute('aria-expanded', 'true')
                await page.locator(f'#claims-drawer-list [data-claim="{claim["id"]}"] [data-act="edit"]').click()
                draft = page.locator(f'form[data-form="{claim["id"]}"] textarea[name="note"]')
                await draft.fill('An unfinished thought beside the source.')
                await page.evaluate('''() => {
                    window.originalPdfPage = document.querySelector('#reader .pdf-page');
                    window.originalDraft = document.querySelector('form[data-form] textarea[name="note"]');
                }''')
                if width > 800:
                    await page.locator('#btn-reader-library').click()
                await page.locator('#btn-reader-claims').click()
                await expect(page.locator('#side')).to_be_hidden()
                await expect(page.locator('#claim-column')).to_be_hidden()
                await expect(page.locator('#claims-resize')).to_be_hidden()
                await expect(page.locator('#btn-reader-claims')).to_have_text('Show overview')
                await expect(page.locator('#btn-reader-claims')).to_have_attribute('aria-expanded', 'false')
                # The claims are under the PDF, so putting the overview away
                # leaves the draft on screen.
                await expect(draft).to_be_visible()
                if width > 800:
                    reader = await page.locator('#reader').bounding_box()
                    main = await page.locator('#main').bounding_box()
                    assert abs(reader['width'] - main['width']) <= 1

                await page.locator('#btn-reader-claims').click()
                await expect(draft).to_have_value('An unfinished thought beside the source.')
                assert await page.evaluate("originalDraft === document.querySelector('form[data-form] textarea[name=\"note\"]')")
                assert await page.evaluate("originalPdfPage === document.querySelector('#reader .pdf-page')")
                await page.locator('#btn-reader-library').click()
                await expect(page.locator('#side')).to_be_visible()

                await page.locator('#btn-reader-claims').click()
                # Adding a claim opens the drawer it is written in.
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                await page.locator(".paper-actions > summary").click()
                await page.locator('[data-act="add-claim"]').click()
                await expect(page.locator('#claims-drawer-list form[data-form="__new__"]')).to_be_visible()

                # Collapsing the reading column must not hide the library's claims.
                await page.locator('#btn-reader-claims').click()
                await page.locator('#btn-back-library').click()
                await expect(page.locator('#claim-column')).to_be_visible()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 1024, 390])
def test_claim_editor_fits_claims_drawer(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Editing claims beside a paper'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    store.save_ledger([{'id': 'L1', 'text': 'Oversight depends on the evaluation conditions.'}])
    claim = store.add_claim('paper', {
        'text': 'Oversight depends on the conditions.',
        'ledger_links': [{'claim': 'L1', 'relation': 'supports', 'note': 'Similar conditions.'}],
    })

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await page.locator(f'#claims-drawer-list [data-claim="{claim["id"]}"] [data-act="edit"]').click()
                form = page.locator(f'form[data-form="{claim["id"]}"]')
                await expect(form).to_be_visible()
                assert await page.locator('#claims-drawer-list').evaluate(
                    '(pane) => pane.scrollWidth <= pane.clientWidth')
                # Check the controls themselves, so clipping overflow cannot pass.
                assert await form.evaluate('''(form) => {
                    const bounds = form.getBoundingClientRect();
                    return [...form.querySelectorAll('input, select, textarea, button')].every((field) => {
                        const rect = field.getBoundingClientRect();
                        return rect.left >= bounds.left - 1 && rect.right <= bounds.right + 1;
                    });
                }''')
                await form.locator('textarea[name="note"]').fill('Edited beside the source.')
                await form.get_by_role('button', name='Save', exact=True).click()
                await expect(form).to_have_count(0)
                assert store.load_paper('paper')['claims'][0]['note'] == 'Edited beside the source.'
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_reading_layout_and_library_return(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Understanding model oversight',
                                     authors=['Alex Smith', 'Taylor Jones'], year=2026))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.', 'A second page.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await expect(page.locator('body')).to_have_class('reading-paper' + (' library-collapsed' if width < 800 else ''))
                await expect(page.locator('#continue-reading')).to_be_hidden()
                await expect(page.locator('#reading-claim-tools')).to_be_hidden()
                await expect(page.locator('[data-act="reextract"]')).to_be_hidden()
                reader = await page.locator('#pdf-scroll').bounding_box()
                pane = await page.locator('#reader').bounding_box()
                header = await page.locator('#paperhead').bounding_box()
                toolbar = await page.locator('.reader-toolbar').bounding_box()
                hint = await page.locator('#reader-hint').bounding_box()
                # Font metrics and wrapped controls vary by platform. The PDF
                # must follow its header/toolbar directly and fill the reader,
                # rather than begin before an arbitrary viewport coordinate.
                assert abs(pane['y'] - header['y'] - header['height']) <= 2
                assert abs(reader['y'] - toolbar['y'] - toolbar['height']) <= 2
                assert abs(reader['y'] + reader['height'] - hint['y']) <= 2
                assert reader['height'] > 0
                if width < 800:
                    assert abs(pane['height'] - page.viewport_size['height'] * .7) <= 2
                else:
                    # The reader runs down to the claims drawer's handle, and
                    # the handle to the bottom of the window.
                    main = await page.locator('#main').bounding_box()
                    drawer = await page.locator('#claims-drawer').bounding_box()
                    assert abs(pane['y'] + pane['height'] - drawer['y']) <= 2
                    assert abs(drawer['y'] + drawer['height'] - main['y'] - main['height']) <= 2
                    assert drawer['height'] < 60
                assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')

                # Background refreshes keep an open menu and its keyboard focus.
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Check quotes', exact=True).focus()
                await _poll(page)
                await expect(page.get_by_role('button', name='Check quotes', exact=True)).to_be_focused()
                await page.keyboard.press('Escape')

                # Show/hide the library without replacing the mounted document.
                await page.evaluate('window.originalPage = document.querySelector("#reader .pdf-page")')
                await page.locator('#btn-reader-library').click()
                await page.locator('#btn-reader-library').click()
                assert await page.evaluate('originalPage === document.querySelector("#reader .pdf-page")')

                if width > 800:
                    await page.set_viewport_size({'width': 700, 'height': 1000})
                    await expect(page.locator('#side')).to_be_hidden()
                    await expect(page.locator('#btn-reader-library')).to_have_attribute('aria-expanded', 'false')
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    await expect(page.locator('#side')).to_be_visible()

                # Filters are discoverable and do not silently disappear on exit.
                await page.keyboard.press('/')
                await expect(page.locator('#reading-claim-tools')).to_be_visible()
                await expect(page.locator('#btn-reader-filters')).to_have_attribute('aria-expanded', 'true')
                await expect(page.locator('#q')).to_be_focused()
                await page.keyboard.insert_text('Oversight')
                await ready(page)
                # The search of the papers' text answers after a debounce, and
                # its redraw rebuilds the paper header. Landing later, it can
                # swallow a click on the reopened paper below.
                await page.wait_for_function("V.textSearch?.q === 'Oversight' && V.textSearch.loading === false")
                await page.locator('#btn-reader-filters').click()
                await expect(page.locator('#btn-reader-filters')).to_have_text('Claim filters · active')
                await page.keyboard.press('/')
                await expect(page.locator('#q')).to_be_focused()
                assert await page.locator('#q').evaluate('(field) => field.selectionStart === 0 && field.selectionEnd === field.value.length')
                await page.locator('#btn-reader-filters').click()
                await page.locator('textarea[data-note="paper"]').fill('My unfinished thought.')
                await page.locator('#btn-back-library').click()
                await expect(page.locator('body')).not_to_have_class('reading-paper')
                await expect(page.locator('#q')).to_have_value('Oversight')
                # Leaving starts the note's save without waiting for it. Let it
                # land first, or the paper reopens on the draft still in flight.
                await page.wait_for_function("!paperNotesSaving() && V.noteDrafts.paper === undefined")
                # The saved note changes the corpus. Have the poll redraw for it
                # now, rather than between the press and release of a click.
                await _poll(page)
                await page.locator('#papers li[data-paper="paper"]').click()
                # Opening the PDF changes the reader's hint and shifts the note.
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                # Leaving the paper saved the note; it comes back as written.
                await expect(page.locator('#content .paper-overview .note')).to_contain_text('My unfinished thought.')
                assert store.load_paper('paper')['notes'] == 'My unfinished thought.'
                await page.locator('[data-act="edit-note"]').click()
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')

                # Escape dismisses the actions without cancelling the note draft.
                await page.locator('.paper-actions > summary').click()
                await _poll(page)
                await expect(page.locator('.paper-actions')).to_have_attribute('open', '')
                await page.keyboard.press('Escape')
                await expect(page.locator('.paper-actions')).not_to_have_attribute('open', '')
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Hide PDF', exact=True).click()
                await expect(page.locator('#reader')).to_be_hidden()
                await expect(page.locator('textarea[data-note="paper"]')).to_have_value('My unfinished thought.')
                await page.locator(".paper-actions > summary").click()
                await page.get_by_role('button', name='Show PDF', exact=True).click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await page.locator('textarea[data-note="paper"]').fill('Saved from the reader.')
                await page.locator('.note [data-act="done-note"]').click()
                await ready(page)
                await expect(page.locator('textarea[data-note="paper"]')).to_have_count(0)
                await expect(page.locator('#content .paper-overview .note')).to_contain_text('Saved from the reader.')
                assert store.load_paper('paper')['notes'] == 'Saved from the reader.'
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_palette_reveals_reading_search_and_import(engine, width):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Reading with the command palette'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A searchable passage.']))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': width, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page[data-page="1"]:not(.pending)').wait_for()
                await expect(page.locator('#reading-claim-tools')).to_be_hidden()
                await expect(page.locator('#refs')).to_be_hidden()
                await page.evaluate('window.originalPage = document.querySelector("#reader .pdf-page")')
                await page.keyboard.press('Meta+k')
                await page.locator('#command-query').fill('Search claims and PDF text')
                await page.keyboard.press('Enter')
                await expect(page.locator('#command-palette')).to_be_hidden()
                await expect(page.locator('#reading-claim-tools')).to_be_visible()
                await expect(page.locator('#btn-reader-filters')).to_have_attribute('aria-expanded', 'true')
                await expect(page.locator('#q')).to_be_focused()
                await page.keyboard.insert_text('searchable')
                await expect(page.locator('#q')).to_have_value('searchable')
                await page.locator('#btn-reader-filters').click()
                await page.keyboard.press('Meta+k')
                await page.locator('#command-query').fill('Add papers')
                await page.keyboard.press('Enter')
                await expect(page.locator('#command-palette')).to_be_hidden()
                await expect(page.locator('#side')).to_be_visible()
                await expect(page.locator('#btn-reader-library')).to_have_attribute('aria-expanded', 'true')
                await expect(page.locator('#paper-import')).to_have_attribute('open', '')
                await expect(page.locator('#refs')).to_be_focused()
                await expect(page.locator('#refs')).to_be_in_viewport()
                assert await page.evaluate('originalPage === document.querySelector("#reader .pdf-page")')
                assert await page.locator('#q').input_value() == 'searchable'
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_sidebar_paper_click_survives_search_refresh(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Paper to open'))
    store.add_claim('paper', {'text': 'A claim to read.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator("#q").fill("Paper")
                await page.wait_for_function("V.textSearch?.loading === false")
                title = page.locator('#papers li[data-paper="paper"] .pt')
                await expect(title).to_be_visible()
                box = await title.bounding_box()
                await page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                await page.mouse.down()
                # Completing a text search must not replace an unchanged
                # sidebar between a user's press and release.
                await page.evaluate("runTextSearch('Paper', true)")
                await page.mouse.up()
                await expect(page.locator('#paperhead')).to_contain_text('Paper to open')
                await expect(page.locator('#papers li[data-paper="paper"]')).to_have_class('active')
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_claims_drawer_is_remembered_and_opened_by_a_claim_link(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    first = store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})
    second = store.add_claim('paper', {'text': 'Weak judges miss subtle errors.', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                bar = page.locator('#claims-drawer-bar')
                await expect(bar).to_contain_text('2 claims')
                await expect(bar).to_contain_text('1 unreviewed')
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                # The overview column holds no claims.
                await expect(page.locator('#claim-column .claim')).to_have_count(0)

                # A reload comes back to the drawer as it was left.
                await page.reload()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                await bar.locator('[data-act="toggle-claims"]').click()
                await expect(page.locator(f'#claims-drawer-list .claim[data-claim="{first["id"]}"]')).to_be_visible()
                await page.reload()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await expect(page.locator('#claims-drawer-list')).to_be_visible()
                await bar.locator('[data-act="toggle-claims"]').click()
                await expect(bar.locator('[data-act="toggle-claims"]')).to_have_text('Show claims')

                # The keys that move between claims open the drawer.
                await page.locator('body').press('j')
                await expect(page.locator('#claims-drawer-list')).to_be_visible()
                await bar.locator('[data-act="toggle-claims"]').click()

                # A link to one claim opens the drawer on it, and leaves the
                # address as the page writes it.
                await page.goto(f'{url}/claim/{second["id"]}')
                card = page.locator(f'#claims-drawer-list .claim.sel[data-claim="{second["id"]}"]')
                await expect(card).to_be_visible()
                await page.wait_for_function("!location.hash.includes('show=')")

                # Off the reader, the claims are in the content pane again and
                # the drawer holds no second copy of them.
                await page.locator('#btn-back-library').click()
                await expect(page.locator('#claims-drawer')).to_be_hidden()
                await expect(page.locator('#claims-drawer-list .claim')).to_have_count(0)
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_search_of_the_pdf_text_opens_the_claims_drawer(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['The judges discuss sandbagging at length.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                drawer = page.locator('#claims-drawer-list')
                toggle = page.locator('#claims-drawer-bar [data-act="toggle-claims"]')
                await expect(drawer).to_be_hidden()

                # A search that finds nothing in the PDFs leaves the drawer shut.
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('nowhere')
                await page.wait_for_function("V.textSearch?.q === 'nowhere' && V.textSearch.loading === false")
                await expect(drawer).to_be_hidden()

                # One that finds a passage opens it on what it found.
                await page.locator('#q').fill('sandbagging')
                await expect(drawer.locator('.pdfhit .pp mark')).to_have_text('sandbagging')
                await expect(toggle).to_have_text('Hide claims')

                # Closed again, the standing query does not reopen it: neither a
                # redraw nor the quiet re-run a corpus change asks for.
                await toggle.click()
                await page.evaluate("renderContent()")
                await page.evaluate("runTextSearch('sandbagging', true)")
                await _poll(page)
                await expect(drawer).to_be_hidden()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_claims_drawer_buttons_keep_focus_across_a_redraw(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                bar = page.locator('#claims-drawer-bar')
                await expect(bar).to_contain_text('1 claim')

                # A claim written elsewhere redraws the bar under the reader's
                # keyboard focus, and the focus stays on the same button.
                for n, act in enumerate(['toggle-claims', 'review-claims'], start=2):
                    await bar.locator(f'[data-act="{act}"]').focus()
                    store.add_claim('paper', {'text': f'Claim number {n}.'})
                    await _poll(page)
                    await expect(bar).to_contain_text(f'{n} claims')
                    await expect(bar.locator(f'[data-act="{act}"]')).to_be_focused()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_moving_to_a_claim_link_in_the_page_opens_the_drawer(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    first = store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})
    second = store.add_claim('paper', {'text': 'Weak judges miss subtle errors.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()

                # A move within the page, as Back, Forward and an edited address
                # make, opens the drawer on the claim the address names.
                go = "([sel, show]) => { location.hash = `view=claims&paper=paper&sel=${encodeURIComponent(sel)}${show}`; }"
                await page.evaluate(go, [second['id'], '&show=claim'])
                card = page.locator(f'#claims-drawer-list .claim.sel[data-claim="{second["id"]}"]')
                await expect(card).to_be_visible()
                await page.wait_for_function("!location.hash.includes('show=')")

                # One that does not ask for it leaves the drawer as it was left.
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await page.evaluate(go, [first['id'], ''])
                await page.wait_for_function("id => V.selectedId === id", arg=first['id'])
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_claim_link_from_a_dataset_opens_the_drawer(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})
    linked = store.add_claim('paper', {'text': 'Weak judges miss subtle errors.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                item = httpx.post(url + '/api/datasets', json={'name': 'Corpus', 'papers': ['paper'],
                    'claims': [{'paper': 'paper', 'claim': linked['id']}]}).json()
                await page.goto(f'{url}/#view=datasets&dataset={item["id"]}')
                await page.wait_for_function(
                    "document.querySelector('#datasets-sheet').getAttribute('aria-busy') !== 'true'")
                # The drawer was left closed, and a link to one claim opens it.
                assert await page.evaluate('claimsDrawerOpen') is False
                await page.locator('#dataset-research').get_by_role('button', name=linked['text'], exact=True).click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                card = page.locator(f'#claims-drawer-list .claim.sel[data-claim="{linked["id"]}"]')
                await expect(card).to_be_visible()
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_failed_claim_save_shows_with_the_overview_hidden(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    claim = store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def refuse(route):
        if route.request.method == 'GET':
            await route.continue_()
        else:
            await route.fulfill(status=500, json={'detail': 'Saving is unavailable.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            await page.add_init_script(CLAIMS_OPEN)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await page.locator('#btn-reader-claims').click()
                await expect(page.locator('#btn-reader-claims')).to_have_text('Show overview')

                await page.route('**/api/papers/paper/claims/**', refuse)
                await page.locator(f'#claims-drawer-list [data-act="edit"][data-claim="{claim["id"]}"]').click()
                form = page.locator(f'#claims-drawer-list form[data-form="{claim["id"]}"]')
                await form.locator('[name="text"]').fill('Oversight depends on the task.')
                await form.get_by_role('button', name='Save', exact=True).click()
                # Reported on the drawer's handle, which the hidden overview
                # does not take with it, and not a second time beside the PDF.
                warning = page.locator('#claims-drawer-bar .warn')
                await expect(warning).to_contain_text('Could not save the claim')
                await expect(warning).to_be_visible()
                await expect(page.locator('#content .warn')).to_have_count(0)
                await expect(form.locator('[name="text"]')).to_have_value('Oversight depends on the task.')

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_failed_search_of_the_pdf_text_opens_the_claims_drawer(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['The judges discuss sandbagging at length.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def refuse(route):
        await route.fulfill(status=500, json={'detail': 'Search is unavailable.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                drawer = page.locator('#claims-drawer-list')
                await expect(drawer).to_be_hidden()

                # The reader's search could not run: the drawer opens on why.
                await page.route('**/api/search*', refuse)
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('sandbagging')
                await expect(drawer.locator('.pdfhits')).to_contain_text('Could not search the papers')
                await expect(drawer).to_be_visible()

                # A quiet re-run that fails again leaves a closed drawer closed.
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await page.locator('#q').fill('sandbag')
                await page.wait_for_function("V.textSearch?.q === 'sandbag' && V.textSearch.loading === false")
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await expect(drawer).to_be_hidden()
                await page.evaluate("runTextSearch('sandbag', true)")
                await _poll(page)
                await expect(drawer).to_be_hidden()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_resuming_a_paper_from_the_reading_queue_leaves_the_drawer_closed(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['A passage about oversight.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['reading'] = [{'paper': 'paper', 'status': 'queued', 'page': 1, 'bookmarks': []}]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.wait_for_function('libraryInitialized')
                await page.locator('#btn-command-palette').click()
                await page.locator('#command-query').fill('Open reading queue')
                await page.locator('#command-results').get_by_text('Open reading queue', exact=True).click()
                await ready(page)
                # The queue asks for the paper, not one of its claims: the
                # first claim is selected, but the drawer stays as it was left.
                await page.locator('#research-tools').get_by_role('button', name='Resume at page 1', exact=True).click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await expect(page.locator('#claims-drawer-list .claim.sel')).to_have_count(1)
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()
                assert await page.evaluate('claimsDrawerOpen') is False
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_drawer_focus_is_not_carried_to_another_paper(engine):
    config.set_ai_enabled(False)
    for key, title in [('one', 'The first paper'), ('two', 'The second paper')]:
        store.save_paper(store.new_paper(key, title=title))
        store.pdf_path(key).write_bytes(minimal_pdf(['A passage about oversight.']))
        store.add_claim(key, {'text': f'A claim from {title}.'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="one"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await page.locator('#papers li[data-paper="two"]').click()
                await expect(page.locator('#paperhead')).to_contain_text('The second paper')
                toggle = page.locator('#claims-drawer-bar [data-act="toggle-claims"]')
                await toggle.focus()
                # Back to the first paper with focus on the second's toggle:
                # the first paper's handle is a different control.
                await page.go_back()
                await expect(page.locator('#paperhead')).to_contain_text('The first paper')
                await expect(toggle).not_to_be_focused()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_late_search_answer_does_not_open_another_papers_drawer(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Claims kept out of the way'))
    store.pdf_path('paper').write_bytes(minimal_pdf(['The judges discuss sandbagging at length.']))
    store.add_claim('paper', {'text': 'Oversight depends on the conditions.'})
    store.save_paper(store.new_paper('other', title='Sandbagging under oversight'))
    store.pdf_path('other').write_bytes(minimal_pdf(['Models may be sandbagging on purpose.']))
    store.add_claim('other', {'text': 'Weak judges miss subtle errors.'})

    async def scenario():
        held, release = asyncio.Event(), asyncio.Event()

        async def hold(route):
            held.set()
            await release.wait()
            await route.continue_()

        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                await page.route('**/api/search*', hold)
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('sandbagging')
                await asyncio.wait_for(held.wait(), timeout=10)

                # The reader moves on before the answer comes back. It is drawn
                # under the paper now open, whose drawer stays as it was left.
                await page.locator('#papers li[data-paper="other"]').click()
                await expect(page.locator('#paperhead')).to_contain_text('Sandbagging under oversight')
                release.set()
                await page.wait_for_function("V.textSearch?.q === 'sandbagging' && V.textSearch.loading === false")
                await page.locator('#claims-drawer-list .pdfhit').first.wait_for(state='attached')
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()

    run_browser(scenario())


def _two_searchable_papers():
    for key, title in [('one', 'Sandbagging in the first paper'), ('two', 'Sandbagging in the second paper')]:
        store.save_paper(store.new_paper(key, title=title))
        store.pdf_path(key).write_bytes(minimal_pdf(['The judges discuss sandbagging at length.']))
        store.add_claim(key, {'text': f'A claim from {title}.'})


async def _hold_searches(page):
    """Hold the next text search until the returned event is set."""
    held, release = asyncio.Event(), asyncio.Event()

    async def hold(route):
        held.set()
        await release.wait()
        await route.continue_()
    await page.route('**/api/search*', hold)
    return held, release


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_late_search_answer_after_leaving_and_returning_leaves_the_drawer(engine):
    config.set_ai_enabled(False)
    _two_searchable_papers()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="one"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                held, release = await _hold_searches(page)
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('sandbagging')
                await asyncio.wait_for(held.wait(), timeout=10)

                # Away and back while the answer is out: the paper is the same
                # one, but the reader has moved since asking.
                await page.locator('#papers li[data-paper="two"]').click()
                await expect(page.locator('#paperhead')).to_contain_text('the second paper')
                await page.locator('#papers li[data-paper="one"]').click()
                await expect(page.locator('#paperhead')).to_contain_text('the first paper')
                release.set()
                await page.wait_for_function("V.textSearch?.q === 'sandbagging' && V.textSearch.loading === false")
                await page.locator('#claims-drawer-list .pdfhit').first.wait_for(state='attached')
                await expect(page.locator('#claims-drawer-list')).to_be_hidden()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_a_drawer_closed_while_a_search_is_out_stays_closed(engine):
    config.set_ai_enabled(False)
    _two_searchable_papers()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            await page.add_init_script(CLAIMS_OPEN)
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="one"]').click()
                await page.locator('#reader .pdf-page:not(.pending)').first.wait_for()
                drawer = page.locator('#claims-drawer-list')
                await expect(drawer).to_be_visible()
                held, release = await _hold_searches(page)
                await page.locator('#btn-reader-filters').click()
                await page.locator('#q').fill('sandbagging')
                await asyncio.wait_for(held.wait(), timeout=10)

                # Closed by the reader while the answer is out: their choice
                # stands when it lands.
                await page.locator('#claims-drawer-bar [data-act="toggle-claims"]').click()
                await expect(drawer).to_be_hidden()
                release.set()
                await page.wait_for_function("V.textSearch?.q === 'sandbagging' && V.textSearch.loading === false")
                await drawer.locator('.pdfhit').first.wait_for(state='attached')
                await expect(drawer).to_be_hidden()

    run_browser(scenario())
