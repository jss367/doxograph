"""Reloads cannot combine an old cached feature module with a new app script."""

import re
import shutil

from fastapi.testclient import TestClient

from doxograph import server


def test_frontend_assets_share_a_version_and_bypass_legacy_caches(tmp_path, monkeypatch):
    assets = tmp_path / "static"
    shutil.copytree(server.STATIC, assets, ignore=shutil.ignore_patterns("vendor"))
    monkeypatch.setattr(server, "STATIC", assets)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        shell = client.get("/")
        assert shell.headers["cache-control"] == "no-store"
        urls = re.findall(r'(?:src|href)="(/[^"?]+\.(?:js|css)\?v=[^"]+)"', shell.text)
        assert any(url.startswith("/startup.js?") for url in urls)
        assert len({url.split("?v=")[1] for url in urls}) == 1
        for url in urls:
            response = client.get(url)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            # Legacy URLs also stop populating caches.
            assert client.get(url.split("?")[0]).headers["cache-control"] == "no-store"

        module = assets / "research-tools.js"
        module.write_text(module.read_text() + "\n// Updated feature module\n")
        updated = client.get("/")
        next_urls = re.findall(r'(?:src|href)="(/[^"?]+\.(?:js|css)\?v=[^"]+)"', updated.text)
        assert next_urls != urls
        assert len({url.split("?v=")[1] for url in next_urls}) == 1
        assert '/vendor/pdf' not in ' '.join(next_urls)
