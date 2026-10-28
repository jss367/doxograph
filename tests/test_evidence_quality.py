"""Human evidence assessments survive analysis and remain private when requested."""

from fastapi.testclient import TestClient

from doxograph import extract, server, store


def test_evidence_assessment_validation_history_and_reextraction():
    store.save_paper(store.new_paper('study'))
    claim = store.add_claim('study', {'text': 'A finding', 'reviewed': False})
    assessment = {'conditions': 'Model A, 500 examples', 'limitations': 'One domain only',
                  'evidence_origin': 'Shared evaluation set', 'evidence_relationship': 'shared_data'}
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        path = f'/api/papers/study/claims/{claim["id"]}'
        assert client.patch(path, json=assessment).status_code == 200
        assert client.patch(path, json={'evidence_relationship': 'proven'}).status_code == 422
        assert client.patch(path, json={'limitations': 'Updated caveat'}).status_code == 200
        history = client.get('/api/notebook/history/study').json()
        assert any(e['fields'] and e['fields'].get('limitations') == 'One domain only' for e in history['entries'])
        private = client.get('/api/query/claims?omit_notes=true').json()['items'][0]
        assert not set(assessment) & private.keys()
        paper = extract.merge_extraction('study', {'claims': [], 'summary': '', 'relevance': ''}, keep_reviewed=False)
        assert paper['claims'][0]['limitations'] == 'Updated caveat'
        assert paper['claims'][0]['evidence_relationship'] == 'shared_data'
