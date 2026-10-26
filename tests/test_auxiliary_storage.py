"""Damage to one document must not hide healthy research or erase saved work."""

import json
from pathlib import Path

import pytest

from doxograph import config, notebook, research_notes, storage, store
from test_storage_recovery import client, corpus


SECTIONS = [
    ("notebook.json", "notebook"),
    ("tensions.json", "tensions"),
    ("agreements.json", "agreements"),
    ("syntheses.json", "syntheses"),
]


@pytest.mark.parametrize("filename,section", SECTIONS)
@pytest.mark.parametrize("contents", [b"{broken", b"\xff", b"null", b"[]"])
def test_damaged_document_is_reported_and_recovers_after_repair(filename, section, contents):
    corpus()
    store.write_json(config.data_dir() / "notebook.json", notebook.Notebook(notes="My writing").model_dump())
    path = config.data_dir() / filename
    original = path.read_bytes()
    path.write_bytes(contents)
    with client() as c:
        response = c.get("/api/state")
        assert response.status_code == 200
        data = response.json()
        assert {p["key"] for p in data["papers"]} == {"first", "second"}
        assert len(data["claims"]) == 2
        [issue] = data["storage_issues"]
        assert issue["path"] == str(path)
        assert "left untouched" in issue["detail"]
        assert data[section] == (notebook.Notebook().model_dump() if section == "notebook" else [])
        if section != "agreements":
            assert len(data["agreements"]) == 1
        if section == "tensions":
            assert data["syntheses"][0]["text"] == "A synthesis"
            assert data["syntheses"][0]["stale"] is True
        unchanged = c.get("/api/state", headers={"If-None-Match": response.headers["etag"]})
        assert unchanged.status_code == 304
        assert path.read_bytes() == contents
        path.write_bytes(original)
        repaired = c.get("/api/state", headers={"If-None-Match": response.headers["etag"]})
        assert repaired.status_code == 200
        assert repaired.json()["storage_issues"] == []
        assert repaired.json()[section]
        assert repaired.json()["syntheses"][0]["stale"] is False


@pytest.mark.parametrize("filename,contents", [
    ("notebook.json", {"boards": [None]}),
    ("tensions.json", {"tensions": [None]}),
    ("tensions.json", {"tensions": [{"id": "t1", "claims": [None]}]}),
    ("tensions.json", {"seq": "one", "tensions": []}),
    ("agreements.json", {"agreements": "not a list"}),
    ("agreements.json", {"agreements": [{"id": "a1", "claims": ["first-c1"], "topics": [None]}]}),
    ("syntheses.json", {"syntheses": {"old": None}}),
    ("syntheses.json", {"syntheses": {"old": {"text": 42}}}),
])
def test_invalid_nested_records_are_isolated(filename, contents):
    corpus()
    path = config.data_dir() / filename
    store.write_json(path, contents)
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert response.json()["storage_issues"][0]["path"] == str(path)
    assert json.loads(path.read_text()) == contents


@pytest.mark.parametrize("filename,method,route,body", [
    ("notebook.json", "put", "/api/notebook/notes", {"previous": "", "notes": "Overwrite?"}),
    ("notebook.json", "put", "/api/notebook", {}),
    ("tensions.json", "patch", "/api/tensions/t1", {"status": "confirmed"}),
    ("agreements.json", "patch", "/api/agreements/a1", {"status": "confirmed"}),
    ("syntheses.json", "patch", "/api/syntheses/old", {"text": "Overwrite?"}),
    ("research-notes.json", "post", "/api/research-notes", {"title": "Overwrite?"}),
])
def test_writes_refuse_unreadable_documents(filename, method, route, body):
    corpus()
    path = config.data_dir() / filename
    path.write_text("{broken")
    with client() as c:
        response = getattr(c, method)(route, json=body)
    assert response.status_code == 503
    assert str(path) in response.json()["detail"]
    assert path.read_text() == "{broken"


@pytest.mark.parametrize("filename", ["tensions.json", "agreements.json", "syntheses.json"])
@pytest.mark.parametrize("method", ["patch", "delete"])
def test_topic_changes_report_storage_damage_without_changing_saved_work(filename, method):
    corpus()
    path = config.data_dir() / filename
    original = path.read_bytes()
    path.write_text("{broken")
    saved_paths = [store.paper_path("first"), store.paper_path("second"),
                   config.data_dir() / "tags.yaml",
                   *(config.data_dir() / name for name, _ in SECTIONS if name != "notebook.json")]
    before = {saved: saved.read_bytes() for saved in saved_paths}
    with client() as c:
        kwargs = {"json": {"name": "new"}} if method == "patch" else {}
        response = getattr(c, method)("/api/tags/old", **kwargs)
        assert response.status_code == 503
        assert str(path) in response.json()["detail"]
        assert {saved: saved.read_bytes() for saved in saved_paths} == before
        assert not store.tag_change_path().exists()
        path.write_bytes(original)
        assert getattr(c, method)("/api/tags/old", **kwargs).status_code == 200
    assert store.tag_names() == (["new"] if method == "patch" else [])


def test_permission_repair_is_retried_without_a_file_change(monkeypatch):
    corpus()
    path = store.tensions_path()
    original = Path.read_text
    with client() as c:
        with monkeypatch.context() as patch:
            def denied(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError("Access denied")
                return original(candidate, *args, **kwargs)
            patch.setattr(Path, "read_text", denied)
            first = c.get("/api/state")
            assert "Access denied" in first.json()["storage_issues"][0]["detail"]
        repaired = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []
    assert len(repaired.json()["tensions"]) == 1


def test_damage_and_defaults_stay_in_their_workspace():
    corpus()
    other = config.create_workspace("Independent research")
    store.tensions_path().write_text("{broken")
    with client() as c:
        assert len(c.get("/api/state").json()["storage_issues"]) == 1
        response = c.get("/api/state", headers={"X-Doxograph-Workspace": other["id"]})
    assert response.json()["storage_issues"] == []
    assert response.json()["tensions"] == []


def test_validated_reads_preserve_legacy_records_and_unknown_fields():
    corpus()
    data = store._read_tensions()
    data["future"] = {"setting": True}
    record = data["tensions"][0]
    record.pop("fingerprints")
    record["future"] = "Keep me"
    store.write_json(store.tensions_path(), data)
    assert store._read_tensions() == data
    store.set_tension_status(record["id"], "dismissed")
    saved = store._read_tensions()
    assert saved["future"] == data["future"]
    assert saved["tensions"][0]["future"] == "Keep me"


def test_invalid_long_form_notes_report_the_path_without_overwriting():
    path = config.data_dir() / "research-notes.json"
    path.write_text('[{"title":"My writing","body":null}]')
    with pytest.raises(storage.StorageReadError, match="research-notes.json"):
        research_notes.load()
    with client() as c:
        assert c.get("/api/research-notes").status_code == 503
        assert c.get("/api/state").status_code == 200


def test_multiple_damaged_documents_are_reported_independently():
    corpus()
    for filename, _ in SECTIONS:
        (config.data_dir() / filename).write_text("{broken")
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    data = response.json()
    assert len(data["papers"]) == 2
    assert {Path(issue["path"]).name for issue in data["storage_issues"]} == {
        filename for filename, _ in SECTIONS
    }
