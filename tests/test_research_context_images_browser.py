"""Research context image pastes survive saves and asynchronous navigation."""
import asyncio
import base64
import io

import pytest
from PIL import Image
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


def image_data():
    """Encode a small valid image for browser clipboard events."""
    output = io.BytesIO()
    Image.new('RGB', (12, 8), 'blue').save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode()


async def paste_image(page):
    """Dispatch an image paste at the research editor's current selection."""
    await page.locator('#research-context').evaluate('''(field, data) => {
        const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
        const clipboard = new DataTransfer();
        clipboard.items.add(new File([bytes], 'diagram.png', {type:'image/png'}));
        field.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:clipboard}));
    }''', image_data())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_context_image_paste_save_reload_and_remove(engine):
    """Images round-trip with context while unsafe preview content is excluded."""
    config.set_ai_enabled(False)

    async def scenario():
        """Exercise insertion, persistence, removal, and preview sanitization."""
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#btn-refresh-library')).to_be_enabled()
                await page.locator('#btn-workspace').click()
                field = page.locator('#research-context')
                await field.fill('Before After')
                await field.evaluate('field => field.setSelectionRange(7, 7)')
                await paste_image(page)
                await expect(page.locator('#research-image-status')).to_contain_text('Image added')
                value = await field.input_value()
                assert value.startswith('Before \n\n![Pasted image](/api/research-notes/images/')
                assert value.endswith('\n\nAfter')
                preview = page.locator('#research-context-preview img')
                await expect(preview).to_have_count(1)
                await page.wait_for_function("document.querySelector('#research-context-preview img').naturalWidth === 12")
                await page.locator('#research-form button[type=submit]').click()
                await expect(field).to_have_count(0)
                await page.reload()
                await expect(page.locator('#btn-refresh-library')).to_be_enabled()
                await page.locator('#btn-workspace').click()
                await expect(field).to_have_value(value)
                await page.wait_for_function("document.querySelector('#research-context-preview img').naturalWidth === 12")
                await field.fill('Just text now')
                await expect(preview).to_have_count(0)
                # Arbitrary remote images must not cause network requests.
                await field.fill('![remote](https://example.com/tracker.png)<script>alert(1)</script>')
                await expect(preview).to_have_count(0)
                await expect(page.locator('#research-context-preview script')).to_have_count(0)
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('return_before_upload', [False, True])
def test_context_image_upload_keeps_newer_context_and_claims(return_before_upload):
    """Completing an upload after navigation cannot replace newer server edits."""
    config.set_ai_enabled(False)
    store.save_context('Original context')
    store.save_ledger([{'id': 'L1', 'text': 'Original claim'}])

    async def scenario():
        """Hold the upload while another writer changes both research fields."""
        async with browser_session() as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def upload(route):
                """Release the real upload only after the navigation race is set up."""
                started.set()
                await release.wait()
                await route.fulfill(response=await route.fetch())

            await page.route('**/api/research-notes/images', upload)
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#btn-refresh-library')).to_be_enabled()
                await page.locator('#btn-workspace').click()
                # Select old text: this range must not replace newer words.
                await page.locator('#research-context').evaluate('field => field.setSelectionRange(0, 8)')
                await paste_image(page)
                await asyncio.wait_for(started.wait(), 5)
                await page.locator('#btn-papers').click()
                store.save_context('Newer context from another window')
                latest_claims = [{'id': 'L2', 'text': 'Newer claim'}]
                store.save_ledger(latest_claims)
                await page.evaluate('pollOnce()')
                if return_before_upload:
                    await page.locator('#btn-workspace').click()
                    await expect(page.locator('#research-context')).to_have_value('Newer context from another window')
                release.set()
                await page.wait_for_function('!V.researchSaving')
                if not return_before_upload:
                    await page.locator('#btn-workspace').click()
                assert (await page.locator('#research-context').input_value()).startswith('Newer context from another window')
                await expect(page.locator('[name="ledger-id"]')).to_have_value('L2')
                await page.locator('#research-form button[type=submit]').click()
                await expect(page.locator('#research-context')).to_have_count(0)
                assert store.load_context().startswith('Newer context from another window\n\n![Pasted image](')
                assert store.load_ledger() == latest_claims

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('fail', [False, True])
def test_context_image_upload_navigation_and_failure(fail):
    """Navigation preserves text and restores saving after success or failure."""
    config.set_ai_enabled(False)

    async def scenario():
        """Navigate away during a controlled successful or rejected upload."""
        async with browser_session() as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def upload(route):
                """Delay the upload response until the editor has been left."""
                started.set()
                await release.wait()
                if fail:
                    await route.fulfill(status=413, json={'detail': 'Choose an image smaller than 10 MB.'})
                else:
                    await route.fulfill(response=await route.fetch())

            await page.route('**/api/research-notes/images', upload)
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#btn-refresh-library')).to_be_enabled()
                await page.locator('#btn-workspace').click()
                await page.locator('#research-context').fill('Keep this draft')
                await paste_image(page)
                await asyncio.wait_for(started.wait(), 5)
                await expect(page.locator('#research-form button[type=submit]')).to_be_disabled()
                await page.locator('#btn-papers').click()
                release.set()
                await page.wait_for_function('!V.researchSaving')
                await page.locator('#btn-workspace').click()
                value = await page.locator('#research-context').input_value()
                assert 'Keep this draft' in value
                if fail:
                    assert value == 'Keep this draft'
                    await expect(page.locator('#toasts')).to_contain_text('smaller than 10 MB')
                else:
                    await page.wait_for_function("document.querySelector('#research-context-preview img').naturalWidth === 12")
                await expect(page.locator('#research-form button[type=submit]')).to_be_enabled()

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('edited', [False, True])
def test_context_image_upload_keeps_polled_context_in_open_editor(edited):
    """A held untouched field adopts polled text; actual local edits take priority."""
    config.set_ai_enabled(False)
    store.save_context('Original context')
    store.save_ledger([{'id': 'L1', 'text': 'Original claim'}])

    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def upload(route):
                started.set()
                await release.wait()
                await route.fulfill(response=await route.fetch())

            await page.route('**/api/research-notes/images', upload)
            with _server() as url:
                await page.goto(url)
                await expect(page.locator('#btn-refresh-library')).to_be_enabled()
                await page.locator('#btn-workspace').click()
                field = page.locator('#research-context')
                if edited:
                    await field.fill('My local edits')
                await field.evaluate('field => field.setSelectionRange(field.value.length, field.value.length)')
                await paste_image(page)
                await asyncio.wait_for(started.wait(), 5)
                store.save_context('Updated elsewhere')
                claims = [{'id': 'L2', 'text': 'Updated claim'}]
                store.save_ledger(claims)
                await page.evaluate('pollOnce()')
                await expect(field).to_have_value('My local edits' if edited else 'Original context')
                release.set()
                await page.wait_for_function('!V.researchSaving')
                prefix = 'My local edits' if edited else 'Updated elsewhere'
                assert (await field.input_value()).startswith(prefix + '\n\n![Pasted image](')
                await page.locator('#research-form button[type=submit]').click()
                await expect(field).to_have_count(0)
                assert store.load_context().startswith(prefix + '\n\n![Pasted image](')
                assert store.load_ledger() == claims

    run_browser(scenario())
