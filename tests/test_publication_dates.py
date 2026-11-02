"""Keep source date precision through import, persistence, and the map's state."""

import httpx
import pytest
from fastapi.testclient import TestClient

from doxograph import ingest, publication_dates, server, store


@pytest.mark.parametrize("parts,expected", [
    ([2026, 7, 10], "2026-07-10"), ([2026, 7], "2026-07"), ([2026], "2026"),
    ([2024, 2, 29], "2024-02-29"), ([2026, 2, 29], None), ([2026, 13], None),
    ([0], None), ([2026, 0, 10], None), ([], None), ([None], None),
    ([2026, "7"], None), ([2026, True], None),
])
def test_crossref_dates_preserve_only_valid_known_parts(parts, expected):
    assert publication_dates.from_parts(parts) == expected


@pytest.mark.parametrize("field", ["published", "issued"])
@pytest.mark.parametrize("parts,expected", [
    ([2026, 7, 10], "2026-07-10"), ([2026, 7], "2026-07"), ([2026], "2026"),
    ([2026, 2, 30], None),
])
def test_doi_dates_survive_import_and_reach_the_map(field, parts, expected):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"message": {
        "DOI": "10.1234/example", "title": ["Publication date study"],
        field: {"date-parts": [parts]},
    }}))
    with httpx.Client(transport=transport) as client:
        key, created = ingest.ingest_ref(ingest.parse_ref("10.1234/example"), client)
    assert created
    assert store.load_paper(key)["publication_date"] == expected
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        [paper] = client.get("/api/state").json()["papers"]
        assert paper["year"] == 2026
        assert paper["publication_date"] == expected


def test_arxiv_uses_first_publication_date_instead_of_revision_date(monkeypatch):
    monkeypatch.setattr(ingest, "_arxiv_turn", lambda: None)
    xml = """<feed xmlns="http://www.w3.org/2005/Atom"><entry>
      <id>http://arxiv.org/abs/2607.07916v2</id><title>Publication date study</title>
      <published>2026-07-10T23:45:00Z</published><updated>2026-08-11T00:00:00Z</updated>
    </entry></feed>"""
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=xml))) as client:
        meta = ingest.fetch_arxiv("2607.07916v2", client)
    store.save_paper(store.new_paper("arxiv-study", **meta))
    assert store.summarize(store.load_paper("arxiv-study"))["publication_date"] == "2026-07-10"


@pytest.mark.parametrize("value", ["2026-02-29", "2026-00", "2026-13-01", "July 10, 2026", "", 2026])
def test_invalid_date_corrections_leave_the_saved_paper_untouched(value):
    store.save_paper(store.new_paper("study", year=2026))
    before = store.paper_path("study").read_bytes()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/study", json={"publication_date": value}).status_code == 422
    assert store.paper_path("study").read_bytes() == before


def test_date_corrections_keep_the_year_consistent_and_can_be_cleared():
    store.save_paper(store.new_paper("study", year=2025, notes="Keep my note"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch("/api/papers/study", json={"publication_date": "2026-07-10"})
        assert response.status_code == 200
        assert response.json()["year"] == 2026
        assert response.json()["notes"] == "Keep my note"
        assert client.patch("/api/papers/study", json={"year": 2025, "publication_date": "2026"}).status_code == 422
        # A title-only patch leaves the date intact; a changed year clears it.
        assert client.patch("/api/papers/study", json={"title": "New title"}).json()["publication_date"] == "2026-07-10"
        assert client.patch("/api/papers/study", json={"year": 2025}).json()["publication_date"] is None
        client.patch("/api/papers/study", json={"publication_date": "2026-07"})
        response = client.patch("/api/papers/study", json={"publication_date": None})
        assert response.json()["publication_date"] is None
        assert response.json()["year"] == 2026
