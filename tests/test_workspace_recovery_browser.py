"""Previous versions, portable downloads, and interrupted imports are usable in WebKit."""

import os
from pathlib import Path
import subprocess
import sys

import pytest
from playwright.async_api import expect

from browser_support import browser_session, run_browser
from doxograph import config, store
from pdfs import minimal_pdf
from test_browser import _answer, _paper, _server


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_restore_previous_version_and_download_archive(engine):
    config.set_ai_enabled(False)
    path = config.data_dir() / 'notebook.json'
    store.write_json(path, {'revision': 1, 'notes': 'Original conclusion'})
    store.write_json(path, {'revision': 2, 'notes': 'Later conclusion'})

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page(viewport={'width': 1280, 'height': 950})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            with _server() as url:
                await page.goto(url)
                await page.locator('#workspace-actions > summary').click()
                await page.locator('#btn-workspace-backups').click()
                await expect(page.locator('.backup-version')).to_have_count(1)
                await page.locator('[data-backup-restore]').first.click()
                await _answer(page, 'Restore version')
                await expect(page.locator('#backups-status')).to_contain_text('Restored.')
                if engine == 'chromium':
                    Path('.context').mkdir(exist_ok=True)
                    await page.screenshot(path='.context/previous-document-versions.png')
                await page.get_by_role('button', name='Close previous versions').click()
                await expect(page.locator('#workspace-notes')).to_have_value('Original conclusion')
                await page.locator('#workspace-actions > summary').click()
                async with page.expect_download() as download:
                    await page.locator('#btn-workspace-archive').click()
                artifact = await download.value
                assert artifact.suggested_filename.endswith('.zip')
                assert await artifact.failure() is None
                assert not errors

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_uploaded_pdf_resumes_after_process_exit(engine):
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='A monitoring study'))
    pdf = minimal_pdf('A monitoring study.')
    store.pdf_path('study').write_bytes(pdf)
    script = f'''from doxograph import config, ingest, job_queue, server
import io
config.ensure_dirs()
staged = ingest.stage_upload(io.BytesIO({pdf!r}), 'study.pdf')
job = job_queue._new_import_job('study.pdf', False, staged=staged, filename='study.pdf')
job_queue._set(job, state='fetching')
'''
    subprocess.run([sys.executable, '-c', script], env=os.environ.copy(), check=True)

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                resume = page.get_by_role('button', name='Resume import', exact=True)
                await expect(resume).to_be_visible()
                await resume.click()
                await expect(resume).to_have_count(0)
                await expect(page.locator('.job .open-job')).to_be_visible()
                assert len(store.paper_keys()) == 1
                assert list(config.pdfs_dir().glob('.incoming-*')) == []

    run_browser(scenario())


@pytest.mark.browser
@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('editor', ['claim', 'research'])
def test_restore_refuses_unsaved_editor_without_losing_the_draft(engine, editor):
    config.set_ai_enabled(False)
    if editor == 'claim':
        _paper('study', 'Synthetic paper')
        store.update_claim('study', 'study-c1', {'text': 'Latest saved claim'})
    else:
        store.save_context('Earlier context')
        store.save_context('Latest saved context')

    async def scenario():
        async with browser_session(engine) as browser:
            page = await browser.new_page()
            with _server() as url:
                await page.goto(url)
                if editor == 'claim':
                    await page.locator('#btn-claims-view').click()
                    await page.locator('[data-act="edit"]').first.click()
                    field = page.locator('form[data-form] [name="text"]')
                else:
                    await page.locator('#research-nav [data-view="research"]').click()
                    field = page.locator('#research-context')
                await field.fill('An unsaved synthetic draft')
                await page.locator('#workspace-actions > summary').click()
                await page.locator('#btn-workspace-backups').click()
                await page.locator('[data-backup-restore]').first.click()
                await expect(page.locator('#backups-status')).to_contain_text('Save or cancel unsaved edits')
                await expect(page.locator('#ask')).not_to_be_visible()
                await page.locator('#backups-close').click()
                await expect(field).to_have_value('An unsaved synthetic draft')
                assert (store.load_paper('study')['claims'][0]['text'] if editor == 'claim' else store.load_context()) == ('Latest saved claim' if editor == 'claim' else 'Latest saved context')

    run_browser(scenario())
