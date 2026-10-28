"""Journal recovery distinguishes process instances when PIDs are reused."""

import json
import os
import subprocess
import sys
from fastapi.testclient import TestClient
from doxograph import config, server, store


def test_dead_process_journal_recovers_even_when_its_pid_is_reused(monkeypatch):
    script = '''from doxograph import config, server, ingest
config.ensure_dirs()
job = server._new_import_job('Synthetic import', False, ref=ingest.Ref('arxiv','2602.06941','2602.06941'))
print(job['id'])
'''
    result = subprocess.run([sys.executable, '-c', script], env=os.environ.copy(), capture_output=True, text=True, check=True)
    identifier = int(result.stdout.strip())
    path = config.data_dir() / '.jobs' / f'{identifier}.json'
    data = json.loads(path.read_text())
    # The original process is gone; this unrelated live PID must not own its
    # saved per-process lease, as happens when the OS recycles PIDs after reboot.
    data['pid'] = os.getpid()
    store.write_json(path, data)
    monkeypatch.setattr(server, '_jobs', {})
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        [job] = client.get('/api/jobs').json()['jobs']
        assert job['id'] == identifier
        assert job['interrupted'] and job['retryable']
