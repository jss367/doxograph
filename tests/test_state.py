"""`/api/state` is cached on a corpus signature and answers 304 when unchanged."""

from __future__ import annotations

from fastapi.testclient import TestClient

from doxograph import config, server, store


def client() -> TestClient:
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def test_state_carries_an_etag_and_honours_it():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with client() as c:
        first = c.get("/api/state")
        assert first.status_code == 200
        etag = first.headers["etag"]
        again = c.get("/api/state", headers={"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""
        assert again.headers["etag"] == etag


def test_a_write_changes_the_etag_and_the_answer():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with client() as c:
        first = c.get("/api/state")
        store.add_claim("doe2026study", {"text": "A holds."})
        second = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 200
        assert second.headers["etag"] != first.headers["etag"]
        assert [r["text"] for r in second.json()["claims"]] == ["A holds."]


def test_the_etag_tells_two_empty_workspaces_apart():
    # The signature is of the corpus files alone, so two empty workspaces
    # share one; an ETag from one must not answer 304 for the other.
    other = config.create_workspace("Other")
    with client() as c:
        etag = c.get("/api/state").headers["etag"]
        headers = {"X-Doxograph-Workspace": other["id"], "If-None-Match": etag}
        response = c.get("/api/state", headers=headers)
        assert response.status_code == 200
        assert response.json()["workspace"]["name"] == "Other"
        assert response.headers["etag"] != etag


def test_the_cached_answer_is_served_while_nothing_changes(monkeypatch):
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    builds = []
    real = server._build_state
    monkeypatch.setattr(server, "_build_state", lambda: builds.append(1) or real())
    with client() as c:
        c.get("/api/state")
        c.get("/api/state")
        assert len(builds) == 1
        store.add_tag("alpha")
        assert c.get("/api/state").json()["tags"] == [{"name": "alpha", "description": ""}]
        assert len(builds) == 2


def test_signature_sees_every_file_the_page_shows():
    before = store.corpus_signature()
    store.save_paper(store.new_paper("doe2026study"))
    after_paper = store.corpus_signature()
    store.add_tag("alpha")
    after_tag = store.corpus_signature()
    store.save_ledger([{"id": "L1", "text": "Mine."}])
    after_ledger = store.corpus_signature()
    store.save_context("What I study.")
    after_context = store.corpus_signature()
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")
    after_pdf = store.corpus_signature()
    assert len({before, after_paper, after_tag, after_ledger, after_context, after_pdf}) == 6


def test_jobs_have_their_own_route_and_are_not_in_state():
    with client() as c:
        job = server._new_job("reading")
        assert "jobs" not in c.get("/api/state").json()
        listed = c.get("/api/jobs").json()["jobs"]
        # Other tests leave jobs behind in the module-level table; this one's
        # is among them, newest first.
        assert listed[0]["id"] == job["id"]


def test_the_etag_changes_when_the_api_key_appears(monkeypatch):
    # has_key comes from the environment, not the corpus, so it is part of
    # the identity: a key added while the server runs is seen on the next poll.
    monkeypatch.setattr(config, "api_key", lambda: None)
    with client() as c:
        first = c.get("/api/state")
        assert first.json()["has_key"] is False
        monkeypatch.setattr(config, "api_key", lambda: "sk-test")
        second = c.get("/api/state", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 200
        assert second.json()["has_key"] is True
