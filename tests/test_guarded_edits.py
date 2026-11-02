"""Undo's read/write pair cannot overwrite a newer paper or claim edit."""
import pytest
from fastapi.testclient import TestClient

from doxograph import server, store


@pytest.mark.parametrize('target', ['paper', 'claim'])
def test_stale_revision_rejects_a_write_and_keeps_current_data(target):
    store.save_paper(store.new_paper('paper', title='Original'))
    claim = store.add_claim('paper', {'text': 'Original'})
    path = '/api/papers/paper' if target == 'paper' else f'/api/papers/paper/claims/{claim["id"]}'
    field = 'title' if target == 'paper' else 'text'
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        snapshot = client.get('/api/papers/paper?with_revision=true').json()
        assert snapshot['paper'] == client.get('/api/papers/paper').json()
        client.patch(path, json={field: 'Other window'}).raise_for_status()
        response = client.patch(path, json={field: 'Overwritten', 'expected_revision': snapshot['revision']})
        assert response.status_code == 409
        current = client.get('/api/papers/paper').json()
        assert (current if target == 'paper' else current['claims'][0])[field] == 'Other window'
        assert 'expected_revision' not in current
        assert 'expected_revision' not in current['claims'][0]
        fresh = client.get('/api/papers/paper?with_revision=true').json()
        client.patch(path, json={field: 'Accepted', 'expected_revision': fresh['revision']}).raise_for_status()
        current = client.get('/api/papers/paper').json()
        assert (current if target == 'paper' else current['claims'][0])[field] == 'Accepted'
        assert 'expected_revision' not in current
        assert 'expected_revision' not in current['claims'][0]
