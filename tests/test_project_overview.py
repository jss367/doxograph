import json

from fastapi.testclient import TestClient

from doxograph import config, project_overview, research_notes, workspace_archive
from doxograph.analysis_types import ModelResponse
from doxograph.server import app
from test_research_notes import image_bytes


def test_project_roundtrip_conflicts_and_export():
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    image = client.post('/api/research-notes/images', files={'file':('diagram.png',image_bytes(),'image/png')}).json()['url'].split('/')[-1].split('?')[0]
    project = {'image':image, 'sections':[{'title':'Attack prompt','x':.1,'y':.2,'width':.3,'height':.4,'body':'My **notes**'}]}
    note = client.post('/api/research-notes', json={'title':'Project','project':project}).json()
    assert research_notes.load()[0]['project'] == project
    payload = {'title':'Project','body':'','revision':note['revision'],'project':project}
    assert client.put('/api/research-notes/'+note['id'],json=payload).status_code == 200
    assert client.put('/api/research-notes/'+note['id'],json=payload).status_code == 409
    assert 'Attack prompt' in client.get('/api/research-notes/'+note['id']+'/export').text
    import zipfile
    archive = workspace_archive.build()
    try:
        with zipfile.ZipFile(archive) as z:
            notes = [name for name in z.namelist() if name.startswith('notes/')]
            assert 'My **notes**' in z.read(notes[0]).decode()
            assert any(image in name for name in z.namelist())
    finally:
        archive.unlink()
    other = config.create_workspace('Other')
    assert client.get('/api/research-notes', headers={'X-Doxograph-Workspace':other['id']}).json()['notes'] == []
    project['sections'][0]['width'] = 1
    assert client.post('/api/research-notes', json={'title':'Invalid','project':project}).status_code == 422


def test_detection_uses_image_only_and_honors_ai_switch(monkeypatch):
    client = TestClient(app, base_url='http://127.0.0.1:8765')
    image = client.post('/api/research-notes/images',files={'file':('diagram.png',image_bytes(),'image/png')}).json()['url'].split('/')[-1].split('?')[0]
    config.set_ai_enabled(False)
    assert client.post('/api/project-overview/detect',json={'image':image}).status_code == 409
    config.set_ai_enabled(True)
    monkeypatch.setattr(project_overview.extract,'client',lambda: object())
    captured = {}
    def respond(*args, **kwargs):
        captured.update(kwargs)
        return ModelResponse.from_text(json.dumps({'sections':[{'title':'Attack prompt','x':.1,'y':.2,'width':.2,'height':.2,'body':'Generated text'}]}))
    monkeypatch.setattr(project_overview.providers,'create',respond)
    response = client.post('/api/project-overview/detect',json={'image':image})
    assert response.status_code == 200
    assert response.json()['sections'][0]['body'] == ''
    assert captured['messages'][0]['content'][0]['type'] == 'image'
    assert len(captured['messages'][0]['content']) == 1
