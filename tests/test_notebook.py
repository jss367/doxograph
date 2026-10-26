"""Persistence and source integrity for research without model calls."""
import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

from doxograph import config, notebook, store
from doxograph.server import app
from pdfs import minimal_pdf


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


def test_workspace_notes_migration_isolation_and_concurrent_board_edits(client):
    # Existing notebooks load with an empty document, without a migration step.
    store.write_json(config.data_dir() / 'notebook.json', {'revision': 4, 'boards': []})
    original = client.get('/api/notebook').json()
    assert original['notes'] == ''
    board_edit = dict(original, boards=[{'id': 'board', 'title': 'Evidence', 'entries': []}])
    assert client.put('/api/notebook', json=board_edit).status_code == 200
    text = '# Research questions\n\nWhat changes across conditions? <literal text>'
    response = client.put('/api/notebook/notes', json={'notes': text, 'previous': ''})
    assert response.status_code == 200
    assert response.json()['boards'][0]['title'] == 'Evidence'
    assert response.json()['notes'] == text
    assert response.json()['revision'] == 6
    assert client.get('/api/state').json()['notebook']['notes'] == text
    assert client.put('/api/notebook', json=board_edit).status_code == 409

    workspace = config.create_workspace('Other research')
    headers = {'X-Doxograph-Workspace': workspace['id']}
    assert client.get('/api/notebook', headers=headers).json()['notes'] == ''
    assert client.put('/api/notebook/notes', headers=headers,
                      json={'notes': 'Other questions', 'previous': ''}).status_code == 200
    assert client.get('/api/notebook').json()['notes'] == text
    assert client.put('/api/notebook/notes', json={'notes': '', 'previous': text}).status_code == 200
    assert client.get('/api/notebook').json()['notes'] == ''


def test_workspace_notes_conflict_keeps_saved_text(client):
    update = {'notes': 'First writer', 'previous': ''}
    first = client.put('/api/notebook/notes', json=update)
    assert first.status_code == 200
    # A lost response may cause the browser to retry an already saved edit.
    assert client.put('/api/notebook/notes', json=update).json() == first.json()
    response = client.put('/api/notebook/notes', json={'notes': 'Second writer', 'previous': ''})
    assert response.status_code == 409
    assert 'another window' in response.json()['detail']
    assert client.get('/api/notebook').json()['notes'] == 'First writer'
    assert client.put('/api/notebook/notes', json={'notes': None, 'previous': 'First writer'}).status_code == 422


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


def test_highlights_validate_their_paper_and_page_geometry(client):
    seed()
    data = client.get('/api/notebook').json()
    highlight = {'id': 'mark', 'paper': 'one', 'page': 2, 'text': 'A quotation', 'color': 'green',
                 'rects': [{'x': .1, 'y': .2, 'w': .5, 'h': .02}], 'note': 'Compare with the appendix'}
    data['highlights'] = [highlight]
    response = client.put('/api/notebook', json=data)
    assert response.status_code == 200
    assert client.get('/api/state').json()['notebook']['highlights'] == [dict(highlight, kind='passage', question=False)]
    data = response.json()
    for change in ({'paper': 'absent'}, {'rects': []}, {'rects': [{'x': 1.5, 'y': 0, 'w': .1, 'h': .1}]},
                   {'color': 'red'}, {'page': 0}):
        data['highlights'] = [dict(highlight, **change)]
        assert client.put('/api/notebook', json=data).status_code in (404, 422), change
    data['highlights'] = [highlight, highlight]
    assert client.put('/api/notebook', json=data).status_code == 422
    # A removed paper keeps the highlights already made on it.
    store.delete_paper('one')
    data['highlights'] = [dict(highlight, note='Still mine')]
    assert client.put('/api/notebook', json=data).status_code == 200
    data = client.get('/api/notebook').json()
    data['highlights'].append(dict(highlight, id='late'))
    assert client.put('/api/notebook', json=data).status_code == 404


