"""The installation-wide analysis switch gates both queued work and API calls."""

import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from doxograph import __main__, config, extract, ingest, server, store


def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_setting_persists_across_processes_and_workspaces_and_invalidates_state():
    other = config.create_workspace("Other")
    with client() as c:
        initial = c.get("/api/state")
        assert initial.json()["ai_enabled"] is True
        assert c.put("/api/settings", json={"ai_enabled": False}).json() == {"ai_enabled": False}
        changed = c.get("/api/state", headers={"If-None-Match": initial.headers["etag"]})
        assert changed.status_code == 200
        assert changed.json()["ai_enabled"] is False
        assert c.get("/api/state", headers={"X-Doxograph-Workspace": other["id"]}).json()["ai_enabled"] is False
        result = subprocess.check_output(
            [sys.executable, "-c", "from doxograph.config import ai_enabled; print(ai_enabled())"], text=True)
        assert result.strip() == "False"
        assert c.put("/api/settings", json={"ai_enabled": True}).json() == {"ai_enabled": True}
        assert c.get("/api/state").json()["ai_enabled"] is True


@pytest.mark.parametrize("contents", ['broken', 'null', '[]', '{"ai_enabled": "false"}'])
def test_invalid_settings_do_not_reenable_analysis(contents):
    (config.base_data_dir() / "settings.json").write_text(contents)
    assert config.ai_enabled() is False


@pytest.mark.parametrize("path", ["/api/papers/example/extract", "/api/retag", "/api/tensions",
                                  "/api/agreements", "/api/syntheses"])
def test_analysis_routes_reject_requests_before_queueing(monkeypatch, path):
    config.set_ai_enabled(False)
    submit = Mock()
    monkeypatch.setattr(server._pool, "submit", submit)
    with client() as c:
        response = c.post(path, json={})
    assert response.status_code == 403
    assert "AI analysis is disabled" in response.json()["detail"]
    submit.assert_not_called()


@pytest.mark.parametrize("operation", [extract.extract_paper, extract.retag_paper,
    extract.find_tensions, extract.find_agreements, extract.synthesize_topic, extract.upload_pdf])
def test_direct_analysis_is_blocked_before_creating_a_client(monkeypatch, operation):
    config.set_ai_enabled(False)
    api = Mock(side_effect=AssertionError("must not create an API client"))
    monkeypatch.setattr(extract, "client", api)
    with pytest.raises(RuntimeError, match="AI analysis is disabled"):
        operation("example")
    api.assert_not_called()


def test_waiting_model_call_rechecks_setting_after_acquiring_slot(monkeypatch):
    @contextmanager
    def slot():
        config.set_ai_enabled(False)
        yield

    api = Mock()
    monkeypatch.setattr(extract, "_pass_slots", slot())
    with pytest.raises(RuntimeError, match="AI analysis is disabled"):
        extract._create(api, model="unused")
    api.messages.create.assert_not_called()
    config.set_ai_enabled(True)
    # Re-enabling permits calls again.
    from contextlib import nullcontext
    monkeypatch.setattr(extract, "_pass_slots", nullcontext())
    extract._create(api, model="unused")
    api.messages.create.assert_called_once_with(model="unused")


def test_import_endpoints_override_automatic_analysis_including_native_defaults(monkeypatch):
    config.set_ai_enabled(False)
    submit = Mock()
    monkeypatch.setattr(server._pool, "submit", submit)
    with client() as c:
        assert c.post("/api/ingest", json={"text": "2602.06941", "extract": True}).status_code == 200
        assert submit.call_args.args[-1] is False
        assert c.post("/api/upload", files={"files": ("paper.pdf", b"%PDF-1.4\n", "application/pdf")}).status_code == 200
        assert submit.call_args.args[-1] is False


@pytest.mark.parametrize("upload", [False, True])
def test_import_workers_skip_analysis_disabled_after_queueing(monkeypatch, upload):
    key = "example"
    store.save_paper(store.new_paper(key))
    store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
    config.set_ai_enabled(False)
    monkeypatch.setattr(ingest, "ingest_ref", lambda *a: (key, True))
    monkeypatch.setattr(ingest, "ingest_staged_pdf", lambda *a: (key, True))
    analyze = Mock(side_effect=AssertionError("must not analyze"))
    monkeypatch.setattr(extract, "extract_paper", analyze)
    job = server._new_job("import")
    if upload:
        server._run_upload(job, store.pdf_path(key), "paper.pdf", True)
    else:
        server._run_ingest(job, None, True)
    assert job["state"] == "done"
    analyze.assert_not_called()


def test_cli_import_still_succeeds_without_analysis(monkeypatch):
    config.set_ai_enabled(False)
    key = "example"
    store.save_paper(store.new_paper(key))
    store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(ingest, "ingest_ref", lambda *a: (key, True))
    analyze = Mock(side_effect=AssertionError("must not analyze"))
    monkeypatch.setattr(extract, "extract_paper", analyze)
    assert __main__.cmd_add(SimpleNamespace(refs=["2602.06941"], no_extract=False)) == 0
    analyze.assert_not_called()
