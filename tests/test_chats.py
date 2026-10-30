"""Saved conversations stay with their workspace and paper, each on its own agent."""
import io
import json
import os
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from doxograph import chat, chats, config, server, storage, store

PAIR = [{'role': 'user', 'content': 'How many adults?'},
        {'role': 'assistant', 'content': 'Twenty (p. 1).', 'assistant': 'Claude', 'model': 'claude-opus-5'}]
ID = 'a' * 32


def op():
    return uuid.uuid4().hex


@pytest.fixture
def client():
    store.save_paper(store.new_paper('study', title='Memory Study'))
    return TestClient(server.app, base_url='http://127.0.0.1:8765')


def test_conversations_save_with_revisions_and_stay_in_their_paper_and_workspace(client):
    saved = client.put(f'/api/chats/{ID}', json={'paper': 'study', 'agent': 'codex', 'messages': PAIR, 'revision': 0, 'operation': op()})
    assert saved.status_code == 200
    saved_chat = saved.json()
    assert saved_chat['revision'] == 1 and saved_chat['agent'] == 'codex' and saved_chat['title'] == 'How many adults?'
    assert saved_chat['messages'][1]['model'] == 'claude-opus-5'
    assert client.get('/api/chats?paper=study').json() == {'chats': [saved_chat], 'issues': []}
    assert client.get('/api/chats').json()['chats'] == []
    follow_up = PAIR + [{'role': 'user', 'content': 'Which ages?'}, {'role': 'assistant', 'content': '18 to 30.'}]
    update = {'paper': 'study', 'agent': 'api', 'messages': follow_up, 'revision': 1, 'operation': op()}
    assert client.put(f'/api/chats/{ID}', json=update).json()['revision'] == 2
    # A second window that saw revision 1 cannot replace the newer conversation.
    assert client.put(f'/api/chats/{ID}', json=update).status_code == 409
    assert client.put(f'/api/chats/{ID}', json=update | {'paper': None, 'revision': 2}).status_code == 422
    workspace = config.create_workspace('Elsewhere')
    headers = {'X-Doxograph-Workspace': workspace['id']}
    assert client.get('/api/chats?paper=study', headers=headers).json()['chats'] == []
    assert client.delete(f'/api/chats/{ID}?revision=1').status_code == 409
    assert client.delete(f'/api/chats/{ID}?revision=2').json() == {'deleted': ID}
    assert client.put(f'/api/chats/{ID}', json=update | {'revision': 2}).status_code == 404


@pytest.mark.parametrize('body', [
    {'paper': None, 'agent': 'api', 'messages': PAIR},
    {'paper': 'missing', 'agent': 'api', 'messages': PAIR},
    {'paper': 'study', 'agent': 'api', 'messages': PAIR[:1]},
    {'paper': 'study', 'agent': 'api', 'messages': PAIR[::-1]},
    {'paper': 'study', 'agent': 'api', 'messages': [PAIR[0] | {'saving': True}, PAIR[1]]},
    {'paper': '../study', 'agent': 'api', 'messages': PAIR},
])
def test_conversations_must_be_answered_and_belong_to_a_real_chat(client, body):
    response = client.put(f'/api/chats/{ID}', json=body | {'revision': 0, 'operation': op()})
    assert response.status_code in (404, 422)
    assert not chats.chats_dir().exists()


def test_unreadable_conversation_is_reported_without_hiding_the_others(client):
    client.put(f'/api/chats/{ID}', json={'paper': None, 'agent': 'claude_code', 'messages': PAIR, 'revision': 0, 'operation': op()})
    (chats.chats_dir() / f'{"b" * 32}.json').write_text('{not json')
    listed = client.get('/api/chats').json()
    assert [c['id'] for c in listed['chats']] == [ID]
    assert len(listed['issues']) == 1 and 'not valid JSON' in listed['issues'][0]


def test_removing_a_paper_preserves_its_conversations(client):
    for chat_id, paper in ((ID, 'study'), ('b' * 32, None)):
        agent = 'api' if paper else 'claude_code'
        client.put(f'/api/chats/{chat_id}', json={'paper': paper, 'agent': agent, 'messages': PAIR, 'revision': 0, 'operation': op()})
    assert client.delete('/api/papers/study').status_code == 200
    assert sorted(p.stem for p in chats.chats_dir().glob('*.json')) == [ID, 'b' * 32]


def test_a_save_whose_response_was_lost_still_counts_as_this_pages_own(client):
    first, second = op(), op()
    body = {'paper': 'study', 'agent': 'api', 'messages': PAIR, 'revision': 0}
    assert client.put(f'/api/chats/{ID}', json=body | {'operation': first}).json()['revision'] == 1
    # The page never saw revision 1, so it saves from revision 0 and names the unconfirmed write.
    follow_up = body | {'messages': PAIR * 2, 'operation': second, 'unconfirmed': [first]}
    saved = client.put(f'/api/chats/{ID}', json=follow_up)
    assert saved.status_code == 200 and saved.json()['revision'] == 2 and len(saved.json()['messages']) == 4
    # An unconfirmed write that is no longer the newest does not hide another window's save.
    assert client.put(f'/api/chats/{ID}', json=follow_up | {'operation': op()}).status_code == 409
    assert client.put(f'/api/chats/{ID}', json=body | {'operation': op()}).status_code == 409


