"""Recovery keeps revisions monotonic and archives portable and workspace-local."""

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile

import pytest

from fastapi.testclient import TestClient

from doxograph import backups, config, ingest, job_journal, notebook, server, store, workspace_archive
from pdfs import minimal_pdf


def test_backups_restore_with_conflict_protection_and_monotonic_revision():
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        client.put('/api/notebook/notes', json={'notes': 'First conclusion', 'previous': ''}).raise_for_status()
        client.put('/api/notebook/notes', json={'notes': 'Second conclusion', 'previous': 'First conclusion'}).raise_for_status()
        versions = client.get('/api/workspace/backups').json()['versions']
        version = next(v for v in versions if client.get('/api/workspace/backups/' + v['id']).json()['notes'] == 'First conclusion')
        assert client.post(f'/api/workspace/backups/{version["id"]}/restore', json={'expected': '0'*64}).status_code == 409
        before = notebook.load()['revision']
        assert client.post(f'/api/workspace/backups/{version["id"]}/restore', json={'expected': version['expected']}).status_code == 200
        assert notebook.load()['notes'] == 'First conclusion'
        assert notebook.load()['revision'] > before
        assert client.put('/api/notebook', json={'revision': before, 'notes': 'A stale edit'}).status_code == 409
        assert any(json.loads(backups.read(p)[1])['notes'] == 'Second conclusion'
                   for p in (config.data_dir() / 'backups').glob('*/*.json'))


def test_paper_restore_does_not_reissue_claim_ids():
    store.save_paper(store.new_paper('study'))
    first = store.add_claim('study', {'text': 'Original'})
    second = store.add_claim('study', {'text': 'Later claim'})
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        version = next(v for v in client.get('/api/workspace/backups').json()['versions']
            if v['path'] == 'papers/study.json' and len(json.loads(client.get('/api/workspace/backups/'+v['id']).content)['claims']) == 1)
        client.post(f'/api/workspace/backups/{version["id"]}/restore', json={'expected': version['expected']}).raise_for_status()
    assert store.load_paper('study')['claims'][0]['id'] == first['id']
    assert store.add_claim('study', {'text': 'Newest'})['id'] not in (first['id'], second['id'])


def test_portable_archive_has_local_assets_checksums_and_no_other_workspace():
    config.set_ai_enabled(False)
    other = config.create_workspace('Separate private research')
    with config.use_workspace(other['id']):
        store.save_paper(store.new_paper('private', title='Do not include this'))
    store.save_paper(store.new_paper('study', title='Monitoring study', authors=['Ada Example'], year=2026))
    store.add_claim('study', {'text': 'A finding', 'limitations': 'One dataset'})
    pdf = minimal_pdf('A finding')
    store.pdf_path('study').write_bytes(pdf)
    note_id = 'a' * 32
    image = 'b'*64 + '.png'
    (config.data_dir() / 'note-images').mkdir()
    (config.data_dir() / 'note-images' / image).write_bytes(b'image bytes')
    body = f'![Figure](/api/research-notes/images/{image})\n[Paper](/#paper=study)\n`[Example](/#paper=study)`'
    store.write_json(config.data_dir() / 'research-notes.json', [dict(id=note_id, title='My argument', body=body,
        revision=1, created=store.now(), updated=store.now())])
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        prepared = client.post('/api/workspace/archive').json()
        assert client.get(prepared['download'], headers={'X-Doxograph-Workspace':other['id']}).status_code == 404
        response = client.get(prepared['download'])
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            assert 'data/pdfs/study.pdf' in names and archive.read('data/pdfs/study.pdf') == pdf
            assert not any('private' in name or 'settings.json' in name for name in names)
            note = archive.read(next(n for n in names if n.startswith('notes/'))).decode()
            assert f'../data/note-images/{image}' in note
            assert '../papers/study.md' in note
            assert '`[Example](/#paper=study)`' in note
            assert 'One dataset' in archive.read('papers/study.md').decode()
            manifest = json.loads(archive.read('manifest.json'))
            assert all(hashlib.sha256(archive.read(name)).hexdigest() == checksum for name, checksum in manifest['files'].items())
            # A moved archive can restore its raw workspace data to an empty directory.
            assert json.loads(archive.read('data/papers/study.json'))['claims'][0]['text'] == 'A finding'


def test_archive_links_do_not_redirect_another_workspaces_paper():
    link = '[Other workspace](/#ws=another&paper=study)'
    assert workspace_archive.portable_links(link, {}) == link


def test_interrupted_import_survives_a_real_process_exit_and_requires_resume(monkeypatch):
    script = '''from doxograph import config, server, ingest
config.ensure_dirs()
job = server._new_import_job('A study', False, ref=ingest.Ref('arxiv','2602.06941','2602.06941'))
server._set(job, state='fetching')
print(job['id'])
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, env=os.environ.copy(), check=True)
    identifier = int(result.stdout.strip())
    monkeypatch.setattr(server, '_jobs', {})
    submitted = []
    monkeypatch.setattr(server._pool, 'submit', lambda *args: submitted.append(args))
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        [job] = client.get('/api/jobs').json()['jobs']
        assert job['id'] == identifier and job['interrupted'] and job['retryable']
        assert '_import' not in job and not submitted
        client.post(f'/api/jobs/{identifier}/retry').raise_for_status()
        assert len(submitted) == 1
        assert client.post(f'/api/jobs/{identifier}/retry').status_code == 409


def test_backup_versions_are_bounded_and_workspace_scoped(monkeypatch):
    monkeypatch.setattr(backups, 'KEEP_VERSIONS', 3)
    for index in range(7):
        store.write_atomic(config.data_dir() / 'context.md', f'Context {index}')
    assert len(backups.versions()['versions']) == 3
    other = config.create_workspace('Other research')
    with config.use_workspace(other['id']):
        assert backups.versions()['versions'] == []


def test_failed_backup_does_not_overwrite_the_original(monkeypatch):
    path = config.data_dir() / 'context.md'
    store.write_atomic(path, 'Original text')

    def fail(*args):
        raise OSError('Backup storage is full')

    monkeypatch.setattr(backups, 'preserve', fail)
    with pytest.raises(OSError, match='full'):
        store.write_atomic(path, 'Replacement text')
    assert path.read_text() == 'Original text'


def test_live_jobs_cannot_be_recovered_by_a_second_server():
    job = server._new_import_job('A study', False, ref=ingest.Ref('arxiv', '2602.06941', '2602.06941'))
    server._set(job, state='fetching')
    assert job_journal.recover() == []
    assert job['state'] == 'fetching'


@pytest.mark.parametrize('key', ['notebook', 'context', 'vocabulary', 'agreements'])
def test_archive_with_a_paper_named_like_a_document_lock_finishes(key):
    store.save_paper(store.new_paper(key, title='Synthetic lock collision'))
    # A subprocess bounds the regression even if a flock blocks on a descriptor
    # owned by its own thread. This never reads the user's workspace.
    script = '''from doxograph import workspace_archive
path = workspace_archive.build()
try:
    assert path.stat().st_size > 0
finally:
    path.unlink()
'''
    subprocess.run([sys.executable, '-c', script], env=os.environ.copy(), check=True, timeout=10)
