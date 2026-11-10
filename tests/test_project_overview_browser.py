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
                await page.locator('#save').click()
                await page.get_by_text('All changes saved.',exact=True).wait_for()
                await page.reload()
                await page.locator('.region').click()
                assert await page.locator('#section-title').input_value() == 'Attack prompt'
                assert 'My **attack prompt** notes' in await page.locator('#notes').input_value()
                await page.locator('#preview img').wait_for()
                assert not errors
    run_browser(scenario())
