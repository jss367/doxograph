"""Long-form notes remain workspace-local and protect concurrent writing."""
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from doxograph import config, research_notes
from doxograph.server import app


@pytest.fixture
def client():
    config.set_ai_enabled(False)
    return TestClient(app, base_url='http://127.0.0.1:8765')


def test_note_persistence_revisions_and_workspace_isolation(client):
    body = '## Evidence\n\n$\\alpha^2$\n\n| Paper | Result |\n| --- | --- |\n| One | 2 |'
    created = client.post('/api/research-notes', json={'title':'A research argument','body':body})
    assert created.status_code == 201
    note = created.json()
    assert len(note['id']) == 32 and note['body'] == body and note['revision'] == 1
    assert client.get('/api/research-notes').json()['notes'] == [note]
    other = client.post('/api/research-notes',json={'title':'Another note'}).json()
    update = {'title':'Revised argument','body':body + '\nMore detail.', 'revision':1}
    saved = client.put('/api/research-notes/' + note['id'],json=update)
    assert saved.status_code == 200 and saved.json()['revision'] == 2
    assert client.put('/api/research-notes/' + note['id'],json=update).status_code == 409
    assert client.delete('/api/research-notes/' + note['id'] + '?revision=1').status_code == 409
    assert client.get('/api/research-notes').json()['notes'][1] == other
    workspace = config.create_workspace('Separate research')
    headers = {'X-Doxograph-Workspace':workspace['id']}
    assert client.get('/api/research-notes',headers=headers).json() == {'notes':[]}
    assert client.put('/api/research-notes/' + note['id'],headers=headers,json=update).status_code == 404
    export = client.get('/api/research-notes/' + note['id'] + '/export')
    assert export.text == '# Revised argument\n\n' + update['body']
    assert client.delete('/api/research-notes/' + note['id'] + '?revision=2').status_code == 200
    assert client.get('/api/research-notes').json()['notes'] == [other]


def image_bytes():
    output = io.BytesIO()
    Image.new('RGB',(80,40),'#377e9e').save(output,format='PNG')
    return output.getvalue()


def test_local_images_are_validated_and_workspace_scoped(client):
    content = image_bytes()
    response = client.post('/api/research-notes/images',files={'file':('figure.png',content,'image/png')})
    assert response.status_code == 201
    url = response.json()['url']
    assert '?workspace=default' in url
    assert client.get(url).content == content
    assert client.get(url).headers['content-type'] == 'image/png'
    assert client.post('/api/research-notes/images',files={'file':('same.png',content,'image/png')}).json()['url'] == url
    workspace = config.create_workspace('Other notebook')
    assert client.get(url.replace('workspace=default','workspace='+workspace['id'])).status_code == 404
    assert client.post('/api/research-notes/images',files={'file':('figure.svg',b'<svg onload="alert(1)"></svg>','image/svg+xml')}).status_code == 422
    assert client.post('/api/research-notes/images',files={'file':('fake.png',b'not an image','image/png')}).status_code == 422
    assert client.post('/api/research-notes/images',files={'file':('large.png',b'x'*(research_notes.MAX_IMAGE_BYTES+1),'image/png')}).status_code == 413


def test_parallel_writers_preserve_notes_and_reject_stale_edits(client):
    def create(index):
        return research_notes.create_note(research_notes.NoteBody(title=f'Note {index}'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        notes = list(pool.map(create,range(8)))
    assert len(research_notes.load()) == 8
    note = notes[0]
    def update(index):
        try:
            return research_notes.update_note(note['id'],research_notes.NoteUpdate(title=f'Edit {index}',revision=1))['revision']
        except HTTPException as error:
            return error.status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update,range(2))) == [2,409]
    assert len(research_notes.load()) == 8


def test_exports_make_research_links_absolute_and_preserve_drafts_and_code(client):
    links = ('[Note](/?note=' + 'a'*32 + '#ws=default)\n'
             '[Paper](/#ws=default&paper=one)\n'
             '![Figure](/api/research-notes/images/' + 'b'*64 + '.png?workspace=default)\n'
             '[reference]: /?note=' + 'a'*32 + '#ws=default\n')
    literal = ('`[Example](/#paper=one)`\n\n'
               '```markdown\n[Example](/#paper=one)\n```\n\n'
               '    [Example](/#paper=one)\n\n'
               '> ~~~markdown\n> [Example](/#paper=one)\n> ~~~\n')
    body = links + '\n[Web](https://example.org/paper)\n\n' + literal
    note = client.post('/api/research-notes',json={'title':'Saved argument','body':body}).json()
    exported = client.get('/api/research-notes/'+note['id']+'/export')
    expected = links.replace('](/','](http://127.0.0.1:8765/').replace(']: /',']: http://127.0.0.1:8765/')
    assert exported.text == '# Saved argument\n\n' + expected + '\n[Web](https://example.org/paper)\n\n' + literal
    draft = client.post('/api/research-notes/export',json={'title':'Unsaved argument','body':body+'\nMore writing.'})
    assert draft.status_code == 200
    assert draft.text.startswith('# Unsaved argument\n\n'+expected)
    assert draft.text.endswith('More writing.')
    assert client.get('/api/research-notes').json()['notes'] == [note]


def test_export_distinguishes_list_links_from_indented_and_fenced_examples(client):
    body = ('- Evidence:\n\n    [Paper](/#paper=one)\n\n'
            '  ```markdown\n  [Example](/#paper=one)\n  ```\n\n'
            '      [Example](/#paper=one)\n')
    response = client.post('/api/research-notes/export',json={'title':'Lists','body':body})
    assert response.text == '# Lists\n\n'+body.replace('[Paper](/#paper=one)','[Paper](http://127.0.0.1:8765/#paper=one)')


@pytest.mark.parametrize('payload',[{'title':' '},{'title':'x','body':'x'*2_000_001},{'title':'x','extra':'unexpected'}])
def test_invalid_notes_are_rejected_without_writing(client,payload):
    assert client.post('/api/research-notes',json=payload).status_code == 422
    assert client.get('/api/research-notes').json()['notes'] == []