def test_workspace_archive_includes_conversations_of_the_papers_it_holds(client):
    library, removed = 'b' * 32, 'c' * 32
    client.put(f'/api/chats/{ID}', json={'paper': 'study', 'agent': 'api', 'messages': PAIR, 'revision': 0, 'operation': op()})
    client.put(f'/api/chats/{library}', json={'agent': 'codex', 'messages': PAIR, 'revision': 0, 'operation': op()})
    store.save_paper(store.new_paper('gone'))
    client.put(f'/api/chats/{removed}', json={'paper': 'gone', 'agent': 'api', 'messages': PAIR, 'revision': 0, 'operation': op()})
    # A paper removed while the archive waited, before its conversations were deleted.
    store.paper_path('gone').unlink()
    download = client.post('/api/workspace/archive').json()['download']
    with zipfile.ZipFile(io.BytesIO(client.get(download).content)) as archive:
        assert json.loads(archive.read(f'data/chats/{ID}.json'))['messages'][0]['content'] == 'How many adults?'
        names = set(json.loads(archive.read('manifest.json'))['files'])
        assert {f'data/chats/{ID}.json', f'data/chats/{library}.json'} <= names
        assert f'data/chats/{removed}.json' in names


@pytest.mark.parametrize('agent', ['api', 'claude_code', 'codex'])
def test_paper_chat_answers_with_the_conversation_agent(client, monkeypatch, agent):
    config.set_ai_enabled(True)
    config.set_ai_provider('api')
    seen = []

    def answer(key, messages):
        seen.append(config.ai_provider())
        return {'answer': 'Twenty.', 'model': 'test', 'assistant': 'Claude'}

    monkeypatch.setattr(chat, 'answer', config.bind_model_access(answer))
    response = client.post('/api/papers/study/chat', json={'messages': PAIR[:1], 'agent': agent})
    assert response.status_code == 200
    assert client.post('/api/papers/study/chat', json={'messages': PAIR[:1]}).status_code == 200
    assert seen == [agent, 'api']


def test_links_and_special_files_among_conversations_are_never_read(client):
    client.put(f'/api/chats/{ID}', json={'agent': 'codex', 'messages': PAIR, 'revision': 0, 'operation': op()})
    (chats.chats_dir() / f'{"b" * 32}.json').symlink_to(chats.chats_dir() / 'missing.json')
    os.mkfifo(chats.chats_dir() / f'{"c" * 32}.json')
    assert [c['id'] for c in client.get('/api/chats').json()['chats']] == [ID]
    download = client.post('/api/workspace/archive').json()['download']
    with zipfile.ZipFile(io.BytesIO(client.get(download).content)) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert f'data/chats/{ID}.json' in manifest['files']
        assert {f'chats/{"b" * 32}.json', f'chats/{"c" * 32}.json'} <= set(manifest['skipped'])


def test_listing_reads_conversations_while_holding_the_lock_deletion_takes(client, monkeypatch):
    client.put(f'/api/chats/{ID}', json={'agent': 'codex', 'messages': PAIR, 'revision': 0, 'operation': op()})
    read, blocked = storage.read_model, []

    def read_while_deleting(path, model):
        # Deletion from another thread must wait until the listing has read every file.
        with ThreadPoolExecutor(1) as other:
            def try_delete_lock():
                try:
                    with store.document_lock('chats', blocking=False):
                        return False
                except BlockingIOError:
                    return True
            blocked.append(other.submit(try_delete_lock).result())
        return read(path, model)

    monkeypatch.setattr(storage, 'read_model', read_while_deleting)
    assert [c['id'] for c in client.get('/api/chats').json()['chats']] == [ID]
    assert blocked == [True]


def test_conversations_saved_within_one_second_list_newest_first(client, monkeypatch):
    # Every save lands in one second: whole-second stamps tie, so only finer ones order them.
    second = datetime(2026, 10, 30, 5, 0, 0, tzinfo=timezone.utc)
    ticks = iter(range(1, 1000))

    class Clock:
        @staticmethod
        def now(tz=None):
            return second.replace(microsecond=next(ticks))

    monkeypatch.setattr(chats, 'datetime', Clock)
    monkeypatch.setattr(store, 'now', lambda: second.isoformat(timespec='seconds'))
    ids = ['b' * 32, 'c' * 32, 'd' * 32]
    for chat_id in ids:
        client.put(f'/api/chats/{chat_id}', json={'agent': 'codex', 'messages': PAIR, 'revision': 0, 'operation': op()})
    # Update the first one last; it must now lead whatever its ID.
    client.put(f'/api/chats/{ids[0]}', json={'agent': 'codex', 'messages': PAIR * 2, 'revision': 1, 'operation': op()})
    assert [c['id'] for c in client.get('/api/chats').json()['chats']] == [ids[0], ids[2], ids[1]]
