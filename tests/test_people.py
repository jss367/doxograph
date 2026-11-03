"""People records gather an author's spellings, isolate workspaces, and protect edits."""
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from doxograph import config, people, store


@pytest.fixture
def client():
    from doxograph import server
    config.set_ai_enabled(False)
    return TestClient(server.app, base_url='http://127.0.0.1:8765')


def test_name_keys_ignore_case_accents_and_punctuation():
    assert people.name_key('Bernhard Schölkopf') == people.name_key('bernhard  SCHOLKOPF')
    assert people.name_key('Jean-Paul Sartre') == people.name_key('Jean Paul Sartre')
    assert people.name_key('B. Schölkopf') == 'b scholkopf'
    assert people.name_key('B. Schölkopf') != people.name_key('Bernhard Schölkopf')
    assert people.name_key(' .- ') == ''


def test_person_lifecycle_and_workspace_isolation(client):
    store.save_paper(store.new_paper('study', title='A study'))
    body = dict(name='Ada Lovelace', other_names=['A. Lovelace', 'Augusta Ada King'], affiliation='Analytical Society',
                homepage='https://example.org/ada', notes='Met at the workshop.', labels=['collaborator'], papers=['study'])
    created = client.post('/api/people', json=body)
    assert created.status_code == 201
    item = created.json()
    assert {key: item[key] for key in body} == body
    assert client.get('/api/people').json() == {'people': [item]}
    updated = client.put('/api/people/' + item['id'], json={**body, 'notes': 'Follow up.', 'revision': item['revision']})
    assert updated.status_code == 200 and updated.json()['revision'] != item['revision']
    stale = {**body, 'revision': item['revision']}
    assert client.put('/api/people/' + item['id'], json=stale).status_code == 409
    assert client.delete(f'/api/people/{item["id"]}?revision={item["revision"]}').status_code == 409
    other = config.create_workspace('Separate project')
    headers = {'X-Doxograph-Workspace': other['id']}
    assert client.get('/api/people', headers=headers).json() == {'people': []}
    assert client.put('/api/people/' + item['id'], headers=headers, json=stale).status_code == 404
    assert client.delete(f'/api/people/{item["id"]}?revision={updated.json()["revision"]}').status_code == 200
    assert client.get('/api/people').json() == {'people': []}


@pytest.mark.parametrize('body', [
    {'name': ' '}, {'name': '.'}, {'name': 'x', 'other_names': [' ']}, {'name': 'x', 'papers': ['../outside']},
    {'name': 'x', 'papers': ['missing']}, {'name': 'x', 'extra': 'unexpected'}, {'name': 'x' * 301},
    {'name': 'x', 'labels': ['two, labels']}, {'name': 'x', 'notes': 'x' * 100001},
])
def test_invalid_records_do_not_write(client, body):
    assert client.post('/api/people', json=body).status_code == 422
    assert not (config.data_dir() / 'people.json').exists()


def test_other_names_drop_repeats_and_the_name_itself(client):
    item = client.post('/api/people', json={'name': ' Ada Lovelace ', 'other_names': [
        'ada lovelace', ' A. Lovelace ', 'A Lovelace', 'Augusta King']}).json()
    assert item['name'] == 'Ada Lovelace'
    assert item['other_names'] == ['A. Lovelace', 'Augusta King']


def test_one_spelling_names_one_person(client):
    first = client.post('/api/people', json={'name': 'Ada Lovelace', 'other_names': ['A. Lovelace']}).json()
    clash = client.post('/api/people', json={'name': 'Augusta King', 'other_names': ['a lovelace']})
    assert clash.status_code == 409 and 'Ada Lovelace' in clash.json()['detail']
    second = client.post('/api/people', json={'name': 'Augusta King'}).json()
    renamed = client.put('/api/people/' + second['id'], json={'name': 'Ada Lovelace', 'revision': second['revision']})
    assert renamed.status_code == 409
    # A record may keep its own names when it is saved again.
    again = client.put('/api/people/' + first['id'], json={'name': 'Ada Lovelace', 'other_names': ['A. Lovelace'],
                                                           'revision': first['revision']})
    assert again.status_code == 200


def test_papers_are_matched_by_any_of_a_persons_names():
    papers = [{'key': 'one', 'authors': ['Ada Lovelace', 'Charles Babbage']},
              {'key': 'two', 'authors': ['A. Lovelace']},
              {'key': 'three', 'authors': ['ADA LOVELACE']},
              {'key': 'four', 'authors': None}]
    person = {'name': 'Ada Lovelace', 'other_names': ['A. Lovelace']}
    assert [paper['key'] for paper in people.authored(person, papers)] == ['one', 'two', 'three']
    assert people.author_keys(papers)['A. Lovelace'] == 'a lovelace'


def test_state_carries_the_servers_author_keys(client):
    store.save_paper(store.new_paper('study', authors=['Bernhard Schölkopf']))
    assert client.get('/api/state').json()['author_keys'] == {'Bernhard Schölkopf': 'bernhard scholkopf'}


