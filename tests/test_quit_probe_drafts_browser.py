"""A held native health probe is separate from the final writing flush."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models, open_note, seed


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('editor', ['paper', 'dataset'])
@pytest.mark.parametrize('finish', ['original-one-flush', 'final-save', 'failed-final-save'])
def test_typing_during_quit_probe_needs_a_final_flush(engine, editor, finish):
    config.set_ai_enabled(False)
    if editor == 'paper':
        seed()

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()
            writing, finish_write = asyncio.Event(), asyncio.Event()

            async def probe(route):
                started.set()
                await release.wait()
                await route.fulfill(status=200, json={'jobs': 0})

            async def refuse(route):
                if route.request.method in ('PUT', 'PATCH'):
                    writing.set()
                    await finish_write.wait()
                    if finish == 'failed-final-save':
                        await route.fulfill(status=503, json={'detail': 'Synthetic final save failure'})
                    else:
                        await route.continue_()
                else:
                    await route.continue_()

            with _server() as url:
                if editor == 'paper':
                    await open_note(page, url)
                    field, path, key = 'textarea[data-note="paper"]', '/api/papers/paper', 'notes'
                elif editor == 'dataset':
                    item = httpx.post(url + '/api/datasets', json={'name': 'Synthetic quit control'}).json()
                    await page.goto(url + '/?dataset=' + item['id'])
                    field, path, key = '#dataset-description', '/api/datasets/' + item['id'], 'description'
                else:
                    item = httpx.post(url + '/api/research-notes', json={'title': 'Synthetic quit control'}).json()
                    await page.goto(url + '/?note=' + item['id'])
                    field, path, key = '#note-body', '/api/research-notes/' + item['id'], 'body'
                await expect(page.locator(field)).to_be_visible()
                await page.locator(field).fill('Saved before probe')
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                # Freeze only debounce/poll timers, never change saver behavior.
                # This makes the original one-flush negative control deterministic.
                await page.clock.install(time=1000000)
                await page.clock.pause_at(1000001)
                await page.route('**/api/health?quit-probe=1', probe)
                native = Path('native/Sources/WebWindow.swift').read_text()
                save_js = native.split('webView.callAsyncJavaScript(', 1)[1].split(chr(34) * 3)[1]
                await page.evaluate('() => { window.nativeQuitSave = async freezeEditing => { const report = await (async () => {' + save_js + '})(); return report.saved; }; }')
                await page.evaluate('''final => {
                  window.quitProbeReply = null;
                  void (async () => {
                    await window.doxographSaveDrafts();
                    await fetch('/api/health?quit-probe=1');
                    window.quitProbeReply = final ? await window.nativeQuitSave(true) : true;
                  })();
                }''', finish != 'original-one-flush')
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    await page.locator(field).fill('Typed while probe waited')
                    if finish != 'original-one-flush':
                        await page.route('**' + (path + '/notes' if editor == 'workspace' else path), refuse)
                    release.set()
                    if finish != 'original-one-flush':
                        await asyncio.wait_for(writing.wait(), 5)
                        assert await page.evaluate('document.body.inert') is True
                        await page.keyboard.type('Blocked during final save')
                        await expect(page.locator(field)).to_have_value('Typed while probe waited')
                        finish_write.set()
                    await page.wait_for_function('window.quitProbeReply !== null')
                    assert await page.evaluate('window.quitProbeReply') is (finish != 'failed-final-save')
                    if editor in ('dataset'):
                        collection = '/api/datasets' if editor == 'dataset' else '/api/research-notes'
                        records = httpx.get(url + collection).json()['datasets' if editor == 'dataset' else 'notes']
                        stored = next(r for r in records if r['id'] == item['id'])[key]
                    else:
                        stored = httpx.get(url + path).json()[key]
                    assert stored == ('Typed while probe waited' if finish == 'final-save' else 'Saved before probe')
                    await expect(page.locator(field)).to_have_value('Typed while probe waited')
                    assert await page.evaluate('document.body.inert') is (finish != 'original-one-flush')
                    if finish == 'failed-final-save':
                        resume_js = native.split('func resumeEditingAfterCancelledQuit()', 1)[1].split(chr(34) * 3)[1]
                        await page.evaluate('() => {' + resume_js + '}')
                        assert await page.evaluate('document.body.inert') is False
                        await page.locator(field).fill('Editable after Cancel')
                        await page.unroute('**' + (path + '/notes' if editor == 'workspace' else path), refuse)
                        assert await page.evaluate('window.doxographSaveDrafts()') is True
                finally:
                    release.set()
                    finish_write.set()

    run_browser(scenario())
