"""Page PDF admission, transfer settlement, and the subsequent quit health snapshot."""
import asyncio
from pathlib import Path

import pytest
from playwright.async_api import expect
from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server
from test_paper_note_revert_browser import no_models as no_models


def native_contract():
    source = Path('native/Sources/WebWindow.swift').read_text()
    save = source.split('webView.callAsyncJavaScript(', 1)[1].split(chr(34)*3)[1]
    resume = source.split('func resumeEditingAfterCancelledQuit()', 1)[1].split(chr(34)*3)[1]
    delegate = Path('native/Sources/AppDelegate.swift').read_text()
    initial = delegate.split('func applicationShouldTerminate(', 1)[1].split('private func probeBeforeQuitting(', 1)[0]
    return save, resume, 'window.saveDrafts(freezeEditing: true)' in initial


async def drop_pdf(page):
    transfer = await page.evaluate_handle('''() => {
      const data = new DataTransfer();
      data.items.add(new File(['%PDF-synthetic-never-parsed'], 'synthetic.pdf', {type:'application/pdf'}));
      return data;
    }''')
    await page.locator('body').dispatch_event('drop', {'dataTransfer':transfer})
    await transfer.dispose()


async def initialize(page, url):
    await page.goto(url)
    await expect(page.locator('#workspace-notes')).to_be_visible()
    save, resume, first_freezes = native_contract()
    await page.evaluate('() => { window.quitMockJobs = 0; window.quitHealth = null; '
                        'window.nativeQuitSave = async freezeEditing => { const report = await (async () => {' + save + '})(); return report.saved; }; }')
    return resume, first_freezes


async def begin_quit(page, freeze):
    await page.evaluate('''freeze => {
      window.quitReady = null;
      void window.nativeQuitSave(freeze).then(saved => {
        window.quitReady = saved;
        if (saved) window.quitHealth = {jobs:window.quitMockJobs};
      });
    }''', freeze)
    # Let already-resolved editor promises drain without relying on network timing.
    await page.evaluate('async () => { await new Promise(resolve => setTimeout(resolve, 0)); }')


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('completion', ['response', 'lost-response'])
def test_admitted_pdf_settles_before_the_quit_health_snapshot(engine, completion):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
            requests = []

            async def upload(route):
                requests.append(route.request.url)
                started.set()
                await release.wait()
                await page.evaluate('window.quitMockJobs += 1')
                if completion == 'lost-response':
                    await route.abort()
                else:
                    await route.fulfill(status=200, json={'queued':1})
                finished.set()

            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/upload?*', upload)
                await drop_pdf(page)
                await asyncio.wait_for(started.wait(), 5)
                try:
                    await begin_quit(page, freeze)
                    assert await page.evaluate('window.quitHealth') is None
                    assert await page.evaluate('document.body.inert') is True
                    release.set()
                    await page.wait_for_function('window.quitReady !== null')
                    assert await page.evaluate('window.quitReady') is (completion == 'response')
                    # An uncertain transfer asks about unsaved work first. Explicit
                    # Discard still proceeds to health, which sees its created job.
                    if completion == 'lost-response':
                        await page.evaluate('window.quitHealth = {jobs:window.quitMockJobs}')
                    assert await page.evaluate('window.quitHealth.jobs') == 1
                    # A Stay decision reopens admission without changing the reference draft.
                    await page.evaluate('() => {' + resume + '}')
                    assert await page.evaluate('document.body.inert') is False
                    await expect(page.locator('#refs')).to_have_value('')
                    await drop_pdf(page)
                    await page.wait_for_function('workspaceClient.pendingMutations === 0')
                    assert len(requests) == 2
                finally:
                    release.set()
                    await asyncio.wait_for(finished.wait(), 5)

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('completion', ['held', 'queued'])
def test_pdf_after_health_cannot_bypass_the_final_freeze(engine, completion):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            release, finished = asyncio.Event(), asyncio.Event()
            requests = []

            async def upload(route):
                requests.append(route.request.url)
                await release.wait()
                await page.evaluate('window.quitMockJobs += 1')
                await route.fulfill(status=200, json={'queued':1})
                finished.set()

            with _server() as url:
                _, freeze = await initialize(page, url)
                await page.route('**/api/upload?*', upload)
                await begin_quit(page, freeze)
                await page.wait_for_function('window.quitHealth !== null')
                assert await page.evaluate('window.quitHealth.jobs') == 0
                try:
                    await drop_pdf(page)
                    pending = await page.evaluate('workspaceClient.pendingMutations')
                    if completion == 'queued':
                        release.set()
                        await page.wait_for_function('workspaceClient.pendingMutations === 0')
                    final = await page.evaluate('window.nativeQuitSave(true)')
                    if completion == 'held':
                        assert not (final and pending), 'A clean quit cannot leave an unreported page transfer'
                    else:
                        assert not final or await page.evaluate('window.quitHealth.jobs >= window.quitMockJobs'), 'A clean quit cannot miss the queued reading job'
                    assert requests == []
                finally:
                    release.set()
                    if requests:
                        await asyncio.wait_for(finished.wait(), 5)

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_queued_reference_callback_cannot_admit_work_while_frozen(engine):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            requests = []

            async def ingest(route):
                requests.append(route.request.post_data_json)
                await route.fulfill(status=200, json={'queued':1,'known':[],'unknown':[]})

            with _server() as url:
                resume, freeze = await initialize(page, url)
                await page.route('**/api/ingest', ingest)
                await page.locator('#refs').fill('Keep this reference draft')
                await begin_quit(page, freeze)
                await page.wait_for_function('window.quitReady !== null')
                assert await page.evaluate('window.quitReady') is False
                # Model a callback queued before the freeze; DOM inert alone cannot stop it.
                await page.evaluate('addReferences()')
                assert requests == []
                await expect(page.locator('#refs')).to_have_value('Keep this reference draft')
                await page.evaluate('() => {' + resume + '}')
                await page.evaluate('addReferences()')
                assert len(requests) == 1

    run_browser(scenario())
