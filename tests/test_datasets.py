"""Dataset records preserve provenance, isolate workspaces, and protect edits."""
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from doxograph import config, datasets, server, store


@pytest.fixture
def client():
    config.set_ai_enabled(False)
    return TestClient(server.app, base_url='http://127.0.0.1:8765')


def test_dataset_lifecycle_and_workspace_isolation(client):
    store.save_paper(store.new_paper('study', title='A study'))
    claim = store.add_claim('study', {'text': 'A measured result'})
    body = dict(name='Evaluation corpus', description='Held-out examples', source='/data/evaluation.parquet',
                version='2026-10', license='CC BY 4.0', access_notes='Request access from the owner',
                roles=['evaluation', 'reference'], facet_values=['Eval corpus'], papers=['study'],
                claims=[{'paper': 'study', 'claim': claim['id']}])
    created = client.post('/api/datasets', json=body)
    assert created.status_code == 201
    item = created.json()
    assert {key: item[key] for key in body} == body
    assert client.get('/api/datasets').json() == {'datasets': [item]}
    updated = client.put('/api/datasets/' + item['id'], json={**body, 'version': '2026-11', 'revision': item['revision']})
    assert updated.status_code == 200 and updated.json()['revision'] != item['revision']
    stale = {**body, 'revision': item['revision']}
    assert client.put('/api/datasets/' + item['id'], json=stale).status_code == 409
    assert client.delete(f'/api/datasets/{item["id"]}?revision={item["revision"]}').status_code == 409
    other = config.create_workspace('Separate project')
    headers = {'X-Doxograph-Workspace': other['id']}
    assert client.get('/api/datasets', headers=headers).json() == {'datasets': []}
    assert client.put('/api/datasets/' + item['id'], headers=headers, json=stale).status_code == 404
    assert client.delete(f'/api/datasets/{item["id"]}?revision={updated.json()["revision"]}').status_code == 200
    assert client.get('/api/datasets').json() == {'datasets': []}
    assert store.load_paper('study')['claims'][0]['id'] == claim['id']


@pytest.mark.parametrize('body', [
    {'name': ' '}, {'name': 'x', 'roles': ['unknown']}, {'name': 'x', 'facet_values': [' ']},
    {'name': 'x', 'papers': ['../outside']}, {'name': 'x', 'papers': ['missing']},
    {'name': 'x', 'claims': [{'paper': 'missing', 'claim': 'one'}]},
    {'name': 'x', 'extra': 'unexpected'}, {'name': 'x' * 301},
    {'name': 'x', 'labels': ['two, labels']}, {'name': 'x', 'labels': ['x' * 301]},
    {'name': 'x', 'labels': 'not a list'}, {'name': 'x', 'labels': [None]},
    {'name': 'x', 'custom_fields': [{'name': ' ', 'value': 'GPT-4o'}]},
    {'name': 'x', 'custom_fields': [{'name': 'Model', 'value': 'a'}, {'name': ' MODEL ', 'value': 'b'}]},
    {'name': 'x', 'custom_fields': [{'name': 'Model', 'value': 'a', 'extra': 'unexpected'}]},
    {'name': 'x', 'custom_fields': [{'name': 'x' * 101}]},
])
def test_invalid_records_do_not_write(client, body):
    assert client.post('/api/datasets', json=body).status_code == 422
    assert not (config.data_dir() / 'datasets.json').exists()


def test_dataset_labels_normalize_and_can_be_replaced_or_removed(client):
    item = client.post('/api/datasets', json={'name': 'Corpus', 'labels': [
        ' Open\tsource ', 'OPEN SOURCE', 'needs review', '', '  ', 'Straße', 'STRASSE',
    ]}).json()
    assert item['labels'] == ['needs review', 'Open source', 'Straße']
    assert datasets.load()[0]['labels'] == item['labels']
    edited = client.put('/api/datasets/' + item['id'], json={
        'name': 'Corpus', 'revision': item['revision'], 'labels': ['reviewed'],
    }).json()
    assert edited['labels'] == ['reviewed']
    cleared = client.put('/api/datasets/' + item['id'], json={
        'name': 'Corpus', 'revision': edited['revision'], 'labels': [],
    }).json()
    assert cleared['labels'] == []


def test_existing_datasets_without_labels_remain_editable(client):
    item = client.post('/api/datasets', json={'name': 'Older corpus'}).json()
    del item['labels']
    (config.data_dir() / 'datasets.json').write_text(json.dumps([item]))
    assert client.get('/api/datasets').json()['datasets'][0]['labels'] == []
    edited = client.put('/api/datasets/' + item['id'], json={
        'name': item['name'], 'revision': item['revision'], 'labels': ['archived'],
    })
    assert edited.status_code == 200
    assert edited.json()['labels'] == ['archived']


def test_custom_fields_record_details_the_catalog_does_not_name(client):
    item = client.post('/api/datasets', json={'name': 'Synthetic dialogues', 'custom_fields': [
        {'name': ' Model ', 'value': 'claude-opus-5-5'}, {'name': 'Prompt set'},
    ]}).json()
    assert item['custom_fields'] == [{'name': 'Model', 'value': 'claude-opus-5-5'}, {'name': 'Prompt set', 'value': ''}]
    assert datasets.load()[0]['custom_fields'] == item['custom_fields']
    edited = client.put('/api/datasets/' + item['id'], json={
        'name': item['name'], 'revision': item['revision'], 'custom_fields': [{'name': 'Model', 'value': 'gpt-5'}],
    }).json()
    assert edited['custom_fields'] == [{'name': 'Model', 'value': 'gpt-5'}]


