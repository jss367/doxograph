"""App edits and text editing share the conventional undo shortcuts."""
import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _server
from test_research_tools_browser import ready, select_first_line


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_highlights_undo_redo_and_text_editing(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Undoable annotations'))
    store.pdf_path('paper').write_bytes(minimal_pdf('Recovery depends on conditions.'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#papers li[data-paper="paper"]').click()
                await select_first_line(page)
                await page.get_by_role('button', name='Highlight', exact=True).click()
                await ready(page)
                mark = page.locator('.pdf-page .highlight-yellow')
                await mark.wait_for()
                # The app focuses an empty note automatically. Undo must still
                # remove the highlight immediately, with no extra click.
                await page.keyboard.press('Meta+z')
                await mark.wait_for(state='hidden')
                assert httpx.get(url + '/api/notebook').json()['highlights'] == []
                await page.keyboard.press('Meta+Shift+z')
                await mark.wait_for()
                await page.get_by_role('button', name='Color green', exact=True).click()
                await ready(page)
                green = page.locator('.pdf-page .highlight-green')
                await green.wait_for()
                await page.keyboard.press('Control+z')
                await mark.wait_for()
                await page.keyboard.press('Control+Shift+z')
                await green.wait_for()
                # A typed note uses native text undo and leaves the mark intact.
                # Native text undo is Command-Z on macOS and Control-Z on Linux.
                await page.get_by_role('button', name='Add note', exact=True).click()
                field = page.locator('[data-highlight-note]')
                await field.press_sequentially('My note')
                await field.press('ControlOrMeta+z')
                assert await field.input_value() != 'My note'
                assert len(httpx.get(url + '/api/notebook').json()['highlights']) == 1
                # The function used by the native menu follows the same route.
                await page.locator('[data-tool="cancel-highlight"]').click()
                assert await page.evaluate('window.doxographUndo(false)') is True
                await mark.wait_for()
                assert await page.evaluate('window.doxographUndo(false)') is True
                await mark.wait_for(state='hidden')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_document_and_claim_changes_follow_last_edit_order(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Original title'))
    claim = store.add_claim('paper', {'text': 'A finding', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f'{url}/#view=claims&paper=paper')
                await page.locator('.claim').wait_for()
                await page.evaluate("""async id => {
                  const headers = {'Content-Type':'application/json'};
                  await api('/api/papers/paper', {method:'PATCH', headers, body:JSON.stringify({title:'Corrected',labels:['reading']})});
                  await api('/api/papers/paper/claims/' + id, {method:'PATCH', headers, body:JSON.stringify({reviewed:true})});
                  await refreshAll();
                }""", claim['id'])
                await page.keyboard.press('Meta+z')
                await page.wait_for_function('!S.claims[0].reviewed')
                assert httpx.get(url + '/api/papers/paper').json()['title'] == 'Corrected'
                await page.keyboard.press('Meta+z')
                await page.wait_for_function('S.papers[0].title === "Original title"')
                assert httpx.get(url + '/api/papers/paper').json()['labels'] == []
                await page.keyboard.press('Meta+Shift+z')
                await page.wait_for_function('S.papers[0].title === "Corrected"')

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_keyboard_undo_restores_pending_delete_and_bulk_review(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Review and deletion'))
    claim = store.add_claim('paper', {'text': 'An unreviewed finding', 'reviewed': False})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f'{url}/#view=claims&paper=paper')
                card = page.locator(f'.claim[data-claim="{claim["id"]}"]')
                await card.wait_for()
                await card.get_by_role('button', name='delete', exact=True).click()
                await page.locator('#toasts .toast', has_text='Deleted the claim.').wait_for()
                await card.wait_for(state='detached')
                await page.keyboard.press('Meta+z')
                await card.wait_for()
                assert len(httpx.get(url + '/api/papers/paper').json()['claims']) == 1
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Mark 1 reviewed', exact=True).click()
                await page.locator('#toasts .toast', has_text='1 claim marked reviewed.').wait_for()
                await page.keyboard.press('Meta+z')
                await page.wait_for_function('!S.claims[0].reviewed')
                await page.keyboard.press('Meta+Shift+z')
                await page.wait_for_function('S.claims[0].reviewed')

    run_browser(scenario())


@pytest.mark.browser
def test_label_save_failure_keeps_the_draft_and_success_can_be_undone():
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Label edits'))

    async def scenario():
        async with browser_session('chromium') as browser:
            page = await browser.new_page()
            failed = False

            async def fail_once(route):
                nonlocal failed
                if route.request.method == 'PATCH' and not failed:
                    failed = True
                    await route.fulfill(status=503, content_type='application/json', body='{"detail":"Disk full"}')
                else:
                    await route.continue_()

            with _server() as url:
                await page.route('**/api/papers/paper', fail_once)
                await page.goto(f'{url}/#view=claims&paper=paper')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Labels', exact=True).click()
                await page.locator('#ask-input').fill('reading list')
                await page.locator('#ask-ok').click()
                await page.wait_for_function('document.querySelector("#ask-note").textContent.includes("Disk full")')
                assert await page.locator('#ask-input').input_value() == 'reading list'
                assert await page.locator('#ask').is_visible()
                await page.locator('#ask-ok').click()
                await page.locator('#ask').wait_for(state='hidden')
                assert httpx.get(url + '/api/papers/paper').json()['labels'] == ['reading list']
                await page.keyboard.press('Meta+z')
                await page.wait_for_function('S.papers[0].labels.length === 0')
                assert httpx.get(url + '/api/papers/paper').json()['labels'] == []

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_added_claim_facets_clear_on_undo_and_return_on_redo(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Facet edits'))
    store.add_facet('method')
    store.add_facet('model')
    claim = store.add_claim('paper', {'text': 'A finding'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f'{url}/#view=claims&paper=paper')
                await page.locator('.claim').wait_for()
                path = f'/api/papers/paper/claims/{claim["id"]}'
                await page.evaluate('''async path => {
                  await api(path, {method:'PATCH', headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({facets:{method:['survey']}})});
                  await refreshAll();
                }''', path)
                # An unrelated facet set by a different window must survive.
                httpx.patch(url + path, json={'facets': {'model': ['GPT']}}).raise_for_status()
                await page.keyboard.press('Meta+z')
                await page.wait_for_function('!S.claims[0].facets.method')
                stored = httpx.get(url + '/api/papers/paper').json()['claims'][0]
                assert stored['facets'] == {'model': ['GPT']}
                await page.keyboard.press('Meta+Shift+z')
                await page.wait_for_function('S.claims[0].facets.method?.[0] === "survey"')
                stored = httpx.get(url + '/api/papers/paper').json()['claims'][0]
                assert stored['facets'] == {'method': ['survey'], 'model': ['GPT']}

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('target', ['paper', 'claim'])
def test_undo_rejects_a_write_interposed_after_its_read(engine, target):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='Original'))
    claim = store.add_claim('paper', {'text': 'Original'})
    path = '/api/papers/paper' if target == 'paper' else f'/api/papers/paper/claims/{claim["id"]}'
    field = 'title' if target == 'paper' else 'text'

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(f'{url}/#view=claims&paper=paper')
                await page.locator('.claim').wait_for()
                await page.evaluate('''async ({path,field}) => {
                  await api(path, {method:'PATCH',headers:{'Content-Type':'application/json'},
                    body:JSON.stringify({[field]:'Saved'})});
                  await refreshAll();
                }''', {'path': path, 'field': field})
                interposed = False

                async def newer_write(route):
                    nonlocal interposed
                    if route.request.method == 'PATCH' and not interposed:
                        interposed = True
                        httpx.patch(url + path, json={field: 'Other window'}).raise_for_status()
                    await route.continue_()

                await page.route('**' + path, newer_write)
                await page.keyboard.press('Meta+z')
                await page.locator('#toasts .toast', has_text='changed in another window').wait_for()
                current = httpx.get(url + '/api/papers/paper').json()
                assert (current if target == 'paper' else current['claims'][0])[field] == 'Other window'
                # A rejected write must leave history retryable. Reconcile the
                # conflict explicitly, then retry the same Undo and Redo.
                httpx.patch(url + path, json={field: 'Saved'}).raise_for_status()
                await page.keyboard.press('Meta+z')
                expression = 'S.papers[0].title' if target == 'paper' else 'S.claims[0].text'
                await page.wait_for_function(expression + ' === "Original"')
                await page.keyboard.press('Meta+Shift+z')
                await page.wait_for_function(expression + ' === "Saved"')

    run_browser(scenario())
