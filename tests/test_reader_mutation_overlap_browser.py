"""Reader mutations and mark autosave share one notebook write sequence."""
import asyncio

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _answer, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('action', ['recolor', 'question', 'remove'])
def test_mark_autosave_overlaps_reader_mutation_without_losing_writing(engine, action):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('paper', title='A marked paper'))
    store.pdf_path('paper').write_bytes(minimal_pdf('A marked passage.'))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1000})
            started, release = asyncio.Event(), asyncio.Event()
            errors, responses = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: responses.append(response.status)
                    if response.url.endswith('/api/notebook') and response.request.method == 'PUT' else None)

            async def hold_first_note(route):
                payload = route.request.post_data_json if route.request.method == 'PUT' else None
                if payload and not started.is_set() and any(
                    mark['id'] == 'one' and mark['note'] == 'First autosave draft'
                    for mark in payload.get('highlights', [])
                ):
                    started.set()
                    await release.wait()
                await route.continue_()

            with _server() as url:
                notebook = httpx.get(url + '/api/notebook').json()
                notebook['highlights'] = [{
                    'id': 'one', 'paper': 'paper', 'page': 1,
                    'rects': [{'x': .1, 'y': .08, 'w': .3, 'h': .02}],
                    'text': 'A marked passage.', 'color': 'yellow', 'note': 'Saved note',
                }]
                assert httpx.put(url + '/api/notebook', json=notebook).status_code == 200
                await page.goto(url)
                await page.locator('#papers [data-paper="paper"]').click()
                mark = page.locator('[data-highlight="one"]')
                await mark.get_by_role('button', name='Edit note', exact=True).click()
                field = mark.locator('[data-highlight-note]')
                await page.route('**/api/notebook', hold_first_note)
                await field.fill('First autosave draft')
                try:
                    await asyncio.wait_for(started.wait(), timeout=5)
                    # Keep the first PUT pending while a separate reader action
                    # joins the sequence. The note remains editable meanwhile.
                    if action == 'recolor':
                        await mark.get_by_role('button', name='Color green', exact=True).click()
                    elif action == 'question':
                        await mark.locator('[data-highlight-question]').check()
                    else:
                        await mark.get_by_role('button', name='Remove', exact=True).click()
                        await _answer(page, 'Remove highlight')
                    await expect(page.locator('#research-tools')).to_have_attribute('aria-busy', 'true')
                    await expect(field).to_be_enabled()
                    await field.fill('Newer writing while the reader action waits')
                    release.set()
                    await ready(page)
                    if action == 'remove':
                        await expect(mark).to_have_count(0)
                        await expect(page.get_by_role('textbox', name='Note on removed mark', exact=True)).to_have_count(0)
                        # Flush any remaining timer before checking that the
                        # deleted mark cannot be recreated by its old editor.
                        assert await page.evaluate('ResearchTools.flush()')
                        assert httpx.get(url + '/api/notebook').json()['highlights'] == []
                    else:
                        await expect(field).to_have_value('Newer writing while the reader action waits')
                        await mark.get_by_role('button', name='Done', exact=True).click()
                        await expect(field).to_have_count(0)
                        saved = httpx.get(url + '/api/notebook').json()['highlights'][0]
                        assert saved['note'] == 'Newer writing while the reader action waits'
                        if action == 'recolor':
                            assert saved['color'] == 'green'
                        else:
                            assert saved['question'] is True
                    assert responses and all(status == 200 for status in responses)
                    assert not errors
                finally:
                    release.set()

    run_browser(scenario())
