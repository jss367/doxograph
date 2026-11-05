"""Benchmark records share the dataset catalog's protections, link claims
through the benchmark facet, and can be made from the names claims give."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from doxograph import config, server, store


@pytest.fixture
def client():
    config.set_ai_enabled(False)
    return TestClient(server.app, base_url='http://127.0.0.1:8765')


def test_benchmark_lifecycle_keeps_its_own_file(client):
    store.save_paper(store.new_paper('study', title='A study'))
    claim = store.add_claim('study', {'text': 'A measured result'})
    body = dict(name='HarmBench', description='Harmful behavior prompts', source='https://example.org/harmbench',
                version='1.0', license='MIT', access_notes='', labels=['red teaming'], facet_values=['Harm Bench'],
                papers=['study'], claims=[{'paper': 'study', 'claim': claim['id']}],
                custom_fields=[{'name': 'Metric', 'value': 'Attack success rate'}])
    created = client.post('/api/benchmarks', json=body)
    assert created.status_code == 201
    item = created.json()
    assert {key: item[key] for key in body} == body and 'roles' not in item
    assert client.get('/api/benchmarks').json() == {'benchmarks': [item]}
    assert client.get('/api/datasets').json() == {'datasets': []}
    assert json.loads((config.data_dir() / 'benchmarks.json').read_text()) == [item]
    stale = {**body, 'revision': item['revision']}
    updated = client.put('/api/benchmarks/' + item['id'], json={**stale, 'version': '1.1'})
    assert updated.status_code == 200 and updated.json()['version'] == '1.1'
    assert client.put('/api/benchmarks/' + item['id'], json=stale).status_code == 409
    other = config.create_workspace('Separate project')
    assert client.get('/api/benchmarks', headers={'X-Doxograph-Workspace': other['id']}).json() == {'benchmarks': []}
    assert client.delete(f'/api/benchmarks/{item["id"]}?revision={updated.json()["revision"]}').status_code == 200
    assert client.get('/api/benchmarks').json() == {'benchmarks': []}


@pytest.mark.parametrize('body', [
    {'name': ' '}, {'name': 'x', 'roles': ['evaluation']}, {'name': 'x', 'papers': ['missing']},
    {'name': 'x', 'extra': 'unexpected'},
])
def test_invalid_benchmarks_do_not_write(client, body):
    response = client.post('/api/benchmarks', json=body)
    assert response.status_code == 422
    assert not (config.data_dir() / 'benchmarks.json').exists()
    if body['name'] == ' ':
        assert 'Give your benchmark a name.' in response.text


def test_records_are_made_for_names_no_record_covers(client):
    client.post('/api/benchmarks', json={'name': 'HarmBench', 'facet_values': ['Harm Bench']}).raise_for_status()
    added = client.post('/api/benchmarks/from-facets',
                        json={'names': ['AdvBench', 'harm  bench', 'HARMBENCH', 'MMLU', 'advbench']}).json()['benchmarks']
    assert [item['name'] for item in added] == ['AdvBench', 'MMLU']
    assert {item['name'] for item in client.get('/api/benchmarks').json()['benchmarks']} == {'HarmBench', 'AdvBench', 'MMLU'}
    assert client.post('/api/benchmarks/from-facets', json={'names': ['MMLU']}).json() == {'benchmarks': []}
    assert client.post('/api/benchmarks/from-facets', json={'names': [' ']}).status_code == 200
    assert len(client.get('/api/benchmarks').json()['benchmarks']) == 3


def test_archive_and_restore_include_benchmarks(client):
    store.save_paper(store.new_paper('study', title='Monitor study'))
    claim = store.add_claim('study', {'text': 'Monitors reach 0.9 AUROC'})
    store.add_claim('study', {'text': 'An unrelated result'})
    store.add_facet('benchmark', 'Benchmarks a result is measured on')
    store.update_claim('study', claim['id'], {'facets': {'benchmark': ['cua-shade-arena']}})
    item = client.post('/api/benchmarks', json={'name': 'CUA-SHADE-Arena', 'version': '2'}).json()
    client.delete(f'/api/benchmarks/{item["id"]}?revision={item["revision"]}').raise_for_status()
    backup = next(version for version in client.get('/api/workspace/backups').json()['versions']
                  if version['path'] == 'benchmarks.json')
    client.post('/api/workspace/backups/' + backup['id'] + '/restore', json={'expected': backup['expected']}).raise_for_status()
    [restored] = client.get('/api/benchmarks').json()['benchmarks']
    assert restored['name'] == 'CUA-SHADE-Arena' and restored['revision'] != item['revision']
    prepared = client.post('/api/workspace/archive').json()
    with zipfile.ZipFile(io.BytesIO(client.get(prepared['download']).content)) as archive:
        assert json.loads(archive.read('data/benchmarks.json')) == [restored]
        readable = archive.read('benchmarks.md').decode()
        assert readable.startswith('# Benchmarks\n\n## CUA-SHADE-Arena\n\nVersion: 2\n')
        assert 'Roles' not in readable
        assert '[Monitor study](papers/study.md)' in readable and 'Monitors reach 0.9 AUROC' in readable
        assert 'An unrelated result' not in readable