def test_figure_marks_and_questions_are_saved_with_highlights(client):
    seed()
    data = client.get('/api/notebook').json()
    figure = {'id': 'fig', 'paper': 'one', 'page': 1, 'kind': 'figure', 'text': '', 'color': 'blue',
              'rects': [{'x': .1, 'y': .1, 'w': .4, 'h': .3}], 'note': 'Why is the axis logarithmic?', 'question': True}
    data['highlights'] = [figure]
    data = client.put('/api/notebook', json=data).json()
    assert data['highlights'] == [figure]
    data['highlights'] = [dict(figure, rects=figure['rects'] * 2)]
    assert client.put('/api/notebook', json=data).status_code == 422
    data['highlights'] = [dict(figure, kind='table')]
    assert client.put('/api/notebook', json=data).status_code == 422


def test_annotated_pdf_carries_marks_without_changing_the_stored_pdf(client):
    seed()
    original = minimal_pdf(['First page.', 'Second page.'])
    store.pdf_path('one').write_bytes(original)
    assert client.get('/api/notebook/annotated/one').status_code == 404
    data = client.get('/api/notebook').json()
    data['highlights'] = [
        {'id': 'passage', 'paper': 'one', 'page': 1, 'text': 'First page.', 'color': 'green', 'note': 'Compare later',
         'rects': [{'x': .1, 'y': .1, 'w': .5, 'h': .02}, {'x': .1, 'y': .12, 'w': .3, 'h': .02}]},
        {'id': 'figure', 'paper': 'one', 'page': 2, 'kind': 'figure', 'color': 'pink', 'question': True,
         'note': 'Which model?', 'rects': [{'x': .5, 'y': .5, 'w': .25, 'h': .25}]},
        {'id': 'replaced', 'paper': 'one', 'page': 9, 'rects': [{'x': 0, 'y': 0, 'w': .1, 'h': .1}]}]
    client.put('/api/notebook', json=data).raise_for_status()
    response = client.get('/api/notebook/annotated/one')
    assert response.status_code == 200 and response.headers['content-type'] == 'application/pdf'
    assert 'one-annotated.pdf' in response.headers['content-disposition']
    assert response.headers['x-doxograph-skipped-marks'] == '1'
    assert store.pdf_path('one').read_bytes() == original
    pages = PdfReader(io.BytesIO(response.content)).pages
    [highlight] = [a.get_object() for a in pages[0]['/Annots']]
    assert highlight['/Subtype'] == '/Highlight' and highlight['/Contents'] == 'Compare later'
    # 612 x 792 points: x runs from the left, y from the top of the page as drawn.
    assert [round(float(n), 2) for n in highlight['/Rect']] == [61.2, 681.12, 367.2, 712.8]
    assert len(highlight['/QuadPoints']) == 16
    [square] = [a.get_object() for a in pages[1]['/Annots']]
    assert square['/Subtype'] == '/Square' and square['/Contents'] == 'Question: Which model?'
    # Both kinds print: flag 4 is Print.
    assert highlight['/F'] == square['/F'] == 4
    assert [round(float(n), 2) for n in square['/Rect']] == [306.0, 198.0, 459.0, 396.0]


def test_annotated_pdf_follows_page_rotation():
    writer = PdfWriter()
    writer.add_blank_page(width=600, height=800)
    page = writer.pages[0]
    rect = {'x': 0, 'y': 0, 'w': .5, 'h': .25}
    assert notebook.page_box(page, rect) == (0, 600, 300, 800)
    # Turned a quarter clockwise, the top left of the view is the bottom left of the page.
    page.rotate(90)
    assert notebook.page_box(page, rect) == (0, 0, 150, 400)
    page.rotate(90)
    assert notebook.page_box(page, rect) == (300, 0, 600, 200)


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


@pytest.mark.parametrize('retain_low_claim', [False, True])
def test_restore_deleted_legacy_claim_does_not_overwrite_replacement(client, retain_low_claim):
    ref = seed()
    if retain_low_claim:
        ref['claim'] = store.add_claim('one', {'text':'Legacy highest claim'})['id']
    legacy = store.load_paper('one')
    legacy.pop('claim_seq')
    store.write_json(store.paper_path('one'), legacy)
    store.delete_claim('one', ref['claim'])
    replacement = store.add_claim('one', {'text':'Unrelated replacement'})
    assert replacement['id'] != ref['claim']
    latest = client.get('/api/notebook/history/one').json()
    assert client.post('/api/notebook/history/one/restore', json={
        'revision':latest['entries'][0]['id'], 'expected':latest['expected']}).status_code == 200
    claims = {c['id']: c for c in store.load_paper('one')['claims']}
    assert claims[replacement['id']]['text'] == 'Unrelated replacement'
    assert ref['claim'] in claims


