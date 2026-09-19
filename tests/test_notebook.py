"""Persistence and source integrity for research without model calls."""
import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from doxograph import config, notebook, store
from doxograph.server import app


@pytest.fixture
def client():
    config.set_ai_enabled(False)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def seed(key='one'):
    paper = store.new_paper(key, title=f'Paper {key}', authors=['Ada Example'], year=2026)
    store.save_paper(paper)
    claim = store.add_claim(key, {'text': 'Original finding', 'quote': 'A quotation', 'tags': ['testing']})
    return {'paper': key, 'claim': claim['id']}


def test_notebook_workspace_isolation_and_conflict(client):
    ref = seed()
    data = client.get('/api/notebook').json()
    data['boards'] = [{'id': 'board', 'title': 'A research question', 'entries': [
        {'id': 'heading', 'kind': 'heading', 'text': 'Evidence'},
        {'id': 'claim', 'kind': 'claim', 'reference': ref, 'text': 'My interpretation'}]}]
    data['searches'] = [{'id': 'search', 'name': 'Unreviewed evidence', 'unreviewed': True}]
    data['reading'] = [{'paper': 'one', 'status': 'reading', 'page': 3, 'bookmarks': [3, 1, 3]}]
    response = client.put('/api/notebook', json=data)
    assert response.status_code == 200
    assert response.json()['reading'][0]['bookmarks'] == [1, 3]
    assert client.put('/api/notebook', json=data).status_code == 409
    workspace = config.create_workspace('Other research')
    other = client.get('/api/notebook', headers={'X-Doxograph-Workspace': workspace['id']}).json()
    assert not other['boards'] and other['revision'] == 0
    assert client.get('/api/notebook').json() == response.json()
    assert client.get('/api/state').json()['notebook'] == response.json()


def test_manual_connections_validate_sources_and_keep_missing_board_entries(client):
    one, two = seed(), seed('two')
    data = client.get('/api/notebook').json()
    data['connections'] = [{'id':'link', 'source':one, 'target':two, 'relation':'qualifies', 'note':'Different conditions'}]
    response = client.put('/api/notebook', json=data)
    assert response.status_code == 200
    assert client.get('/api/state').json()['notebook']['connections'][0]['relation'] == 'qualifies'
    data = response.json()
    store.delete_claim('two', two['claim'])
    data['connections'][0]['note'] = 'Keep my judgment even when the source is removed'
    assert client.put('/api/notebook', json=data).status_code == 200
    data = client.get('/api/notebook').json()
    data['connections'][0]['source'] = {'paper':'absent', 'claim':'absent'}
    assert client.put('/api/notebook', json=data).status_code == 404
    data['connections'][0]['source'] = two
    assert client.put('/api/notebook', json=data).status_code == 422


def test_history_restores_only_requested_claim_or_note_and_rechecks_quotes(client):
    ref = seed()
    store.update_claim('one', ref['claim'], {'text':'Revised finding', 'note':'A new note'})
    state = client.get('/api/notebook/history/one').json()
    assert state['entries'][0]['fields']['text'] == 'Original finding'
    # A concurrent edit refuses restoration rather than overwriting unseen work.
    store.update_claim('one', ref['claim'], {'text':'Another edit'})
    restore = {'revision':state['entries'][0]['id'], 'expected':state['expected']}
    assert client.post('/api/notebook/history/one/restore', json=restore).status_code == 409
    latest = client.get('/api/notebook/history/one').json()
    restore['expected'] = latest['expected']
    assert client.post('/api/notebook/history/one/restore', json=restore).status_code == 200
    restored = store.load_paper('one')['claims'][0]
    assert restored['text'] == 'Original finding' and restored['note'] == ''
    assert len(store.load_paper('one')['history']) == 3
    client.patch('/api/papers/one', json={'notes':'First note'})
    client.patch('/api/papers/one', json={'notes':'Second note'})
    latest = client.get('/api/notebook/history/one').json()
    assert client.post('/api/notebook/history/one/restore', json={
        'revision':latest['entries'][0]['id'], 'expected':latest['expected']}).status_code == 200
    assert store.load_paper('one')['notes'] == 'First note'
    assert store.load_paper('one')['claims'][0]['text'] == 'Original finding'


def test_deleted_claim_can_be_restored_without_reusing_an_identifier(client):
    ref = seed()
    store.delete_claim('one', ref['claim'])
    newer = store.add_claim('one', {'text':'Keep this claim'})
    latest = client.get('/api/notebook/history/one').json()
    assert client.post('/api/notebook/history/one/restore', json={
        'revision':latest['entries'][0]['id'], 'expected':latest['expected']}).status_code == 200
    claims = store.load_paper('one')['claims']
    assert {c['id'] for c in claims} == {ref['claim'], newer['id']}


