from fastapi.testclient import TestClient

from doxograph import bib, config, export, server, store


def build_corpus():
    store.add_tag("recovery-rate", "How often a model returns to task.")
    store.save_ledger([{"id": "L1", "text": "Recovery is path dependent."}])
    paper = store.new_paper(
        "doe2026recovery",
        title="Recovery under steering",
        authors=["Jane Doe", "Bo Li"],
        year=2026,
        venue="arXiv",
        summary="Measures recovery.",
        source={"kind": "arxiv", "id": "2602.06941", "url": "https://arxiv.org/abs/2602.06941"},
    )
    store.save_paper(paper)
    store.add_claim("doe2026recovery", {
        "text": "Llama-3 70B recovers in 46% of rollouts.",
        "tags": ["recovery-rate"],
        "quote": "We observe recovery in 46% <script>of</script> rollouts.",
        "ledger_links": [{"claim": "L1", "relation": "supports", "note": "Same endpoint."}],
    })


def test_export_is_self_contained_and_escapes_content():
    build_corpus()
    html = export.render()
    assert "<script>of</script>" not in html
    assert "&lt;script&gt;of&lt;/script&gt;" in html
    assert "src=" not in html.replace('src="/', '')  # no external assets
    assert "recovery-rate" in html
    assert "Recovery is path dependent." in html
    assert "Llama-3 70B recovers" in html


def test_export_writes_where_asked(tmp_path):
    build_corpus()
    out = tmp_path / "nested" / "doxograph.html"
    assert export.write(out) == out
    assert out.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")


def test_bibtex_uses_eprint_for_arxiv():
    build_corpus()
    text = bib.render()
    assert text.startswith("@misc{doe2026recovery,")
    assert "archivePrefix = {arXiv}" in text
    assert "author = {Jane Doe and Bo Li}" in text


def test_bibtex_escapes_specials():
    store.save_paper(store.new_paper("x2026y", title="Cost & Benefit of 50% Steering",
                                     authors=["A B"], year=2026, venue="NeurIPS"))
    text = bib.render()
    assert r"Cost \& Benefit of 50\% Steering" in text
    assert "@article{x2026y," in text


def test_the_exported_file_is_served_back():
    """The page offers to open what it just exported, rather than naming a
    path the reader has to go and find."""
    build_corpus()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/export").status_code == 404
        client.post("/api/export", json={"title": "Doxograph"})
        response = client.get("/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "inline" in response.headers["content-disposition"]
    assert "Llama-3 70B recovers" in response.text


def test_the_export_api_writes_only_where_the_workspace_keeps_its_export(tmp_path):
    """A `path` in the request once named any file on disk to create and
    overwrite. The page never sent one, and the CLI keeps `--out`."""
    build_corpus()
    elsewhere = tmp_path / "nested" / "elsewhere.html"
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/export", json={"title": "Doxograph", "path": str(elsewhere)})
    assert response.status_code == 200
    assert response.json()["path"] == str(config.export_path())
    assert config.export_path().exists()
    assert not elsewhere.parent.exists()
