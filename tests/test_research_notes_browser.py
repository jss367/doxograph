"""Exercise the offline writing workflow in the browser and native WebKit engine."""
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _server, _answer
from test_research_notes import image_bytes


async def ready(page):
    await page.wait_for_function("document.querySelector('#research-notes').getAttribute('aria-busy') !== 'true'")


async def settled(page):
    await page.wait_for_function("document.querySelector('#notes-status').textContent === 'Saved'")


@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_same_note_image_can_be_retried_and_inserted_twice(engine, tmp_path):
    config.set_ai_enabled(False)
    image = tmp_path / 'figure.png'
    image.write_bytes(image_bytes())

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            attempts = 0

            async def upload(route):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    await route.fulfill(status=503, json={'detail':'Upload unavailable. Try again.'})
                else:
                    await route.continue_()

            await page.route('**/api/research-notes/images', upload)
            with _server() as url:
                note = httpx.post(url+'/api/research-notes', json={'title':'Repeated figures'}).json()
                await page.goto(url)
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                dialog = page.locator('#research-notes')
                for attempt in range(1, 4):
                    async with page.expect_file_chooser() as chooser:
                        await dialog.get_by_role('button',name='Image',exact=True).click()
                    await (await chooser.value).set_files(str(image))
                    if attempt == 1:
                        await page.get_by_text('Upload unavailable. Try again.',exact=True).wait_for()
                        assert await page.locator('#note-preview img').count() == 0
                    else:
                        await page.wait_for_function(
                            "count => document.querySelectorAll('#note-preview img').length === count",
                            arg=attempt - 1, timeout=5000)
                    await ready(page)
                    assert attempts == attempt
                await settled(page)
                saved = httpx.get(url+'/api/research-notes').json()['notes'][0]
                assert saved['id'] == note['id']
                assert saved['body'].count('![figure.png](') == 2

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_rich_research_notes(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper',title='A study of recovery',authors=['Ada Researcher'],year=2026))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1600,'height':1100})
            errors, external = [], []
            page.on('pageerror',lambda error: errors.append(str(error)))
            page.on('request',lambda req: external.append(req.url) if not req.url.startswith(('http://127.0.0.1:', 'blob:','data:')) else None)
            with _server() as url:
                await page.goto(url)
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                dialog = page.locator('#research-notes')
                await dialog.get_by_role('button',name='New note',exact=True).click()
                await _answer(page,'Create note','Why recovery differs')
                await ready(page)
                first_id = httpx.get(url+'/api/research-notes').json()['notes'][0]['id']
                content = r'''## Working hypothesis

Recovery depends on the task and the intervention, rather than model size alone.

Inline math: $p(y \mid x) = \frac{e^{s_y}}{\sum_j e^{s_j}}$.

$$
\Delta = \frac{1}{n}\sum_{i=1}^{n}(r_i-b_i)
$$

| Condition | Recovery |
| --- | --- |
| Baseline | 0.46 |
| Intervention | 0.71 |

> Compare these conditions before drawing a conclusion.

- Check task differences
- Replicate the intervention

<script>window.noteUnsafe = true</script>

[Unsafe](javascript:alert(1))

![Remote](https://example.invalid/tracking.png)
'''
                await page.locator('#note-body').fill(content)
                await page.locator('#note-preview table').wait_for()
                assert await page.locator('#note-preview .katex').count() == 2
                assert await page.locator('#note-preview .katex-display').count() == 1
                assert await page.evaluate('window.noteUnsafe') is None
                assert await page.locator('#note-preview [href^="javascript:"]').count() == 0
                assert await page.locator('#note-preview img').count() == 0
                async with page.expect_file_chooser() as chooser:
                    await dialog.get_by_role('button',name='Image',exact=True).click()
                await (await chooser.value).set_files({'name':'recovery.png','mimeType':'image/png','buffer':image_bytes()})
                await ready(page)
                await page.wait_for_function("document.querySelector('#note-preview img')?.complete")
                assert await page.locator('#note-preview img').evaluate('(img) => img.naturalWidth') == 80
                await dialog.get_by_role('button',name='Link note or paper',exact=True).click()
                await ready(page)
                await page.locator('#note-link-results').get_by_role('button',name='Paper A study of recovery').click()
                await ready(page)
                await settled(page)
                saved_body = httpx.get(url+'/api/research-notes').json()['notes'][0]['body']
                await dialog.get_by_role('button',name='New note',exact=True).click()
                await _answer(page,'Create note','Intervention design')
                await ready(page)
                await page.locator('#note-body').fill('## Next experiment\n\nTest whether the task explains the difference.\n\n')
                await dialog.get_by_role('button',name='Link note or paper',exact=True).click()
                await ready(page)
                await page.locator('#note-link-results').get_by_role('button',name='Note Why recovery differs').click()
                await ready(page)
                await settled(page)
                await page.locator('#note-preview a').click()
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Why recovery differs'
                assert await page.locator('#note-backlinks').get_by_role('button',name='Intervention design',exact=True).count() == 1
                # Moving to another note saves the one being written, at once.
                await page.locator('#note-title').fill('Saved by leaving')
                await page.locator('#notes-list').get_by_role('button',name='Intervention design').click()
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Intervention design'
                first = next(n for n in httpx.get(url+'/api/research-notes').json()['notes'] if n['id'] == first_id)
                assert first['title'] == 'Saved by leaving'
                await page.locator('#notes-list').get_by_role('button',name='Saved by leaving').click()
                await ready(page)
                # A second writer changes the same note; the draft must stay intact.
                httpx.put(url+'/api/research-notes/'+first_id,json={'title':'Another window title','body':'Another window text','revision':first['revision']}).raise_for_status()
                await page.locator('#note-title').fill('My unsaved argument')
                await page.wait_for_function("document.querySelector('#notes-status').textContent.includes('another window')")
                assert await page.locator('#notes-status').get_attribute('class') == 'warn'
                assert await page.locator('#note-title').input_value() == 'My unsaved argument'
                # A draft that could not save survives canceled note navigation.
                await page.locator('#notes-list').get_by_role('button',name='Intervention design').click()
                await _answer(page,'Cancel')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'My unsaved argument'
                async with page.expect_download() as download:
                    await dialog.get_by_role('button',name='Export Markdown',exact=True).click()
                downloaded = await download.value
                assert downloaded.suggested_filename == 'My-unsaved-argument.md'
                exported = Path(await downloaded.path()).read_text()
                assert exported.startswith('# My unsaved argument\n\n')
                assert ']('+url+'/api/research-notes/images/' in exported
                assert ']('+url+'/#ws=default&paper=paper)' in exported
                await ready(page)
                await dialog.get_by_role('button',name='Reload saved version',exact=True).click()
                await ready(page)
                assert 'Another window text' in await page.locator('.note-saved-comparison').inner_text()
                assert await page.locator('#note-title').input_value() == 'My unsaved argument'
                await dialog.get_by_role('button',name='Replace draft with saved version').click()
                await _answer(page,'Replace draft')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Another window title'
                # Restore the richer content and take a review screenshot.
                await page.locator('#note-title').fill('Why recovery differs')
                rich_body = saved_body.replace('<script>window.noteUnsafe = true</script>','').replace('[Unsafe](javascript:alert(1))','').replace('![Remote](https://example.invalid/tracking.png)','')
                await page.locator('#note-body').fill(rich_body)
                await settled(page)
                await page.locator('#note-body').evaluate('(field) => field.scrollTop = 0')
                await page.wait_for_function('document.fonts.status === "loaded"')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/research-notes.png')
                # Deep links restore the note after reload.
                await page.reload()
                await dialog.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Why recovery differs'
                await page.locator('#notes-search').fill('Intervention design')
                assert await page.locator('#notes-list button').count() == 1
                await page.locator('#note-preview a').get_by_text('A study of recovery',exact=True).click()
                await dialog.wait_for(state='hidden')
                assert 'paper=paper' in page.url
                # Deleting a destination leaves other notes' writing and links intact.
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                await dialog.get_by_role('button',name='Delete note',exact=True).click()
                await _answer(page,'Delete note')
                await ready(page)
                await page.locator('#notes-list').get_by_role('button',name='Intervention design').click()
                await ready(page)
                assert await page.locator('#note-preview .note-missing-link').count() == 1
                assert not errors and not external

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_note_workspace_navigation_and_writing_tools(engine):
    config.set_ai_enabled(False)
    workspace = config.create_workspace('Replication studies')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1280,'height':900})
            with _server() as url:
                await page.goto(url+'/#ws='+workspace['id'])
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                dialog = page.locator('#research-notes')
                await dialog.get_by_role('button',name='New note',exact=True).click()
                await _answer(page,'Create note','Replication protocol')
                await ready(page)
                await dialog.get_by_role('button',name='Equation',exact=True).click()
                await ready(page)
                assert await page.locator('#note-preview .katex-display').count() == 1
                await dialog.get_by_role('button',name='Table',exact=True).click()
                await ready(page)
                assert await page.locator('#note-preview table').count() == 1
                await page.locator('#note-view').select_option('preview')
                assert not await page.locator('#note-body').is_visible()
                # A toolbar insertion returns to the editor even from Preview.
                await dialog.get_by_role('button',name='Heading',exact=True).click()
                await ready(page)
                assert await page.locator('#note-body').is_visible()
                # The shortcut saves without waiting for the pause in typing.
                await page.locator('#note-body').press('Control+s')
                await settled(page)
                assert '## Heading' in httpx.get(url+'/api/research-notes',headers={'X-Doxograph-Workspace':workspace['id']}).json()['notes'][0]['body']
                await page.locator('#note-image').set_input_files({'name':'figure.png','mimeType':'image/png','buffer':image_bytes()})
                await ready(page)
                assert 'workspace='+workspace['id'] in await page.locator('#note-body').input_value()
                await settled(page)
                await page.reload()
                await dialog.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Replication protocol'
                await page.wait_for_function("document.querySelector('#note-preview img')?.complete")
                assert await page.locator('#note-preview img').evaluate('(img) => img.naturalWidth') == 80
                # Switching workspaces mid-sentence saves the sentence first.
                # Workspace navigation can also arrive from browser history.
                await page.locator('#note-title').fill('Retitled protocol')
                await page.evaluate("void switchWorkspace('default')")
                await dialog.wait_for(state='hidden')
                await page.wait_for_function("workspaceClient.workspaceId === 'default'")
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                assert await page.locator('#notes-list button').count() == 0
                assert httpx.get(url+'/api/research-notes',headers={'X-Doxograph-Workspace':workspace['id']}).json()['notes'][0]['title'] == 'Retitled protocol'

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_notes_view_keeps_the_library_usable_and_the_draft_across_navigation(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study',title='Evaluation study'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':900})
            errors = []
            page.on('pageerror',lambda error: errors.append(str(error)))
            with _server() as url:
                note = httpx.post(url+'/api/research-notes', json={'title':'Working argument'}).json()
                # Older links, including the ones written into notes, name the note in the query.
                await page.goto(url+'/?note='+note['id'])
                view = page.locator('#research-notes')
                await view.wait_for(state='visible')
                await ready(page)
                assert '?note=' not in page.url and 'view=notes' in page.url and 'note='+note['id'] in page.url
                assert await page.locator('#btn-research-notes').get_attribute('aria-current') == 'page'
                assert await page.locator('#side').is_visible()
                assert not await page.locator('#panes').is_visible()
                assert await page.locator('dialog[open]').count() == 0
                # Leaving for a paper saves without waiting for the pause in typing.
                await page.locator('#note-body').fill('A paragraph in progress.')
                await page.locator('#papers').get_by_text('Evaluation study').click()
                await view.wait_for(state='hidden')
                assert 'paper=study' in page.url
                await page.wait_for_function("!ResearchNotes.hasUnsavedChanges()")
                assert httpx.get(url+'/api/research-notes').json()['notes'][0]['body'] == 'A paragraph in progress.'
                await page.go_back()
                await view.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-body').input_value() == 'A paragraph in progress.'
                assert await page.locator('#notes-status').inner_text() == 'Saved'
                await page.locator('#note-body').fill('An unsaved paragraph.')
                # The sidebar entry also returns to the note last open.
                await page.locator('#papers').get_by_text('Evaluation study').click()
                await view.wait_for(state='hidden')
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await view.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-body').input_value() == 'An unsaved paragraph.'
                await page.locator('#note-body').press('Control+s')
                await ready(page)
                assert httpx.get(url+'/api/research-notes').json()['notes'][0]['body'] == 'An unsaved paragraph.'
                # Picking a note moves the URL; a redraw that follows leaves the editor alone.
                await view.get_by_role('button',name='New note',exact=True).click()
                await _answer(page,'Create note','Second argument')
                await ready(page)
                await page.evaluate('render(); new Promise(resolve => setTimeout(resolve, 100))')
                await ready(page)
                await page.locator('#notes-list').get_by_role('button',name='Working argument').click()
                await ready(page)
                assert 'note='+note['id'] in page.url
                await page.locator('#note-body').focus()
                await page.evaluate('render(); new Promise(resolve => setTimeout(resolve, 100))')
                await ready(page)
                assert await page.evaluate('document.activeElement.id') == 'note-body'
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/research-notes-view.png')
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_a_save_whose_answer_was_lost_is_not_saved_by_restoring_the_old_text(engine):
    """The lost save may have stored its edit, so typing the old text back must
    not count as saved while the server holds a change the writer took back."""
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1440,'height':900})
            errors = []
            page.on('pageerror',lambda error: errors.append(str(error)))
            updates = []

            async def lose_first_answer(route):
                if route.request.method != 'PUT':
                    await route.continue_()
                    return
                updates.append(route.request.post_data_json)
                if len(updates) == 1:
                    # The edit is stored, but its answer never arrives.
                    await route.fetch()
                    await route.abort()
                else:
                    await route.continue_()

            with _server() as url:
                note = httpx.post(url+'/api/research-notes', json={'title':'Working argument'}).json()
                second = httpx.post(url+'/api/research-notes', json={'title':'Second argument'}).json()
                await page.route('**/api/research-notes/*', lose_first_answer)
                await page.goto(url+'/?note='+note['id'])
                view = page.locator('#research-notes')
                await view.wait_for(state='visible')
                await ready(page)
                body = page.locator('#note-body')
                await body.fill('A transient paragraph.')
                await page.wait_for_function(
                    "document.querySelector('#notes-status').textContent.startsWith('Couldn’t save')")
                stored = {n['id']: n for n in httpx.get(url+'/api/research-notes').json()['notes']}
                assert stored[note['id']]['body'] == 'A transient paragraph.'

                await body.fill('')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                assert [update['body'] for update in updates][:2] == ['A transient paragraph.', '']
                # The lost save moved the revision on, so the retry is a conflict.
                stored = {n['id']: n for n in httpx.get(url+'/api/research-notes').json()['notes']}
                assert stored[note['id']]['body'] == 'A transient paragraph.'
                assert await page.locator('#notes-status').inner_text() != 'Saved'
                assert await page.evaluate('ResearchNotes.hasUnsavedChanges()') is True
                # Opening another note asks first instead of leaving silently.
                await page.locator('#notes-list').get_by_role('button',name='Second argument').click()
                await expect(page.locator('#ask')).to_be_visible()
                await page.locator('#ask').get_by_role('button',name='Cancel',exact=True).click()
                await ready(page)
                assert 'note='+note['id'] in page.url and 'note='+second['id'] not in page.url
                assert await body.input_value() == ''
                assert not errors

    run_browser(scenario())
