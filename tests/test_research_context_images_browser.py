"""Research context image pastes survive saves and asynchronous navigation."""
import asyncio
import base64
import io

import pytest
from PIL import Image
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_ask_browser import _server


def image_data():
    output = io.BytesIO()
    Image.new('RGB', (12, 8), 'blue').save(output, format='PNG')
    return base64.b64encode(output.getvalue()).decode()


async def paste_image(page):
    await page.locator('#research-context').evaluate('''(field, data) => {
        const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
        const clipboard = new DataTransfer();
        clipboard.items.add(new File([bytes], 'diagram.png', {type:'image/png'}));
        field.dispatchEvent(new ClipboardEvent('paste', {bubbles:true, cancelable:true, clipboardData:clipboard}));
    }''', image_data())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_context_image_paste_save_reload_and_remove(engine):
    config.set_ai_enabled(False)

    async def scenario():
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
@pytest.mark.parametrize('fail', [False, True])
def test_context_image_upload_navigation_and_failure(fail):
    config.set_ai_enabled(False)

    async def scenario():
        async with browser_session() as browser:
            page = await browser.new_page()
            started, release = asyncio.Event(), asyncio.Event()

            async def upload(route):
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
