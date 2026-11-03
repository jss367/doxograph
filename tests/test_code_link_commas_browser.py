"""Valid commas belong to a URL, never to the editor's separator grammar."""
import asyncio

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from test_ask_browser import _server


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('links', [
    ['https://example.org/Repo?ref=A,B'],
    ['https://example.org/Owner,Repo'],
    ['https://User,Name:Se,cret@example.org/Repo'],
    ['https://example.org/Repo?ref=A,B', 'https://example.org/Other,Repo'],
], ids=['query', 'path', 'credentials', 'multiple'])
def test_code_editor_roundtrips_comma_urls_with_whitespace_separators(engine, links):
    config.set_ai_enabled(False)
    paper = store.new_paper('synthetic', title='Synthetic comma URL')
    paper['code'] = links
    store.save_paper(paper)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            requested = asyncio.Event()
            payloads = []

            async def record(route):
                if route.request.method == 'PATCH':
                    payloads.append(route.request.post_data_json['code'])
                    requested.set()
                await route.continue_()

            with _server() as url:
                await page.goto(url + '/#paper=synthetic')
                await page.route('**/api/papers/synthetic', record)
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Code links', exact=True).click()
                await expect(page.locator('#ask-input')).to_have_value('\n'.join(links))
                await page.locator('#ask-input').fill('  ' + '\n'.join(links) + '  ')
                await page.locator('#ask').get_by_role('button', name='OK', exact=True).click()
                await asyncio.wait_for(requested.wait(), 5)
                assert payloads == [links]
                await expect(page.locator('#ask')).not_to_be_visible()
                assert store.load_paper('synthetic')['code'] == links
                await expect(page.locator('.paper-code a')).to_have_count(len(links))

    run_browser(scenario())


@pytest.mark.parametrize('link', [
    'https://example.org/Repo?ref=A,B',
    'https://example.org/Owner,Repo',
    'https://User,Name:Se,cret@example.org/Repo',
])
def test_comma_urls_roundtrip_the_offline_paper_api(link):
    from fastapi.testclient import TestClient
    from doxograph import server

    store.save_paper(store.new_paper('synthetic', title='Synthetic comma URL'))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.patch('/api/papers/synthetic', json={'code': [link]})
        assert response.status_code == 200
        assert response.json()['code'] == [link]
    assert store.load_paper('synthetic')['code'] == [link]


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('entry', ['paste', 'enter-key'])
def test_multiline_code_editor_accepts_paste_and_enter_without_submitting(engine, entry):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('synthetic', title='Synthetic multiline URL'))
    links = ['https://example.org/Repo?ref=A,B', 'https://example.org/Other,Repo']

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url + '/#paper=synthetic')
                await page.locator('.paper-actions > summary').click()
                await page.get_by_role('button', name='Code links', exact=True).click()
                field = page.locator('#ask-input')
                assert await field.evaluate('node => node.tagName') == 'TEXTAREA'
                if entry == 'paste':
                    await field.focus()
                    await page.keyboard.insert_text('\n'.join(links))
                else:
                    await field.fill(links[0])
                    await field.press('End')
                    await field.press('Enter')
                    await page.keyboard.insert_text(links[1])
                await expect(field).to_have_value('\n'.join(links))
                await expect(page.locator('#ask')).to_be_visible()
                assert store.load_paper('synthetic')['code'] == []
                await page.locator('#ask').get_by_role('button', name='OK', exact=True).click()
                await expect(page.locator('#ask')).not_to_be_visible()
                assert store.load_paper('synthetic')['code'] == links
                # A later ordinary prompt keeps single-line Enter submission.
                await page.evaluate('void askDialog({title:"Synthetic ordinary prompt",input:{value:""}})')
                assert await field.evaluate('node => node.tagName') == 'INPUT'
                await field.fill('Ordinary prompt')
                await field.press('Enter')
                await expect(page.locator('#ask')).not_to_be_visible()

    run_browser(scenario())
