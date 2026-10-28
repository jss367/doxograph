"""Question-centered evidence keeps user judgments, source links, and revisions."""

from fastapi.testclient import TestClient

from doxograph import config, server, store


def test_question_roundtrip_export_and_missing_source():
    config.set_ai_enabled(False)
    store.save_paper(store.new_paper('study', title='An oversight study'))
    claim = store.add_claim('study', {'text': 'Monitoring works under these conditions.'})
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        data = client.get('/api/notebook').json()
        data['boards'] = [{'id': 'q', 'title': 'Does monitoring generalize?',
            'question': 'Does monitoring generalize?', 'conclusion': 'Only under tested conditions.',
            'open_questions': 'Test a new model.', 'entries': [{'id': 'e', 'kind': 'claim',
                'reference': {'paper': 'study', 'claim': claim['id']}, 'stance': 'qualifying'}]}]
        saved = client.put('/api/notebook', json=data)
        assert saved.status_code == 200
        assert client.put('/api/notebook', json=data).status_code == 409
        exported = client.post('/api/notebook/export', json={'board': 'q'}).text
        assert 'Only under tested conditions.' in exported
        assert 'Test a new model.' in exported
        assert 'Bearing on question: qualifying' in exported
        store.delete_paper('study')
        assert 'Missing source: study' in client.post('/api/notebook/export', json={'board': 'q'}).text
        preserved = client.get('/api/notebook').json()
        assert preserved['boards'][0]['conclusion'] == 'Only under tested conditions.'
        assert client.put('/api/notebook', json=preserved).status_code == 200