def test_bulk_validates_all_sources_before_writing_and_records_history(client):
    one, two = seed(), seed('two')
    store.add_tag('testing')
    store.add_tag('methods')
    body = {'claims':[one,two], 'field':'tags','mode':'add','values':['methods']}
    assert client.post('/api/notebook/bulk', json=body).json() == {'changed':2}
    assert store.load_paper('one')['claims'][0]['tags'] == ['methods','testing']
    assert len(store.load_paper('one')['history']) == 1
    body.update(mode='remove', values=['testing'])
    assert client.post('/api/notebook/bulk', json=body).status_code == 200
    body.update(mode='replace', values=['testing'], claims=[one, {'paper':'missing','claim':'missing'}])
    assert client.post('/api/notebook/bulk', json=body).status_code == 404
    assert store.load_paper('one')['claims'][0]['tags'] == ['methods']
    body.update(claims=[one], values=['undeclared-topic'])
    assert client.post('/api/notebook/bulk', json=body).status_code == 422
    assert client.post('/api/notebook/bulk', json={'papers':['one','two'],'field':'labels','values':['Read Next']}).status_code == 200
    assert store.load_paper('one')['labels'] == ['read-next']
    assert client.post('/api/notebook/bulk', json={'papers':['one'],'field':'labels','mode':'replace','values':[]}).status_code == 200
    assert store.load_paper('one')['labels'] == []


def test_selective_exports_preserve_board_order_citations_notes_and_missing_sources(client):
    one, two = seed(), seed('two')
    store.update_claim('one', one['claim'], {'text':'=HYPERLINK("bad")','note':'My note'})
    client.patch('/api/papers/one', json={'notes':'Paper context'})
    data = client.get('/api/notebook').json()
    data['boards'] = [{'id':'board','title':'Evidence for my argument','entries':[
        {'id':'heading','kind':'heading','text':'First question'},
        {'id':'two','kind':'claim','reference':two,'text':'Second paper first'},
        {'id':'one','kind':'claim','reference':one,'text':'My reasoning'},
        {'id':'note','kind':'note','text':'<script>literal text</script>'}]}]
    assert client.put('/api/notebook', json=data).status_code == 200
    markdown = client.post('/api/notebook/export',json={'board':'board'}).text
    assert markdown.index('Paper two') < markdown.index('Paper one')
    assert 'Ada Example' in markdown and 'My note' in markdown and 'Paper context' in markdown
    assert '&lt;script&gt;' in markdown and '<script>' not in markdown
    response = client.post('/api/notebook/export',json={'claims':[one], 'format':'csv'})
    rows = list(csv.reader(io.StringIO(response.text)))
    assert len(rows) == 2 and rows[1][1].startswith("'=HYPERLINK")
    assert rows[1][2] == 'Paper one' and rows[1][10] == 'My note'
    store.delete_claim('two', two['claim'])
    assert 'Missing source: two' in client.post('/api/notebook/export',json={'board':'board'}).text
    assert client.post('/api/notebook/export',json={'claims':[two]}).status_code == 404


@pytest.mark.parametrize('path,body', [
    ('/export', {'papers':['../../outside']}),
    ('/bulk', {'papers':['../../outside'],'field':'labels','values':[]}),
    ('', {'reading':[{'paper':'../../outside'}]}),
])
def test_rejects_paths_in_paper_keys(client,path,body):
    call = client.put if path == '' else client.post
    assert call('/api/notebook'+path,json=body).status_code == 422


def test_concurrent_notebook_edits_require_refresh(client):
    body = notebook.Notebook(searches=[notebook.SavedSearch(id='one',name='One')])
    def write():
        try:
            return notebook.save_notebook(body)['revision']
        except Exception as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: write(), range(2))) == [1,409]


def test_review_only_changes_do_not_fill_edit_history(client):
    ref = seed()
    store.update_claim('one', ref['claim'], {'reviewed':False})
    store.review_claims('one', True)
    assert client.get('/api/notebook/history/one').json()['entries'] == []


def test_bulk_topics_on_explicitly_selected_papers(client):
    seed()
    store.add_claim('one', {'text':'A second finding'})
    store.add_tag('methods')
    result = client.post('/api/notebook/bulk', json={'papers':['one'], 'field':'tags', 'values':['methods']})
    assert result.status_code == 200 and result.json()['changed'] == 2
    assert all('methods' in c['tags'] for c in store.load_paper('one')['claims'])
