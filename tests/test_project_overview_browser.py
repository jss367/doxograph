import pytest
from browser_support import browser_session, run_browser
from test_browser import _server
from test_research_notes import image_bytes


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_diagram_notes_survive_reload(engine):
    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width':1400,'height':1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url+'/project?workspace=default')
                await page.wait_for_function('!document.querySelector("#upload").disabled')
                await page.locator('#upload').set_input_files({'name':'Diagram.png','mimeType':'image/png','buffer':image_bytes()})
                await page.get_by_text('Diagram saved.', exact=False).wait_for()
                await page.locator('#draw').click()
                box = await page.locator('#canvas').bounding_box()
                await page.mouse.move(box['x']+box['width']*.1,box['y']+box['height']*.1)
                await page.mouse.down()
                await page.mouse.move(box['x']+box['width']*.4,box['y']+box['height']*.4)
                await page.mouse.up()
                await page.locator('#section-title').fill('Attack prompt')
                await page.locator('#notes').fill('My **attack prompt** notes')
                await page.locator('#picture').set_input_files({'name':'note.png','mimeType':'image/png','buffer':image_bytes()})
                await page.wait_for_function('!document.querySelector("#picture").disabled')
                assert not errors, errors
                assert await page.locator('#preview img').count(), (await page.locator('#status').inner_text(), await page.locator('#notes').input_value(), await page.locator('#preview').inner_html())
                await page.locator('#save-status').get_by_text('Saved', exact=True).wait_for()
                # Saving replaces the project object, but the still-open
                # section editor must keep editing its current boundaries.
                await page.get_by_label('Left (%)', exact=True).fill('20')
                await page.get_by_label('Left (%)', exact=True).press('Tab')
                await page.locator('#save-status').get_by_text('Saved', exact=True).wait_for()
                await page.reload()
                await page.locator('.region').click()
                assert await page.get_by_label('Left (%)', exact=True).input_value() == '20.0'
                assert await page.locator('#section-title').input_value() == 'Attack prompt'
                assert 'My **attack prompt** notes' in await page.locator('#notes').input_value()
                await page.locator('#preview img').wait_for()
                assert not errors
    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_diagram_autosave_keeps_newer_text_and_failed_writes(engine):
    import asyncio
    import httpx
    from playwright.async_api import expect

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                image = httpx.post(url + '/api/research-notes/images', files={'file': ('diagram.png', image_bytes(), 'image/png')}).json()['url'].split('/')[-1].split('?')[0]
                project = httpx.post(url + '/api/research-notes', json={'title': 'Diagram', 'body': '', 'project': {
                    'image': image, 'sections': [{'title': 'Section', 'body': '', 'x': .1, 'y': .1, 'width': .3, 'height': .3}],
                }}).json()
                await page.goto(url + '/project')
                await page.locator('.region').click()
                started, release = asyncio.Event(), asyncio.Event()
                writes = []

                async def hold(route):
                    writes.append(route.request.post_data_json)
                    if len(writes) == 1:
                        started.set()
                        await release.wait()
                    await route.continue_()

                path = '**/api/research-notes/' + project['id'] + '?*'
                await page.route(path, hold)
                await page.locator('#notes').fill('First text')
                await asyncio.wait_for(started.wait(), 5)
                await page.locator('#notes').fill('Newer text while saving')
                release.set()
                await expect(page.locator('#save-status')).to_have_text('Saved')
                assert len(writes) == 2
                await page.unroute(path, hold)

                async def refuse(route):
                    await route.fulfill(status=409, json={'detail': 'Changed in another window'})

                await page.route(path, refuse)
                await page.locator('#notes').fill('Keep this failed edit')
                await expect(page.locator('#save-status')).to_contain_text('Couldn’t save')
                assert await page.evaluate('window.doxographSaveDrafts()') is False
                await expect(page.locator('#notes')).to_have_value('Keep this failed edit')
                await page.unroute(path, refuse)
                assert await page.evaluate('window.doxographSaveDrafts()') is True
                await page.reload()
                await page.locator('.region').click()
                await expect(page.locator('#notes')).to_have_value('Keep this failed edit')
    run_browser(asyncio.wait_for(scenario(), 25))
