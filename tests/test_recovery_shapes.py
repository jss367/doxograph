"""Damaged recovery files stay visible as issues without hiding valid records."""
import json

import pytest
from fastapi.testclient import TestClient

from doxograph import backups, config, job_journal, server, store


@pytest.mark.parametrize('payload', [[], None, 'text', 3, True])
def test_nonobject_backup_does_not_hide_valid_versions(payload):
    store.write_atomic(config.data_dir() / 'context.md', 'Original')
    store.write_atomic(config.data_dir() / 'context.md', 'Current')
    valid = backups.versions()['versions']
    assert len(valid) == 1
    path = config.data_dir() / 'backups' / 'damaged' / ('b' * 32 + '.json')
    store.write_json(path, payload)
    before = path.read_bytes()
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.get('/api/workspace/backups')
        assert response.status_code == 200
        assert response.json()['versions'] == valid
        assert len(response.json()['issues']) == 1
        assert client.get('/api/workspace/backups/' + path.stem).status_code == 409
        assert client.post('/api/workspace/backups/' + path.stem + '/restore',
                           json={'expected': '0' * 64}).status_code == 409
    assert path.read_bytes() == before


@pytest.mark.parametrize('field,payload', [('root', []), ('root', None), ('job', []),
                                          ('job', None), ('source', []), ('source', 'text')])
def test_wrong_shaped_journal_keeps_other_jobs_recoverable(monkeypatch, field, payload):
    monkeypatch.setattr(job_journal, '_paths', {})
    valid = {'version': 2, 'pid': 1, 'owner': 'f' * 32,
             'job': {'id': 2, 'workspace': config.workspace_id(), 'state': 'done', 'label': 'Valid job'}, 'source': {}}
    damaged = json.loads(json.dumps(valid))
    damaged = payload if field == 'root' else {**damaged, field: payload}
    folder = config.data_dir() / '.jobs'
    path = folder / '1.json'
    store.write_json(path, damaged)
    before = path.read_bytes()
    store.write_json(folder / '2.json', valid)
    issues = []
    recovered = job_journal.recover(issues)
    assert [job['id'] for job in recovered] == [2]
    assert len(issues) == 1 and '1.json' in issues[0]
    assert path.read_bytes() == before
