import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from doxograph import config, export, server, store


@pytest.fixture
def client():
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        yield client


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    store.add_tag("dose", "Steering strength.")
    store.save_ledger([{"id": "L1", "text": "Recovery is path dependent."},
                       {"id": "my claim", "text": "An id typed with a space."}])
    store.save_paper(store.new_paper("doe2026recovery", title="Recovery under steering",
                                     authors=["Jane Doe"], year=2026))
    store.add_claim("doe2026recovery", {
        "text": "Llama-3 70B recovers in 46% of rollouts.",
        "tags": ["recovery-rate", "dose"],
        "ledger_links": [{"claim": "L1", "relation": "supports"}],
    })


def fragment(response):
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    url = urlsplit(response.headers["location"])
    assert url.path == "/"
    return {key: values[0] for key, values in parse_qs(url.fragment).items()}


def test_each_kind_redirects_to_the_view_that_shows_it(client):
    build_corpus()
    claim_id = store.load_paper("doe2026recovery")["claims"][0]["id"]
    # The workspace is always named: without it the page opens whichever one
    # this browser used last.
    assert fragment(client.get("/paper/doe2026recovery", follow_redirects=False)) == \
        {"ws": "default", "view": "claims", "paper": "doe2026recovery"}
    assert fragment(client.get(f"/claim/{claim_id}", follow_redirects=False)) == \
        {"ws": "default", "view": "claims", "paper": "doe2026recovery", "sel": claim_id}
    assert fragment(client.get("/ledger/L1", follow_redirects=False)) == \
        {"ws": "default", "view": "claims", "ledger": "L1"}
    assert fragment(client.get("/ledger/my%20claim", follow_redirects=False))["ledger"] == "my claim"


def test_a_removed_or_unknown_record_says_so(client):
    build_corpus()
    for path in ("/paper/nobody2026", "/paper/a.b", "/claim/doe2026recovery-c99",
                 "/claim/nonsense", "/ledger/L9"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 404, path
        assert "not in this workspace" in response.text
    # A key is never a path: an encoded slash does not even reach the route.
    assert client.get("/paper/..%2Fsettings", follow_redirects=False).status_code == 404


def test_an_id_not_derived_from_its_key_is_found_by_scanning(client):
    build_corpus()
    paper = store.load_paper("doe2026recovery")
    paper["claims"][0]["id"] = "legacy-7"
    store.save_paper(paper)
    assert fragment(client.get("/claim/legacy-7", follow_redirects=False))["paper"] == "doe2026recovery"


def test_links_resolve_in_the_workspace_they_name(client):
    other = config.create_workspace("Research")["id"]
    with config.use_workspace(other):
        build_corpus()
    assert client.get("/paper/doe2026recovery", follow_redirects=False).status_code == 404
    response = client.get(f"/paper/doe2026recovery?workspace={other}", follow_redirects=False)
    assert fragment(response)["ws"] == other


def test_agents_reading_the_corpus_get_the_link_to_cite(client):
    build_corpus()
    claim = client.get("/api/query/claims").json()["items"][0]
    assert claim["url"] == f"http://127.0.0.1:8765/claim/{claim['id']}"
    assert client.get("/api/query/papers").json()["items"][0]["url"] == \
        "http://127.0.0.1:8765/paper/doe2026recovery"
    assert client.get("/api/query/papers/doe2026recovery").json()["paper"]["url"] == \
        "http://127.0.0.1:8765/paper/doe2026recovery"
    other = config.create_workspace("Research")["id"]
    with config.use_workspace(other):
        build_corpus()
    scoped = client.get("/api/query/claims", headers={"X-Doxograph-Workspace": other}).json()
    assert scoped["items"][0]["url"].endswith(f"?workspace={other}")


def test_the_export_anchors_every_paper_claim_and_ledger_entry_once():
    build_corpus()
    claim_id = store.load_paper("doe2026recovery")["claims"][0]["id"]
    html = export.render()
    ids = re.findall(r'\bid="([^"]+)"', html)
    # Drawn under two topics and under L1, but anchored once.
    assert ids.count(f"claim-{claim_id}") == 1
    assert "paper-doe2026recovery" in ids
    assert "ledger-L1" in ids
    assert "ledger-my%20claim" in ids
    # Every drawing links to the one anchor.
    assert html.count(f'href="#claim-{claim_id}"') == 3


def test_the_export_head_describes_it_for_an_index_card():
    build_corpus()
    html = export.render(title="ESR")
    assert "<title>ESR</title>" in html
    assert '<meta name="description" content="1 papers · 1 claims · 2 topics">' in html
    assert re.search(r'<meta name="date" content="\d{4}-\d\d-\d\dT[^"]+">', html)
