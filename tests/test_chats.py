"""Saved conversations stay with their workspace and paper, each on its own agent."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from doxograph import chat, chats, config, server, store

PAIR = [{'role': 'user', 'content': 'How many adults?'},
        {'role': 'assistant', 'content': 'Twenty (p. 1).', 'assistant': 'Claude', 'model': 'claude-opus-5'}]
ID = 'a' * 32


@pytest.fixture
def client():
    store.save_paper(store.new_paper('study', title='Memory Study'))
    return TestClient(server.app, base_url='http://127.0.0.1:8765')


def test_conversations_save_with_revisions_and_stay_in_their_paper_and_workspace(client):
    saved = client.put(f'/api/chats/{ID}', json={'paper': 'study', 'agent': 'codex', 'messages': PAIR, 'revision': 0})
    assert saved.status_code == 200
    saved_chat = saved.json()
    assert saved_chat['revision'] == 1 and saved_chat['agent'] == 'codex' and saved_chat['title'] == 'How many adults?'
    assert saved_chat['messages'][1]['model'] == 'claude-opus-5'
    assert client.get('/api/chats?paper=study').json() == {'chats': [saved_chat], 'issues': []}
    assert client.get('/api/chats').json()['chats'] == []
    follow_up = PAIR + [{'role': 'user', 'content': 'Which ages?'}, {'role': 'assistant', 'content': '18 to 30.'}]
    update = {'paper': 'study', 'agent': 'api', 'messages': follow_up, 'revision': 1}
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
    response = client.put(f'/api/chats/{ID}', json=body | {'revision': 0})
    assert response.status_code in (404, 422)
    assert not chats.chats_dir().exists()


def test_unreadable_conversation_is_reported_without_hiding_the_others(client):
    client.put(f'/api/chats/{ID}', json={'paper': None, 'agent': 'claude_code', 'messages': PAIR, 'revision': 0})
    (chats.chats_dir() / f'{"b" * 32}.json').write_text('{not json')
    listed = client.get('/api/chats').json()
    assert [c['id'] for c in listed['chats']] == [ID]
    assert len(listed['issues']) == 1 and 'not valid JSON' in listed['issues'][0]


def test_removing_a_paper_removes_its_conversations(client):
    for chat_id, paper in ((ID, 'study'), ('b' * 32, None)):
        agent = 'api' if paper else 'claude_code'
        client.put(f'/api/chats/{chat_id}', json={'paper': paper, 'agent': agent, 'messages': PAIR, 'revision': 0})
    assert client.delete('/api/papers/study').status_code == 200
    assert sorted(p.stem for p in chats.chats_dir().glob('*.json')) == ['b' * 32]


def test_workspace_archive_includes_conversations(client):
    client.put(f'/api/chats/{ID}', json={'paper': 'study', 'agent': 'api', 'messages': PAIR, 'revision': 0})
    download = client.post('/api/workspace/archive').json()['download']
    with zipfile.ZipFile(io.BytesIO(client.get(download).content)) as archive:
        assert json.loads(archive.read(f'data/chats/{ID}.json'))['messages'][0]['content'] == 'How many adults?'
        assert f'data/chats/{ID}.json' in json.loads(archive.read('manifest.json'))['files']


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
