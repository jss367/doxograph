"""Question-centered evidence keeps user judgments, source links, and revisions."""

import csv
import io

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


def test_csv_preserves_evidence_assessments_and_question_stance():
    from doxograph import notebook
    store.save_paper(store.new_paper('study'))
    fields = {'conditions': '=formula()', 'limitations': 'One dataset',
              'evidence_origin': 'empirical', 'evidence_relationship': 'independent_replication'}
    claim = store.add_claim('study', {'text': 'A finding', **fields})
    board = notebook.Notebook(boards=[notebook.Board(id='q', title='Question', question='Does it generalize?',
        entries=[notebook.Entry(id='heading', kind='heading', text='Evidence'),
                 notebook.Entry(id='e', kind='claim', reference=notebook.Reference(paper='study', claim=claim['id']),
                                stance='qualifying')])])
    notebook.save_notebook(board)
    def rows(selection):
        return list(csv.reader(io.StringIO(bytes(notebook.export_selection(selection).body).decode())))
    selected = rows(notebook.Selection(claims=[notebook.Reference(paper='study', claim=claim['id'])], format='csv'))
    record = dict(zip(*selected, strict=True))
    assert record['Conditions'] == "'=formula()"
    assert record['Limitations'] == fields['limitations']
    assert record['Evidence origin'] == fields['evidence_origin']
    assert record['Replication assessment'] == fields['evidence_relationship']
    assert record['Bearing on question'] == ''
    exported = rows(notebook.Selection(board='q', format='csv'))
    assert all(len(row) == len(exported[0]) for row in exported)
    assert dict(zip(exported[0], exported[-1], strict=True))['Bearing on question'] == 'qualifying'
    store.delete_paper('study')
    missing = rows(notebook.Selection(board='q', format='csv'))
    assert missing[-1][0] == 'missing' and all(len(row) == len(missing[0]) for row in missing)


def test_cleared_question_keeps_conclusion_open_questions_and_stance_in_exports():
    from doxograph import notebook
    store.save_paper(store.new_paper('study'))
    claim = store.add_claim('study', {'text': 'Finding'})
    notebook.save_notebook(notebook.Notebook(boards=[notebook.Board(id='q', title='Investigation',
        conclusion='Still provisional', open_questions='Try another dataset', entries=[notebook.Entry(
            id='e', kind='claim', reference=notebook.Reference(paper='study', claim=claim['id']), stance='qualifying')])]))
    saved = notebook.load()['boards'][0]
    assert not saved['question'] and notebook.is_question(saved)
    markdown = bytes(notebook.export_selection(notebook.Selection(board='q')).body).decode()
    assert 'Still provisional' in markdown and 'Try another dataset' in markdown
    assert 'Bearing on question: qualifying' in markdown
    csv_text = bytes(notebook.export_selection(notebook.Selection(board='q', format='csv')).body).decode()
    assert 'Still provisional' in csv_text and 'Try another dataset' in csv_text and 'qualifying' in csv_text


def test_question_identity_survives_empty_text_and_reset_stances():
    from doxograph import notebook
    store.save_paper(store.new_paper('study'))
    claim = store.add_claim('study', {'text': 'Finding'})
    # Legacy question without populated text: infer from its evidence bearing.
    board = {'id': 'q', 'title': 'Does monitoring generalize?', 'entries': [
        {'id': 'e', 'kind': 'claim', 'reference': {'paper': 'study', 'claim': claim['id']}, 'stance': 'opposing'}]}
    store.write_json(config.data_dir() / 'notebook.json', {'boards': [board]})
    loaded = notebook.load()
    assert loaded['boards'][0]['kind'] == 'question'
    for fmt in ('markdown', 'csv'):
        exported = bytes(notebook.export_selection(notebook.Selection(board='q', format=fmt)).body).decode()
        assert 'opposing' in exported
    loaded['boards'][0]['entries'][0]['stance'] = 'background'
    notebook.save_notebook(notebook.Notebook.model_validate(loaded))
    restored = notebook.load()['boards'][0]
    assert restored['kind'] == 'question' and notebook.is_question(restored)
    assert notebook.Board(id='new', title='Blank question', kind='question').kind == 'question'
    assert notebook.Board(id='board', title='Ordinary board').kind == 'board'