def test_removed_papers_stay_linked_but_new_broken_links_are_rejected(client):
    store.save_paper(store.new_paper('study'))
    item = client.post('/api/people', json={'name': 'Ada', 'papers': ['study']}).json()
    store.paper_path('study').unlink()
    updated = client.put('/api/people/' + item['id'], json={'name': 'Ada', 'papers': ['study'], 'revision': item['revision']})
    assert updated.status_code == 200 and updated.json()['papers'] == ['study']


def test_parallel_writers_preserve_other_records_and_reject_stale_updates(client):
    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(lambda i: people.create_person(people.PersonBody(name=f'Person {i}')), range(8)))
    assert len(people.load()) == 8
    item = items[0]

    def update(index):
        try:
            people.update_person(item['id'], people.PersonUpdate(name=f'Edited {index}', revision=item['revision']))
            return 200
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, range(2))) == [200, 409]
    assert len(people.load()) == 8


def test_damaged_records_are_never_overwritten(client):
    path = config.data_dir() / 'people.json'
    path.write_text('[{"name": "Incomplete record"}]')
    assert client.get('/api/people').status_code == 503
    assert client.post('/api/people', json={'name': 'New record'}).status_code == 503
    assert json.loads(path.read_text()) == [{'name': 'Incomplete record'}]


def test_retrying_creation_returns_the_record_without_overwriting_edits(client):
    headers = {'Idempotency-Key': 'a' * 32}
    item = client.post('/api/people', headers=headers, json={'name': 'Ada'}).json()
    edited = client.put('/api/people/' + item['id'], json={'name': 'Ada Lovelace', 'revision': item['revision']}).json()
    assert client.post('/api/people', headers=headers, json={'name': 'Ada'}).json() == edited
    assert client.get('/api/people').json()['people'] == [edited]


def test_archive_and_restore_include_people_and_invalidate_old_editors(client):
    store.save_paper(store.new_paper('engine', title='Notes on the engine', authors=['A. Lovelace'], year=1843))
    store.save_paper(store.new_paper('review', title='A review'))
    item = client.post('/api/people', json={'name': 'Ada Lovelace', 'other_names': ['A. Lovelace'],
                                            'affiliation': 'Analytical Society', 'notes': 'Ask about *Note G*.',
                                            'labels': ['collaborator'], 'papers': ['review']}).json()
    revised = client.put('/api/people/' + item['id'], json={**people.PersonBody.model_validate(
        {key: item[key] for key in people.PersonBody.model_fields}).model_dump(), 'name': 'Ada King', 'revision': item['revision']}).json()
    client.delete(f'/api/people/{item["id"]}?revision={revised["revision"]}').raise_for_status()
    backup = next(version for version in client.get('/api/workspace/backups').json()['versions']
                  if version['path'] == 'people.json' and any(
                      record['revision'] == item['revision'] for record in client.get('/api/workspace/backups/' + version['id']).json()))
    client.post('/api/workspace/backups/' + backup['id'] + '/restore', json={'expected': backup['expected']}).raise_for_status()
    [restored] = client.get('/api/people').json()['people']
    assert restored['name'] == 'Ada Lovelace' and restored['revision'] not in (item['revision'], revised['revision'])
    assert client.put('/api/people/' + item['id'], json={'name': 'Stale edit', 'revision': item['revision']}).status_code == 409
    prepared = client.post('/api/workspace/archive').json()
    with zipfile.ZipFile(io.BytesIO(client.get(prepared['download']).content)) as archive:
        assert json.loads(archive.read('data/people.json')) == [restored]
        assert archive.read('people.md').decode() == '\n'.join([
            '# People', '', '## Ada Lovelace', '', 'Other names: A. Lovelace', '', 'Affiliation: Analytical Society', '',
            'Labels: collaborator', '', '### My notes', '', 'Ask about *Note G*.', '',
            '### Papers', '', '- [Notes on the engine](papers/engine.md) (1843)', '',
            '### Other papers', '', '- [A review](papers/review.md) (year unknown)', ''])
        assert '[People](people.md)' in archive.read('index.md').decode()


def test_a_file_giving_one_spelling_to_two_people_is_never_overwritten(client):
    first = client.post('/api/people', json={'name': 'Ada Lovelace'}).json()
    second = client.post('/api/people', json={'name': 'Augusta King'}).json()
    path = config.data_dir() / 'people.json'
    # A hand edit gives the first record's name to the second.
    damaged = [first, {**second, 'other_names': ['ADA lovelace']}]
    path.write_text(json.dumps(damaged))
    assert client.get('/api/people').status_code == 503
    assert client.put('/api/people/' + first['id'], json={'name': 'Ada', 'revision': first['revision']}).status_code == 503
    assert json.loads(path.read_text()) == damaged


def test_a_linked_paper_the_person_wrote_is_listed_once(client):
    store.save_paper(store.new_paper('engine', title='Notes on the engine', authors=['Ada Lovelace'], year=1843))
    client.post('/api/people', json={'name': 'Ada Lovelace', 'papers': ['engine']}).raise_for_status()
    prepared = client.post('/api/workspace/archive').json()
    with zipfile.ZipFile(io.BytesIO(client.get(prepared['download']).content)) as archive:
        readable = archive.read('people.md').decode()
    assert readable.count('papers/engine.md') == 1 and '### Other papers' not in readable
