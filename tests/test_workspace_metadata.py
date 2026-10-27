"""Workspace metadata damage must not hide or overwrite healthy research."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from doxograph import __main__, config, extract, storage, store
from test_storage_recovery import client, corpus


DOCUMENTS = [
    ("tags.yaml", "tags", b"{broken"),
    ("ledger.yaml", "ledger", b"{broken"),
    ("context.md", "context", b"\xff"),
    ("retired-keys.json", None, b"{broken"),
]


@pytest.mark.parametrize("filename,section,contents", DOCUMENTS)
def test_damage_keeps_healthy_research_visible_and_repair_clears_warning(filename, section, contents):
    corpus()
    store.save_ledger([{"id": "L1", "text": "My claim"}])
    store.save_context("My research")
    store.retire_key("previous")
    path = config.data_dir() / filename
    original = path.read_bytes()
    with client() as c:
        healthy = c.get("/api/state")
        path.write_bytes(contents)
        damaged = c.get("/api/state", headers={"If-None-Match": healthy.headers["etag"]})
        assert damaged.status_code == 200
        data = damaged.json()
        assert len(data["papers"]) == len(data["claims"]) == 2
        assert len(data["tensions"]) == len(data["agreements"]) == 1
        [issue] = data["storage_issues"]
        assert issue["path"] == str(path)
        assert "left untouched" in issue["detail"]
        if section:
            assert data[section] == ("" if section == "context" else [])
        assert path.read_bytes() == contents
        assert c.get("/api/state", headers={"If-None-Match": damaged.headers["etag"]}).status_code == 304
        path.write_bytes(original)
        repaired = c.get("/api/state", headers={"If-None-Match": damaged.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []
    if section:
        assert repaired.json()[section]


@pytest.mark.parametrize("filename,contents", [
    ("tags.yaml", "[]"),
    ("tags.yaml", "false"),
    ("tags.yaml", "tags: wrong"),
    ("tags.yaml", "tags: [null]"),
    ("tags.yaml", "tags: [{name: 42}]"),
    ("tags.yaml", "tags: [{name: ' '}]"),
    ("tags.yaml", "tags: [{name: old, description: null}]"),
    ("tags.yaml", "tags: [{name: old}, {name: old}]"),
    ("tags.yaml", "retired_topics: [null]"),
    ("tags.yaml", "future: 2026-10-26"),
    ("tags.yaml", "future: .nan"),
    ("tags.yaml", "future: &cycle [*cycle]"),
    ("ledger.yaml", "claims: [null]"),
    ("ledger.yaml", "claims: [{text: lost identity}]"),
    ("ledger.yaml", "claims: [{id: 1}]"),
    ("ledger.yaml", "claims: [{id: ' '}]"),
    ("ledger.yaml", "claims: [{id: L1, text: []}]"),
    ("ledger.yaml", "claims: [{id: L1}, {id: L1}]"),
    ("retired-keys.json", "null"),
    ("retired-keys.json", "{}"),
    ("retired-keys.json", '["previous", null]'),
    ("retired-keys.json", '["previous", 42]'),
    ("retired-keys.json", '[" "]'),
])
def test_malformed_records_are_reported_instead_of_silently_dropped(filename, contents):
    corpus()
    path = config.data_dir() / filename
    path.write_text(contents)
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert response.json()["storage_issues"][0]["path"] == str(path)
    assert path.read_text() == contents


@pytest.mark.parametrize("filename,contents,method,route,body", [
    ("tags.yaml", b"{broken", "post", "/api/tags", {"name": "new"}),
    ("tags.yaml", b"{broken", "patch", "/api/tags/old", {"name": "new"}),
    ("tags.yaml", b"{broken", "delete", "/api/tags/old", None),
    ("ledger.yaml", b"{broken", "put", "/api/ledger", {"claims": []}),
    ("context.md", b"\xff", "put", "/api/context", {"text": "Overwrite?"}),
    ("retired-keys.json", b"{broken", "delete", "/api/papers/first", None),
])
def test_mutations_refuse_unreadable_metadata_before_changing_research(filename, contents, method, route, body):
    corpus()
    path = config.data_dir() / filename
    path.write_bytes(contents)
    before = {p: p.read_bytes() for p in config.data_dir().rglob("*") if p.is_file() and p.suffix != ".lock"}
    with client() as c:
        response = getattr(c, method)(route, **({"json": body} if body is not None else {}))
    assert response.status_code == 503
    assert str(path) in response.json()["detail"]
    assert all(p.read_bytes() == content for p, content in before.items())
    assert not store.tag_change_path().exists()


@pytest.mark.parametrize("filename,section,_", DOCUMENTS)
def test_read_permission_repair_is_retried_without_changing_file_stats(monkeypatch, filename, section, _):
    corpus()
    path = config.data_dir() / filename
    original = Path.read_text
    with client() as c:
        with monkeypatch.context() as patch:
            def denied(candidate, *args, **kwargs):
                if candidate == path:
                    raise PermissionError("Access denied")
                return original(candidate, *args, **kwargs)
            patch.setattr(Path, "read_text", denied)
            first = c.get("/api/state")
            assert first.status_code == 200
            assert "Access denied" in first.json()["storage_issues"][0]["detail"]
        repaired = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []


def test_metadata_stat_failure_does_not_bypass_the_safe_reader(monkeypatch):
    corpus()
    path = config.tags_path()
    original_stat, original_read = Path.stat, Path.read_text

    def denied_stat(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("Cannot stat vocabulary")
        return original_stat(candidate, *args, **kwargs)

    def denied_read(candidate, *args, **kwargs):
        if candidate == path:
            raise PermissionError("Cannot read vocabulary")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(Path, "read_text", denied_read)
    with client() as c:
        response = c.get("/api/state")
    assert response.status_code == 200
    assert response.json()["storage_issues"][0]["path"] == str(path)


@pytest.mark.parametrize("filename,section,_", DOCUMENTS)
def test_acl_change_invalidates_healthy_metadata_cache(monkeypatch, filename, section, _):
    corpus()
    store.save_ledger([{"id": "L1", "text": "My claim"}])
    store.save_context("My research")
    store.retire_key("previous")
    path = config.data_dir() / filename
    original_stat, original_read = Path.stat, Path.read_text
    baseline = path.stat()
    readable = True
    ctime = baseline.st_ctime_ns

    def acl_stat(candidate, *args, **kwargs):
        if candidate == path:
            return SimpleNamespace(
                st_size=baseline.st_size, st_mtime_ns=baseline.st_mtime_ns,
                st_ino=baseline.st_ino, st_mode=baseline.st_mode, st_ctime_ns=ctime,
            )
        return original_stat(candidate, *args, **kwargs)

    def acl_read(candidate, *args, **kwargs):
        if candidate == path and not readable:
            raise PermissionError("ACL denies reading metadata")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", acl_stat)
    monkeypatch.setattr(Path, "read_text", acl_read)
    with client() as c:
        healthy = c.get("/api/state")
        assert healthy.json()["storage_issues"] == []
        readable = False
        ctime += 1
        damaged = c.get("/api/state", headers={"If-None-Match": healthy.headers["etag"]})
        assert damaged.status_code == 200
        [issue] = damaged.json()["storage_issues"]
        assert issue["path"] == str(path)
        assert "ACL denies" in issue["detail"]
        assert len(damaged.json()["papers"]) == len(damaged.json()["claims"]) == 2
        readable = True
        ctime += 1
        repaired = c.get("/api/state", headers={"If-None-Match": damaged.headers["etag"]})
    assert repaired.status_code == 200
    assert repaired.json()["storage_issues"] == []
    if section:
        assert repaired.json()[section] == healthy.json()[section]


@pytest.mark.parametrize("filename,_,contents", DOCUMENTS)
def test_metadata_damage_is_confined_to_its_workspace(filename, _, contents):
    corpus()
    other = config.create_workspace("Independent research")
    (config.data_dir() / filename).write_bytes(contents)
    with client() as c:
        assert len(c.get("/api/state").json()["storage_issues"]) == 1
        separate = c.get("/api/state", headers={"X-Doxograph-Workspace": other["id"]})
    assert separate.json()["storage_issues"] == []


@pytest.mark.parametrize("filename,read,field", [
    ("tags.yaml", store.load_tags, "tags"),
    ("ledger.yaml", store.load_ledger, "claims"),
])
@pytest.mark.parametrize("contents", ["", "null", "{}", "{field}: null"])
def test_empty_yaml_documents_and_nullable_lists_remain_compatible(filename, read, field, contents):
    path = config.data_dir() / filename
    path.write_text(contents.replace("{field}", field))
    assert read() == []
    assert path.read_text() == contents.replace("{field}", field)


def test_metadata_edits_preserve_future_fields_and_legacy_optional_fields():
    config.tags_path().write_text("future: {enabled: true}\ntags: [{name: old, extra: [keep]}]\n")
    config.ledger_path().write_text("future: [keep]\nclaims: [{id: L1, extra: {keep: true}}]\n")
    assert store.load_tags() == [{"name": "old", "extra": ["keep"]}]
    assert store.load_ledger() == [{"id": "L1", "extra": {"keep": True}}]
    store.rename_tag("old", "new")
    store.save_ledger([{"id": "L1", "text": "Edited"}, {"id": "L2"}])
    tags = yaml.safe_load(config.tags_path().read_text())
    ledger = yaml.safe_load(config.ledger_path().read_text())
    assert tags["future"] == {"enabled": True}
    assert tags["tags"] == [{"name": "new", "extra": ["keep"]}]
    assert ledger["future"] == ["keep"]
    assert ledger["claims"] == [{"id": "L1", "text": "Edited", "extra": {"keep": True}}, {"id": "L2"}]


def test_corrupt_retirement_history_blocks_reservation_and_local_and_remote_deletion():
    paper = store.new_paper("current", pdf_upload={"file_id": "remote-file"})
    store.save_paper(paper)
    store.pdf_path("current").write_bytes(b"PDF")
    store.text_path("current").write_text("Extracted text")
    store.retired_keys_path().write_text("{broken")
    api = Mock()
    for action in (lambda: store.reserve_key("previous"), lambda: store.retire_key("current"),
                   lambda: store.delete_paper("current"), lambda: extract.delete_paper("current", api=api)):
        with pytest.raises(storage.StorageReadError, match="retired-keys.json"):
            action()
    api.files.delete.assert_not_called()
    assert store.load_paper("current") == paper
    assert store.pdf_path("current").read_bytes() == b"PDF"
    assert store.text_path("current").read_text() == "Extracted text"
    assert not store.paper_path("previous").exists()
    assert store.retired_keys_path().read_text() == "{broken"
    store.retired_keys_path().write_text('["previous"]')
    assert store.reserve_key("previous") == "previousa"


@pytest.mark.parametrize("failure", ["read", "write"])
def test_retirement_failure_prevents_remote_cleanup(monkeypatch, failure):
    paper = store.new_paper("current", pdf_upload={"file_id": "remote-file"})
    store.save_paper(paper)
    store.pdf_path("current").write_bytes(b"PDF")
    store.text_path("current").write_text("Extracted text")
    store.retire_key("previous")
    path = store.retired_keys_path()
    original_read, original_write = Path.read_text, store.write_atomic

    def read(candidate, *args, **kwargs):
        if candidate == path and failure == "read":
            raise PermissionError("Cannot read retirement history")
        return original_read(candidate, *args, **kwargs)

    def write(candidate, contents):
        if candidate == path and failure == "write":
            raise PermissionError("Cannot write retirement history")
        return original_write(candidate, contents)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(store, "write_atomic", write)
    api = Mock()
    with pytest.raises((storage.StorageReadError, PermissionError), match="retirement history"):
        extract.delete_paper("current", api=api)
    api.files.delete.assert_not_called()
    assert store.load_paper("current") == paper
    assert store.pdf_path("current").read_bytes() == b"PDF"
    assert store.text_path("current").read_text() == "Extracted text"
    assert original_read(path) == '[\n  "previous"\n]\n'


def test_cleanup_does_not_repeat_retirement_after_remote_deletion(monkeypatch):
    store.save_paper(store.new_paper("current", pdf_upload={
        "file_id": "remote-current", "superseded_file_ids": ["remote-old"],
    }))
    store.pdf_path("current").write_bytes(b"PDF")
    store.text_path("current").write_text("Extracted text")
    retired = store.retired_keys_path()
    api = Mock()

    def delete_remote(file_id):
        if file_id == "remote-old":
            assert "current" in store.retired_keys()
            # Simulate damage or a permission change while cloud calls run.
            retired.write_text("{broken")
            monkeypatch.setattr(store, "retire_key", Mock(side_effect=AssertionError("retirement repeated")))

    api.files.delete.side_effect = delete_remote
    extract.delete_paper("current", api=api)
    assert [call.args[0] for call in api.files.delete.call_args_list] == ["remote-old", "remote-current"]
    assert not store.paper_path("current").exists()
    assert not store.pdf_path("current").exists()
    assert not store.text_path("current").exists()
    with pytest.raises(storage.StorageReadError):
        store.reserve_key("current")


def test_damaged_vocabulary_blocks_pending_recovery_before_any_papers_move():
    corpus()
    store.write_json(store.tag_change_path(), {"version": 1, "old": "old", "new": "new", "tags": [{"name": "new"}]})
    config.tags_path().write_text("{broken")
    with pytest.raises(store.StorageRecoveryError, match="tags.yaml"):
        store.recover_storage()
    assert store.load_paper("first")["claims"][0]["tags"] == ["old"]
    assert store.tag_change_path().exists()
    config.tags_path().write_text("tags: [{name: old}]")
    store.recover_storage()
    assert store.load_paper("first")["claims"][0]["tags"] == ["new"]


@pytest.mark.parametrize("filename,_,contents", DOCUMENTS[:3])
def test_extraction_refuses_damaged_metadata_before_any_upload_or_model_request(monkeypatch, filename, _, contents):
    corpus()
    (config.data_dir() / filename).write_bytes(contents)
    api, request, upload = Mock(), Mock(), Mock()
    monkeypatch.setattr(config, "require_ai_enabled", lambda: None)
    monkeypatch.setattr(extract, "client", lambda: api)
    monkeypatch.setattr(extract, "_create", request)
    monkeypatch.setattr(extract, "paper_document", upload)
    with pytest.raises(storage.StorageReadError, match=filename):
        extract.extract_paper("first")
    request.assert_not_called()
    upload.assert_not_called()


def test_cli_reports_the_damaged_path_without_a_traceback(capsys):
    config.tags_path().write_text("{broken")
    assert __main__.main(["tags"]) == 1
    captured = capsys.readouterr()
    assert str(config.tags_path()) in captured.err
    assert "Traceback" not in captured.err
