"""Exercise local Loupe launch, process ownership, and dataset boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from doxograph import loupe, loupe_runner, server


@pytest.fixture(autouse=True)
def local_loupe(tmp_path, monkeypatch):
    checkout = tmp_path / 'Loupe checkout'
    checkout.mkdir()
    # A real child process exercises argument handling, readiness, bind errors,
    # and cleanup without requiring a second repository in CI.
    (checkout / 'server.py').write_text('''
import argparse, json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
def make_handler():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    return Handler
def main():
    p = argparse.ArgumentParser()
    for flag in ('survey', 'conversations', 'data-dir', 'port'):
        p.add_argument('--' + flag)
    a = p.parse_args()
    mode = next(k for k in ('survey', 'conversations', 'data_dir') if getattr(a, k))
    source = getattr(a, mode)
    if Path(source).name == 'fail.json':
        p.error('Invalid message in dataset')
    s = ThreadingHTTPServer(('127.0.0.1', int(a.port)), make_handler())
    Path('opened.json').write_text(json.dumps({'mode': mode, 'source': source}))
    print(f'Loaded 1 records. Loupe: http://127.0.0.1:{s.server_port}', flush=True)
    s.serve_forever()
''')
    monkeypatch.setenv('DOXOGRAPH_LOUPE_DIR', str(checkout))
    monkeypatch.setenv('DOXOGRAPH_LOUPE_PYTHON', sys.executable)
    loupe.close()
    yield checkout
    loupe.close()


def conversation(tmp_path, name='runs.json'):
    path = tmp_path / name
    path.write_text(json.dumps([{'messages': [{'role': 'user', 'content': 'Hello'}]}]))
    return path


@pytest.mark.parametrize('kind', ['json', 'jsonl', 'survey', 'gloom', 'parquet'])
def test_launch_formats_and_local_urls(tmp_path, local_loupe, kind):
    path = tmp_path / 'Dataset with spaces'
    if kind in ('gloom', 'parquet'):
        path.mkdir()
        for split in ('train', 'test'):
            (path / f'gloom-{split}.parquet').write_bytes(b'fixture')
        source = path if kind == 'gloom' else path / 'gloom-test.parquet'
        expected = 'data_dir'
    else:
        path = path.with_suffix('.jsonl' if kind == 'jsonl' else '.json')
        value = {'schema': 'loupe.survey.v1'} if kind == 'survey' else {'messages': []}
        path.write_text(json.dumps(value if kind != 'json' else {'records': [value]}))
        source, expected = path, 'survey' if kind == 'survey' else 'conversations'
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        response = client.post('/api/datasets/loupe', json={'source': source.as_uri()})
        assert response.status_code == 200, response.text
        url = response.json()['url']
        assert url.startswith('http://127.0.0.1:')
        assert json.loads((local_loupe / 'opened.json').read_text()) == {'mode': expected, 'source': str(path)}
        process = loupe._sessions[path].process
    assert process.poll() is not None
    assert path.exists()


def test_reuses_concurrent_launches_and_reloads_changed_sources(tmp_path):
    path = conversation(tmp_path)
    original = path.read_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        urls = list(pool.map(lambda _: loupe.open_dataset(str(path)), range(4)))
    assert len(set(urls)) == len(loupe._sessions) == 1
    first = loupe._sessions[path].process
    path.write_text(json.dumps([{'messages': [], 'title': 'Changed dataset'}]))
    assert loupe.open_dataset(str(path)) == urls[0]
    second = loupe._sessions[path].process
    assert second.pid != first.pid and first.poll() is not None
    loupe.close()
    assert second.poll() is not None
    assert loupe.open_dataset(str(path)) == urls[0]
    assert path.read_bytes() != original  # Only the explicit fixture edit changed it.


@pytest.mark.parametrize('source', ['https://example.org/data.json', 'relative.json', '',
                                  'file://elsewhere/data.json', 'file:///tmp/data.json?x=1', 'file://['])
def test_rejects_nonlocal_and_ambiguous_sources(source):
    with pytest.raises(HTTPException) as error:
        loupe.open_dataset(source)
    assert error.value.status_code == 422
    assert not loupe._sessions


@pytest.mark.parametrize('kind', ['missing', 'directory', 'parquet', 'invalid-json', 'wrong-records'])
def test_rejects_missing_and_unsupported_data(tmp_path, kind):
    path = tmp_path / ('data.parquet' if kind == 'parquet' else 'data.json')
    if kind == 'directory':
        path.mkdir()
    elif kind != 'missing':
        path.write_text('invalid' if kind == 'invalid-json' else '[{"answers": {}}]')
    with pytest.raises(HTTPException) as error:
        loupe.open_dataset(str(path))
    assert error.value.status_code == 422
    assert not loupe._sessions


def test_startup_errors_and_timeouts_leave_no_child(tmp_path, monkeypatch):
    path = conversation(tmp_path, 'fail.json')
    children = []
    start = loupe.subprocess.Popen

    def track(*args, **kwargs):
        process = start(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(loupe.subprocess, 'Popen', track)
    with pytest.raises(HTTPException, match='Invalid message in dataset'):
        loupe.open_dataset(str(path))
    monkeypatch.setattr(loupe, 'START_TIMEOUT', 0)
    with pytest.raises(HTTPException) as error:
        loupe.open_dataset(str(conversation(tmp_path)))
    assert error.value.status_code == 504
    assert len(children) == 2 and all(child.poll() is not None for child in children)
    assert not loupe._sessions


def test_never_reuses_an_unowned_server(tmp_path):
    path = conversation(tmp_path)
    url = loupe.open_dataset(str(path))
    loupe.close()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', int(url.rsplit(':', 1)[1])))
        listener.listen()
        with pytest.raises(HTTPException, match='already in use'):
            loupe.open_dataset(str(path))
    assert not loupe._sessions


def test_missing_installation_and_interpreter_are_actionable(tmp_path, monkeypatch):
    path = conversation(tmp_path)
    monkeypatch.setenv('DOXOGRAPH_LOUPE_PYTHON', '/missing/python')
    with pytest.raises(HTTPException, match='Could not start Loupe') as error:
        loupe.open_dataset(str(path))
    assert error.value.status_code == 503
    monkeypatch.setenv('DOXOGRAPH_LOUPE_DIR', str(tmp_path / 'missing'))
    with pytest.raises(HTTPException, match='DOXOGRAPH_LOUPE_DIR') as error:
        loupe.open_dataset(str(path))
    assert error.value.status_code == 503


def test_launch_obeys_origin_and_workspace_guards(tmp_path, monkeypatch):
    path = conversation(tmp_path)
    monkeypatch.setattr(loupe, 'open_dataset', lambda _: pytest.fail('Must reject before launch'))
    client = TestClient(server.app, base_url='http://127.0.0.1:8765')
    assert client.post('/api/datasets/loupe', json={'source': str(path)},
                       headers={'Origin': 'https://untrusted.example'}).status_code == 403
    assert client.post('/api/datasets/loupe', json={'source': str(path)},
                       headers={'X-Doxograph-Workspace': 'missing'}).status_code == 404


@pytest.mark.parametrize('platform', [SimpleNamespace(name='nt'), SimpleNamespace(name='posix')])
def test_unsupported_platform_is_rejected_before_reading_or_launching(monkeypatch, platform):
    monkeypatch.setattr(loupe, 'os', platform)
    monkeypatch.setattr(loupe, 'source_path', lambda _: pytest.fail('Must reject before reading data'))
    monkeypatch.setattr(loupe.subprocess, 'Popen', lambda *a, **kw: pytest.fail('Must reject before launch'))
    client = TestClient(server.app, base_url='http://127.0.0.1:8765')
    response = client.post('/api/datasets/loupe', json={'source': 'C:\\data\\runs.jsonl'})
    assert response.status_code == 501
    assert 'macOS and Linux' in response.json()['detail']
    assert 'start Loupe separately' in response.json()['detail']


@pytest.mark.parametrize('survey', [False, True])
def test_navigation_shim_only_exposes_static_shell_to_top_level_links(tmp_path, survey):
    (tmp_path / 'index.html').write_text('Conversation shell')
    (tmp_path / 'survey.html').write_text('Survey shell')

    class OriginalHandler(BaseHTTPRequestHandler):
        def send(self, body, status=200, mime='text/plain'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            # Stand in for Loupe's unchanged cross-origin API guard.
            self.send(b'Original handler', status=403)

        def log_message(self, *args):
            pass

    handler = loupe_runner.navigation_handler(OriginalHandler, tmp_path, survey)
    http = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{http.server_port}'
    navigation = {'Sec-Fetch-Site': 'same-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'document'}
    try:
        response = httpx.get(url + '/', headers=navigation)
        assert response.status_code == 200
        assert response.text == ('Survey shell' if survey else 'Conversation shell')
        assert httpx.get(url + '/api/meta', headers=navigation).status_code == 403
        assert httpx.get(url + '/', headers={**navigation, 'Sec-Fetch-Dest': 'iframe'}).status_code == 403
        assert httpx.get(url + '/', headers={**navigation, 'Sec-Fetch-Mode': 'cors'}).status_code == 403
        assert httpx.get(url + '/', headers={**navigation, 'Host': 'untrusted.example'}).status_code == 403
    finally:
        http.shutdown()
        http.server_close()
        thread.join()
