import base64
import io
import json

import pytest
from PIL import Image

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
    monkeypatch.setattr(project_overview.extract.providers,'create',respond)
    response = client.post('/api/project-overview/detect',json={'image':image})
    assert response.status_code == 200
    assert response.json()['sections'][0]['body'] == ''
    assert captured['messages'][0]['content'][0]['type'] == 'image'
    assert len(captured['messages'][0]['content']) == 1


@pytest.mark.parametrize("image_format", ["PNG", "WEBP", "GIF"])
def test_detection_composites_transparency_on_white(monkeypatch, image_format):
    image = Image.new("RGBA", (80, 40), (0, 0, 0, 0))
    for x in range(30, 50):
        for y in range(10, 30):
            image.putpixel((x, y), (0, 0, 0, 255))
    output = io.BytesIO()
    image.save(output, format=image_format)
    sent = detect_image(monkeypatch, output.getvalue())
    assert all(channel > 245 for channel in sent.getpixel((5, 5)))
    assert all(channel < 10 for channel in sent.getpixel((40, 20)))


@pytest.mark.parametrize("orientation", [2, 6, 8])
def test_detection_honors_exif_orientation(monkeypatch, orientation):
    image = Image.new("RGB", (80, 40), "white")
    for x in range(40):
        for y in range(40):
            image.putpixel((x, y), (0, 0, 0))
    exif = Image.Exif()
    exif[274] = orientation
    output = io.BytesIO()
    image.save(output, format="JPEG", exif=exif)
    sent = detect_image(monkeypatch, output.getvalue())
    assert sent.size == ((80, 40) if orientation == 2 else (40, 80))
    first = sent.getpixel((10, 10))[0]
    last = sent.getpixel((sent.width - 10, sent.height - 10))[0]
    assert (first < 10 and last > 245) if orientation == 6 else (first > 245 and last < 10)


def detect_image(monkeypatch, content):
    config.set_ai_enabled(True)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    url = client.post("/api/research-notes/images", files={"file": ("diagram", content)}).json()["url"]
    captured = {}
    monkeypatch.setattr(project_overview.extract, "client", lambda: object())
    def respond(*args, **kwargs):
        captured.update(kwargs)
        return ModelResponse.from_text('{"sections":[]}')
    monkeypatch.setattr(project_overview.extract.providers, "create", respond)
    result = client.post("/api/project-overview/detect", json={"image": url.split("/")[-1].split("?")[0]})
    assert result.status_code == 200
    encoded = captured["messages"][0]["content"][0]["source"]["data"]
    return Image.open(io.BytesIO(base64.b64decode(encoded)))


def test_detection_rechecks_ai_after_waiting_for_shared_slot(monkeypatch):
    config.set_ai_enabled(True)
    client = TestClient(app, base_url="http://127.0.0.1:8765")
    url = client.post("/api/research-notes/images", files={"file": ("diagram.png", image_bytes())}).json()["url"]
    monkeypatch.setattr(project_overview.extract, "client", lambda: object())
    entered = []
    class DisableWhileWaiting:
        def __enter__(self):
            entered.append(True)
            config.set_ai_enabled(False)
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(project_overview.extract, "_pass_slots", DisableWhileWaiting())
    def forbidden(*args, **kwargs):
        pytest.fail("Provider must not receive a diagram after AI is disabled")
    monkeypatch.setattr(project_overview.extract.providers, "create", forbidden)
    with pytest.raises(RuntimeError, match="AI analysis is disabled"):
        client.post("/api/project-overview/detect", json={"image": url.split("/")[-1].split("?")[0]})
    assert entered == [True]
