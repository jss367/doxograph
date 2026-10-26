"""Exercise the offline writing workflow in the browser and native WebKit engine."""
from pathlib import Path

import httpx
import pytest

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_browser import _server, _answer
from test_research_notes import image_bytes


async def ready(page):
    await page.wait_for_function("document.querySelector('#research-notes').getAttribute('aria-busy') !== 'true'")


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
                await page.locator('#note-image').set_input_files({'name':'recovery.png','mimeType':'image/png','buffer':image_bytes()})
                await ready(page)
                await page.wait_for_function("document.querySelector('#note-preview img')?.complete")
                assert await page.locator('#note-preview img').evaluate('(img) => img.naturalWidth') == 80
                await dialog.get_by_role('button',name='Link note or paper',exact=True).click()
                await ready(page)
                await page.locator('#note-link-results').get_by_role('button',name='Paper A study of recovery').click()
                await ready(page)
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                saved_body = httpx.get(url+'/api/research-notes').json()['notes'][0]['body']
                await dialog.get_by_role('button',name='New note',exact=True).click()
                await _answer(page,'Create note','Intervention design')
                await ready(page)
                await page.locator('#note-body').fill('## Next experiment\n\nTest whether the task explains the difference.\n\n')
                await dialog.get_by_role('button',name='Link note or paper',exact=True).click()
                await ready(page)
                await page.locator('#note-link-results').get_by_role('button',name='Note Why recovery differs').click()
                await ready(page)
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                await page.locator('#note-preview a').click()
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Why recovery differs'
                assert await page.locator('#note-backlinks').get_by_role('button',name='Intervention design',exact=True).count() == 1
                # An unsaved draft survives canceled note navigation.
                await page.locator('#note-title').fill('My unsaved argument')
                await page.locator('#notes-list').get_by_role('button',name='Intervention design').click()
                await _answer(page,'Cancel')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'My unsaved argument'
                # A second writer changes the same note; the draft must stay intact.
                httpx.put(url+'/api/research-notes/'+first_id,json={'title':'Another window title','body':'Another window text','revision':2}).raise_for_status()
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                assert 'another window' in await page.locator('#notes-status').inner_text()
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
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
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
                await dialog.get_by_role('button',name='Close research notes',exact=True).click()
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
                await page.locator('#note-body').press('Control+s')
                await ready(page)
                assert 'Saved.' in await page.locator('#notes-status').inner_text()
                await page.locator('#note-image').set_input_files({'name':'figure.png','mimeType':'image/png','buffer':image_bytes()})
                await ready(page)
                assert 'workspace='+workspace['id'] in await page.locator('#note-body').input_value()
                await dialog.get_by_role('button',name='Save note',exact=True).click()
                await ready(page)
                await page.reload()
                await dialog.wait_for(state='visible')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Replication protocol'
                await page.wait_for_function("document.querySelector('#note-preview img')?.complete")
                assert await page.locator('#note-preview img').evaluate('(img) => img.naturalWidth') == 80
                await page.locator('#note-title').fill('Unsaved protocol')
                # Workspace navigation can also arrive from browser history.
                await page.evaluate("void switchWorkspace('default')")
                await _answer(page,'Cancel')
                await ready(page)
                assert await page.locator('#note-title').input_value() == 'Unsaved protocol'
                assert await page.locator('#workspace').input_value() == workspace['id']
                await page.evaluate("void switchWorkspace('default')")
                await _answer(page,'Discard changes')
                await dialog.wait_for(state='hidden')
                await page.wait_for_function("currentWorkspaceId === 'default'")
                await page.get_by_role('button',name='Research notes',exact=True).click()
                await ready(page)
                assert await page.locator('#notes-list button').count() == 0
                assert httpx.get(url+'/api/research-notes',headers={'X-Doxograph-Workspace':workspace['id']}).json()['notes'][0]['title'] == 'Replication protocol'

    run_browser(scenario())
