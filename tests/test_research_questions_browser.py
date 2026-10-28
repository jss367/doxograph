"""Questions combine claims from several papers without requiring a model."""

from pathlib import Path

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config
from test_browser import _answer, _paper, _server
from test_research_tools_browser import ready


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_question_evidence_and_conclusion_survive_reload(engine):
    config.set_ai_enabled(False)
    _paper('one', 'Monitoring in one setting')
    _paper('two', 'Monitoring under different conditions')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1440, 'height': 1100})
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-tool="select-visible"]').click()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                dialog = page.locator('#research-tools')
                await dialog.get_by_role('button', name='New research question', exact=True).click()
                await _answer(page, 'Create question', 'Does monitoring generalize?')
                await ready(page)
                await dialog.get_by_role('button', name='Add selected claims', exact=True).click()
                await ready(page)
                await page.locator('#board-conclusion').fill('Evidence depends on the evaluation conditions.')
                await page.locator('#board-open_questions').fill('Try another model family.')
                await page.locator('[data-entry-stance]').first.select_option('supporting')
                await page.locator('[data-entry-stance]').last.select_option('qualifying')
                await expect(page.locator('.question-overview')).to_contain_text('Supporting evidence')
                await dialog.get_by_role('button', name='Save question', exact=True).click()
                await ready(page)
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/research-question-evidence.png')
                await page.reload()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                await expect(page.locator('#board-conclusion')).to_have_value('Evidence depends on the evaluation conditions.')
                await expect(page.locator('[data-entry-stance]').last).to_have_value('qualifying')
                await page.locator('#board-question').fill('')
                await dialog.get_by_role('button', name='Save question', exact=True).click()
                await ready(page)
                await expect(page.locator('#board-conclusion')).to_have_value('Evidence depends on the evaluation conditions.')
                await page.reload()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                await expect(page.locator('#board-question')).to_have_value('')
                await expect(page.locator('#board-open_questions')).to_have_value('Try another model family.')
                await expect(page.locator('[data-entry-stance]').last).to_have_value('qualifying')
                await page.reload()
                await page.locator('[data-tool="boards"]').click()
                await ready(page)
                await expect(page.locator('#board-conclusion')).to_have_value('Evidence depends on the evaluation conditions.')
                await page.locator('#board-conclusion').fill('')
                await page.locator('#board-open_questions').fill('')
                await dialog.get_by_role('button', name='Save board', exact=True).click()
                await ready(page)
                await page.reload()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                await expect(page.locator('#board-question')).to_have_value('')
                await expect(page.locator('[data-entry-stance]').last).to_have_value('qualifying')
                for selector in await page.locator('[data-entry-stance]').all():
                    await selector.select_option('background')
                await dialog.get_by_role('button', name='Save question', exact=True).click()
                await ready(page)
                await page.reload()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                await expect(page.locator('#board-picker')).not_to_have_value('')
                await expect(page.locator('[data-entry-stance]').last).to_have_value('background')


    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_existing_board_can_become_a_question_without_losing_commentary(engine):
    from doxograph import notebook
    config.set_ai_enabled(False)
    _paper('study', 'Monitoring study')
    notebook.save_notebook(notebook.Notebook(boards=[notebook.Board(id='board', title='Does it generalize?',
        entries=[notebook.Entry(id='note', kind='note', text='Existing interpretation')])]))

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                await page.locator('[data-tool="boards"]').click()
                await ready(page)
                await page.get_by_role('button', name='Turn into research question', exact=True).click()
                await ready(page)
                await expect(page.locator('#board-question')).to_have_value('Does it generalize?')
                await expect(page.locator('[data-entry-text]')).to_have_value('Existing interpretation')
                await page.reload()
                await page.locator('[data-tool="questions"]').click()
                await ready(page)
                await expect(page.locator('#board-question')).to_have_value('Does it generalize?')
                await expect(page.locator('[data-entry-text]')).to_have_value('Existing interpretation')
    run_browser(scenario())