def test_existing_datasets_without_custom_fields_remain_editable(client):
    item = client.post('/api/datasets', json={'name': 'Older corpus'}).json()
    del item['custom_fields']
    (config.data_dir() / 'datasets.json').write_text(json.dumps([item]))
    assert client.get('/api/datasets').json()['datasets'][0]['custom_fields'] == []
    edited = client.put('/api/datasets/' + item['id'], json={
        'name': item['name'], 'revision': item['revision'], 'custom_fields': [{'name': 'Model', 'value': 'gpt-5'}],
    })
    assert edited.status_code == 200


def test_removed_references_are_preserved_but_new_broken_links_are_rejected(client):
    store.save_paper(store.new_paper('study'))
    claim = store.add_claim('study', {'text': 'Original finding'})
    assert client.post('/api/datasets', json={'name': 'Corpus', 'claims': [{'paper': 'study', 'claim': 'missing'}]}).status_code == 422
    body = {'name': 'Corpus', 'papers': ['study'], 'claims': [{'paper': 'study', 'claim': claim['id']}]}
    item = client.post('/api/datasets', json=body).json()
    store.paper_path('study').unlink()
    updated = client.put('/api/datasets/' + item['id'], json={**body, 'description': 'Still useful', 'revision': item['revision']})
    assert updated.status_code == 200
    assert updated.json()['claims'] == body['claims'] and updated.json()['papers'] == ['study']


def test_parallel_writers_preserve_other_records_and_reject_stale_updates(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda i: datasets.create_dataset(datasets.DatasetBody(name=f'Corpus {i}')), range(8)))
    assert len(datasets.load()) == 8
    item = items[0]

    def update(index):
        try:
            datasets.update_dataset(item['id'], datasets.DatasetUpdate(name=f'Edited {index}', revision=item['revision']))
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == [200, 409]
    assert len(datasets.load()) == 8


def test_damaged_catalog_is_never_overwritten(client):
    path = config.data_dir() / 'datasets.json'
    path.write_text('[{"name": "Incomplete record"}]')
    assert client.get('/api/datasets').status_code == 503
    assert client.post('/api/datasets', json={'name': 'New record'}).status_code == 503
    assert json.loads(path.read_text()) == [{'name': 'Incomplete record'}]


def test_retrying_creation_returns_the_record_without_overwriting_edits(client):
    headers = {'Idempotency-Key': 'a' * 32}
    body = {'name': 'Corpus'}
    item = client.post('/api/datasets', headers=headers, json=body).json()
    edited = client.put('/api/datasets/' + item['id'], json={'name': 'Edited corpus', 'revision': item['revision']}).json()
    assert client.post('/api/datasets', headers=headers, json=body).json() == edited
    assert client.get('/api/datasets').json()['datasets'] == [edited]


def test_archive_and_restore_include_datasets_and_invalidate_old_editors(client):
    store.save_paper(store.new_paper('study', title='Dataset evaluation study'))
    claim = store.add_claim('study', {'text': 'A matched result'})
    store.add_facet('dataset', 'Datasets used')
    store.update_claim('study', claim['id'], {'facets': {'dataset': ['EVAL corpus']}})
    item = client.post('/api/datasets', json={'name': 'Evaluation corpus', 'facet_values': ['Eval corpus'],
                                            'labels': ['open source', 'needs review'],
                                            'source': '/data/evaluation.parquet', 'license': 'CC BY 4.0',
                                            'custom_fields': [{'name': 'Model', 'value': 'gpt-5'}, {'name': 'Seed'}]}).json()
    revised = client.put('/api/datasets/' + item['id'], json={**datasets.DatasetBody.model_validate(
        {key: item[key] for key in datasets.DatasetBody.model_fields}).model_dump(), 'name': 'Revised corpus', 'revision': item['revision']}).json()
    client.delete(f'/api/datasets/{item["id"]}?revision={revised["revision"]}').raise_for_status()
    backup = next(version for version in client.get('/api/workspace/backups').json()['versions']
                  if version['path'] == 'datasets.json' and any(
                      record['revision'] == item['revision'] for record in client.get('/api/workspace/backups/' + version['id']).json()))
    client.post('/api/workspace/backups/' + backup['id'] + '/restore', json={'expected': backup['expected']}).raise_for_status()
    [restored] = client.get('/api/datasets').json()['datasets']
    assert restored['name'] == item['name'] and restored['revision'] not in (item['revision'], revised['revision'])
    assert restored['labels'] == ['needs review', 'open source']
    assert client.put('/api/datasets/' + item['id'], json={'name': 'Stale edit', 'revision': item['revision']}).status_code == 409
    prepared = client.post('/api/workspace/archive').json()
    with zipfile.ZipFile(io.BytesIO(client.get(prepared['download']).content)) as archive:
        assert json.loads(archive.read('data/datasets.json')) == [restored]
        readable = archive.read('datasets.md').decode()
        assert '/data/evaluation.parquet' in readable and 'CC BY 4.0' in readable
        assert 'Labels: needs review, open source' in readable
        assert 'Model: gpt-5' in readable and 'Seed' not in readable
        assert '[Dataset evaluation study](papers/study.md)' in readable and 'A matched result' in readable
        assert '[Project datasets](datasets.md)' in archive.read('index.md').decode()
