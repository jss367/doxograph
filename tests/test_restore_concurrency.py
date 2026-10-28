"""Restores respect extraction ownership and current operational metadata."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from fastapi.testclient import TestClient
from doxograph import server, store
from pdfs import minimal_pdf


def test_paper_restore_waits_for_extraction_and_rechecks_the_saved_version(monkeypatch):
    store.save_paper(store.new_paper('study', title='Earlier title'))
    paper = store.load_paper('study')
    paper['title'] = 'Current title'
    store.save_paper(paper)
    held, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original = store.extraction_lock

    @contextmanager
    def observe(key):
        attempted.set()
        with original(key):
            yield

    def extract():
        with original('study'):
            held.set()
            assert release.wait(5)
            current = store.load_paper('study')
            current['summary'] = 'Synthetic extraction completed'
            store.save_paper(current)

    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client, ThreadPoolExecutor(2) as pool:
        version = next(v for v in client.get('/api/workspace/backups').json()['versions'] if v['path'] == 'papers/study.json')
        worker = pool.submit(extract)
        assert held.wait(5)
        monkeypatch.setattr(store, 'extraction_lock', observe)
        restore = pool.submit(client.post, f'/api/workspace/backups/{version["id"]}/restore', json={'expected': version['expected']})
        try:
            assert attempted.wait(2), 'Restore did not acquire the extraction lock'
            assert not restore.done()
        finally:
            release.set()
        worker.result(timeout=5)
        assert restore.result(timeout=5).status_code == 409
    assert store.load_paper('study')['summary'] == 'Synthetic extraction completed'


def test_restore_keeps_current_pdf_error_state_and_recomputes_status():
    old = store.new_paper('study', title='A study')
    old['error'] = 'PDF download failed'
    store.save_paper(old)
    current = store.load_paper('study')
    current.update(extraction={'at': store.now()}, error='')
    store.pdf_path('study').write_bytes(minimal_pdf('Recovered source PDF'))
    store.refresh_status(current)
    store.save_paper(current)
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        version = next(v for v in client.get('/api/workspace/backups').json()['versions'] if v['path'] == 'papers/study.json')
        client.post(f'/api/workspace/backups/{version["id"]}/restore', json={'expected': version['expected']}).raise_for_status()
    restored = store.load_paper('study')
    assert restored['status'] == 'extracted'
    assert restored['error'] == ''
    assert restored['extraction'] == current['extraction']
