"""The PDF reaches the model through the Files API and is uploaded once."""

from __future__ import annotations

import json
import os

import anthropic
import pytest
from fastapi.testclient import TestClient

from doxograph import extract, server, store


# --- The PDF reaches the model through the Files API, not inline -------------

class FakeFiles:
    def __init__(self, fail_delete=()):
        self.uploads = []
        self.deletions = []
        self.fail_delete = set(fail_delete)

    def upload(self, file):
        self.uploads.append(file)
        return type("F", (), {"id": f"file_{len(self.uploads)}"})()

    def delete(self, file_id):
        self.deletions.append(file_id)
        if file_id in self.fail_delete:
            self.fail_delete.discard(file_id)
            import httpx2
            raise anthropic.APIConnectionError(
                request=httpx2.Request("DELETE", f"https://api.anthropic.com/v1/files/{file_id}"))


class FilesClient:
    """A client whose messages.create records the PDF block it was sent."""

    def __init__(self, fail_on=(), fail_delete=()):
        self.files = FakeFiles(fail_delete)
        self.messages = self
        self.blocks = []
        self.fail_on = set(fail_on)

    def create(self, **kwargs):
        block = kwargs["messages"][0]["content"][0]
        self.blocks.append(block)
        file_id = block["source"]["file_id"]
        if file_id in self.fail_on:
            self.fail_on.discard(file_id)
            raise anthropic.BadRequestError(
                f"file {file_id} not found", response=_response(400), body=None)
        payload = {"summary": "", "relevance": "", "proposed_tags": [], "claims": []}
        text = type("B", (), {"type": "text", "text": json.dumps(payload)})()
        return type("R", (), {"content": [text], "stop_reason": "end_turn", "usage": None})()


def _response(status: int):
    import httpx2
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.fixture
def paper_with_pdf():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4 fake")
    return "doe2026study"


def test_the_pdf_is_sent_by_file_id_not_inlined(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    extract.extract_paper(paper_with_pdf)

    assert api.files.uploads == [store.pdf_path(paper_with_pdf)]
    assert api.blocks[0]["source"] == {"type": "file", "file_id": "file_1"}
    assert api.blocks[0]["cache_control"]["type"] == "ephemeral"
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_1"


def test_a_second_read_reuses_the_upload(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    extract.extract_paper(paper_with_pdf)
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 1
    assert [b["source"]["file_id"] for b in api.blocks] == ["file_1", "file_1"]


def test_a_replaced_pdf_is_uploaded_again(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    store.pdf_path(paper_with_pdf).write_bytes(b"%PDF-1.4 a different, longer file")
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1"]
    assert api.blocks[-1]["source"]["file_id"] == "file_2"
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_2"


def test_same_size_and_mtime_with_different_bytes_is_uploaded_again(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    pdf = store.pdf_path(paper_with_pdf)
    original = pdf.stat()
    pdf.write_bytes(b"%PDF-1.4 new!")
    os.utime(pdf, ns=(original.st_atime_ns, original.st_mtime_ns))
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1"]
    assert api.blocks[-1]["source"]["file_id"] == "file_2"


def test_a_failed_remote_delete_is_retried_on_the_next_read(monkeypatch, paper_with_pdf):
    api = FilesClient(fail_delete={"file_1"})
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    store.pdf_path(paper_with_pdf).write_bytes(b"%PDF-1.4 replacement")
    extract.extract_paper(paper_with_pdf)
    assert store.load_paper(paper_with_pdf)["pdf_upload"]["superseded_file_ids"] == ["file_1"]

    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert api.files.deletions == ["file_1", "file_1"]
    assert "superseded_file_ids" not in store.load_paper(paper_with_pdf)["pdf_upload"]


def test_removing_a_paper_deletes_all_of_its_remote_uploads(monkeypatch, paper_with_pdf):
    paper = store.load_paper(paper_with_pdf)
    paper["pdf_upload"] = {
        "file_id": "file_current",
        "superseded_file_ids": ["file_old_1", "file_old_2"],
    }
    store.save_paper(paper)
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.delete(f"/api/papers/{paper_with_pdf}")

    assert response.status_code == 200
    assert api.files.deletions == ["file_old_1", "file_old_2", "file_current"]
    assert not store.paper_path(paper_with_pdf).exists()
    assert not store.pdf_path(paper_with_pdf).exists()


def test_a_remote_failure_keeps_the_paper_metadata_for_delete_retry(monkeypatch, paper_with_pdf):
    paper = store.load_paper(paper_with_pdf)
    paper["pdf_upload"] = {"file_id": "file_current"}
    store.save_paper(paper)
    api = FilesClient(fail_delete={"file_current"})

    with pytest.raises(anthropic.APIConnectionError):
        extract.delete_paper(paper_with_pdf, api=api)

    assert store.load_paper(paper_with_pdf)["pdf_upload"]["file_id"] == "file_current"
    assert store.pdf_path(paper_with_pdf).exists()


def test_an_upload_the_server_forgot_is_redone_once(monkeypatch, paper_with_pdf):
    api = FilesClient()
    monkeypatch.setattr(extract, "client", lambda: api)
    extract.extract_paper(paper_with_pdf)

    api.fail_on.add("file_1")  # the server no longer has it
    extract.extract_paper(paper_with_pdf)

    assert len(api.files.uploads) == 2
    assert [b["source"]["file_id"] for b in api.blocks] == ["file_1", "file_1", "file_2"]


def test_an_unrelated_bad_request_is_not_retried(monkeypatch, paper_with_pdf):
    api = FilesClient()

    def create(**kwargs):
        raise anthropic.BadRequestError("something else", response=_response(400), body=None)

    api.create = create
    monkeypatch.setattr(extract, "client", lambda: api)

    with pytest.raises(anthropic.BadRequestError):
        extract.extract_paper(paper_with_pdf)
    assert len(api.files.uploads) == 1


def test_extracting_without_a_pdf_says_so(monkeypatch):
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    monkeypatch.setattr(extract, "client", lambda: FilesClient())
    with pytest.raises(FileNotFoundError):
        extract.extract_paper("doe2026study")
