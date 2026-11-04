"""Ask load ordering, geometry and refreshes preserve accessible controls."""
import asyncio
import pytest
from playwright.async_api import expect
from browser_support import browser_session,run_browser
from doxograph import store
from test_ask_browser import _server,seed

@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from doxograph import agent,chat,claude_code,codex_cli,config,extract
    config.set_ai_enabled(True)
    def blocked(*args,**kwargs):
        pytest.fail('Ask lifecycle tests must not invoke providers')
    for module,name in ((agent,'answer'),(chat,'answer'),(claude_code,'_run'),(codex_cli,'_run'),(extract,'client')):
        monkeypatch.setattr(module,name,blocked)

async def open_pane(page,url):
    await page.goto(url)
    await page.locator('#btn-agent:enabled').click()
    await expect(page.locator('#agent-question')).to_be_visible()

@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('wide',[False,True],ids=['default-width','wide-pane'])
def test_jobs_stay_outside_ask_width(engine,wide):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page=await browser.new_page(viewport={'width':1800,'height':1100})
            await page.route('**/api/jobs',lambda route:route.fulfill(json={'jobs':[{
                'id':'synthetic','kind':'import','state':'fetching','label':'Synthetic active job',
                'workspace':'default','detail':''}]}))
            with _server() as url:
                await open_pane(page,url)
                if wide: await page.evaluate('()=>{wantAskWidth(850,true);fitAskPane();}')
                await expect(page.locator('#jobs .job')).to_be_visible()
                boxes=await page.evaluate('''()=>({pane:document.getElementById('library-agent').getBoundingClientRect().toJSON(),
                  job:document.querySelector('#jobs .job').getBoundingClientRect().toJSON(),
                  layoutWidth:getComputedStyle(document.getElementById('layout')).getPropertyValue('--ask-width'),
                  jobsWidth:getComputedStyle(document.getElementById('jobs')).getPropertyValue('--ask-width')})''')
                assert boxes['job']['right']<=boxes['pane']['left'],boxes
    run_browser(asyncio.wait_for(scenario(),25))

@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_focused_context_chip_survives_changed_corpus_poll(engine):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page=await browser.new_page(viewport={'width':1600,'height':1100})
            with _server() as url:
                await open_pane(page,url)
                await page.locator('#papers li[data-paper="a"]').click()
                chip=page.locator('#agent-context [data-ask-context]')
                await expect(chip).to_be_enabled()
                await chip.focus()
                await page.evaluate('window.originalChip=document.activeElement')
                store.update_claim('c',store.load_paper('c')['claims'][0]['id'],{'text':'Synthetic changed corpus'})
                await page.evaluate('pollOnce()')
                assert await page.evaluate("document.activeElement.matches('#agent-context [data-ask-context]')"), await page.evaluate('({activeId:document.activeElement.id,activeTag:document.activeElement.tagName,originalConnected:originalChip.isConnected})')
    run_browser(asyncio.wait_for(scenario(),25))

@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('late',['api','chat'])
def test_saved_open_pane_survives_script_api_order(engine,late):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page=await browser.new_page(viewport={'width':1600,'height':1100})
            await page.add_init_script("localStorage.setItem('doxograph-ask-open','1')")
            entered,release=asyncio.Event(),asyncio.Event()
            async def held(route):
                response=await route.fetch()
                entered.set()
                await release.wait()
                await route.fulfill(response=response)
            await page.route('**/agent-chat.js*' if late=='chat' else '**/api/workspaces',held)
            with _server() as url:
                await page.goto(url,wait_until='commit')
                await asyncio.wait_for(entered.wait(),5)
                try:
                    if late=='chat':
                        await page.wait_for_function('typeof libraryInitialized!=="undefined" && libraryInitialized')
                        assert await page.evaluate('typeof restoreAsk')=='undefined'
                    else: await page.wait_for_function('typeof restoreAsk==="function"')
                    release.set()
                    await page.wait_for_function('typeof restoreAsk==="function" && libraryInitialized')
                    await expect(page.locator('#library-agent')).to_be_visible()
                finally: release.set()
    run_browser(asyncio.wait_for(scenario(),25))

@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('redraw',[False,True],ids=['same-node','redrawn'])
@pytest.mark.parametrize('topic',['recovery','scaling'])
def test_repeated_finding_opener_restores_its_topic_focus(engine,redraw,topic):
    seed()
    live={r['id']:r for r in store.claim_rows()}
    ids=[next(k for k in live if k.startswith(p+'-')) for p in ('a','b')]
    store.record_tensions('recovery',[{'claims':ids,'kind':'tension','note':'Synthetic finding'}],live)
    tid=store.tension_rows()[0]['id']
    async def scenario():
        async with browser_session(engine) as browser:
            page=await browser.new_page(viewport={'width':1600,'height':1100})
            with _server() as url:
                await page.goto(url+'/#view=findings')
                await page.wait_for_function('libraryInitialized')
                await page.evaluate("()=>{S.tensions[0].topics=['recovery','scaling'];renderAll();}")
                row=page.locator(f'.ftopic[data-topic="{topic}"] .frow[data-finding="{tid}"]')
                await row.locator('summary').click()
                await row.locator('[data-ask-finding]').click()
                await expect(page.locator('#agent-question')).to_be_focused()
                if redraw: await page.evaluate('renderAll()')
                await page.locator('#agent-question').focus()
                await page.keyboard.press('Escape')
                active=await page.evaluate("({finding:document.activeElement.dataset.askFinding||null,topic:document.activeElement.closest('.ftopic')?.dataset.topic||null,id:document.activeElement.id})")
                assert active['finding']==tid and active['topic']==topic,active
    run_browser(asyncio.wait_for(scenario(),25))

@pytest.mark.browser
@pytest.mark.parametrize('engine',['chromium','webkit'])
@pytest.mark.parametrize('clamped',[False,True],ids=['unclamped','clamped'])
def test_keyboard_nudge_moves_from_displayed_width(engine,clamped):
    seed()
    async def scenario():
        async with browser_session(engine) as browser:
            page=await browser.new_page(viewport={'width':1600,'height':1100})
            with _server() as url:
                await open_pane(page,url)
                await page.evaluate('px=>{wantAskWidth(px,true);fitAskPane();}',800 if clamped else 400)
                if clamped: await page.set_viewport_size({'width':1400,'height':1100})
                handle=page.locator('#ask-resize')
                await expect(handle).to_be_visible()
                before=(await page.locator('#library-agent').bounding_box())['width']
                wanted=await page.evaluate('askWidthWanted')
                await handle.focus()
                if clamped:
                    await page.keyboard.press('ArrowLeft')
                    assert (await page.locator('#library-agent').bounding_box())['width']==before
                    assert await page.evaluate('askWidthWanted')==wanted
                await page.keyboard.press('ArrowRight')
                after=(await page.locator('#library-agent').bounding_box())['width']
                assert after==pytest.approx(before-12,abs=1),{'before':before,'after':after,'wanted':wanted}
    run_browser(asyncio.wait_for(scenario(),25))