def test_legacy_sequence_recovers_deleted_identifiers_from_history(client):
    ref = seed()
    store.delete_claim('one', ref['claim'])
    legacy = store.load_paper('one')
    legacy.pop('claim_seq')
    store.write_json(store.paper_path('one'), legacy)
    assert store.add_claim('one', {'text':'New research'})['id'] != ref['claim']


@pytest.mark.parametrize('change', ['rename', 'delete'])
def test_restore_omits_obsolete_topics_and_reports_them(client, change):
    ref = seed()
    store.add_tag('testing')
    store.add_tag('methods')
    store.update_claim('one', ref['claim'], {'tags':['testing', 'methods']})
    if change == 'rename':
        store.rename_tag('testing', 'renamed')
    else:
        store.delete_tag('testing')
    state = client.get('/api/notebook/history/one').json()
    response = client.post('/api/notebook/history/one/restore', json={
        'revision':state['entries'][0]['id'], 'expected':state['expected']})
    assert response.status_code == 200
    assert response.json()['omitted_topics'] == ['testing']
    assert store.load_paper('one')['claims'][0]['tags'] == ['methods']
    assert 'testing' not in store.tag_counts(store.claim_rows())


def test_restore_keeps_valid_historical_topics(client):
    ref = seed()
    store.add_tag('testing')
    store.update_claim('one', ref['claim'], {'tags':[]})
    state = client.get('/api/notebook/history/one').json()
    response = client.post('/api/notebook/history/one/restore', json={
        'revision':state['entries'][0]['id'], 'expected':state['expected']})
    assert response.json()['omitted_topics'] == []
    assert store.load_paper('one')['claims'][0]['tags'] == ['testing']


@pytest.mark.parametrize('reviewed', [False, True])
def test_restore_wording_preserves_current_review_decision(client, reviewed):
    ref = seed()
    store.update_claim('one', ref['claim'], {'reviewed':not reviewed})
    store.update_claim('one', ref['claim'], {'text':'Revised wording'})
    store.review_claims('one', reviewed)
    state = client.get('/api/notebook/history/one').json()
    assert state['entries'][0]['fields']['reviewed'] is not reviewed
    result = client.post('/api/notebook/history/one/restore', json={
        'revision':state['entries'][0]['id'], 'expected':state['expected']})
    assert result.status_code == 200
    paper = store.load_paper('one')
    assert paper['claims'][0]['text'] == 'Original finding'
    assert paper['claims'][0]['reviewed'] is reviewed
    assert paper['status'] == ('reviewed' if reviewed else 'extracted')



def test_restoring_a_deleted_claim_can_be_undone_and_redone(client):
    ref = seed()
    store.delete_claim('one', ref['claim'])
    state = client.get('/api/notebook/history/one').json()
    content_revision = state['entries'][0]['id']
    assert client.post('/api/notebook/history/one/restore', json={
        'revision':content_revision, 'expected':state['expected']}).status_code == 200
    state = client.get('/api/notebook/history/one').json()
    deletion = state['entries'][0]
    assert deletion['claim'] == ref['claim'] and deletion['fields'] is None
    result = client.post('/api/notebook/history/one/restore', json={
        'revision':deletion['id'], 'expected':state['expected']})
    assert result.json()['deleted'] is True
    assert store.load_paper('one')['claims'] == []
    state = client.get('/api/notebook/history/one').json()
    assert state['entries'][0]['fields']['text'] == 'Original finding'
    result = client.post('/api/notebook/history/one/restore', json={
        'revision':state['entries'][0]['id'], 'expected':state['expected']})
    assert result.status_code == 200
    assert store.load_paper('one')['claims'][0]['id'] == ref['claim']
