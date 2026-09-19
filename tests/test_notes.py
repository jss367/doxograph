"""What a person writes themselves: a note on a paper and a note on a claim.

Neither is ever sent to a model, neither is written by one, and neither makes
a pass think its topic has changed. The other half of the same split is
`error`, which is where the ingest now says why a paper has no PDF.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from doxograph import export, server, store


def paper_with_a_claim(note: str = "") -> str:
    store.save_paper(store.new_paper("doe2026study", title="A Study", authors=["Jane Doe"], year=2026))
    claim = store.add_claim("doe2026study", {
        "text": "Recovery happens in 46% of rollouts.",
        "tags": ["recovery"],
        "note": note,
    })
    return claim["id"]


# --- the claim's note ------------------------------------------------------

def test_a_claim_carries_a_note_written_by_hand():
    claim_id = paper_with_a_claim()
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.patch(
            f"/api/papers/doe2026study/claims/{claim_id}",
            json={"note": "The 46% is the best of three conditions."},
        )
    assert response.status_code == 200
    assert response.json()["note"] == "The 46% is the best of three conditions."
    stored = store.load_paper("doe2026study")["claims"][0]
    assert stored["note"] == "The 46% is the best of three conditions."


def test_a_new_claim_can_be_written_with_a_note():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/papers/doe2026study/claims",
                               json={"text": "A claim of mine.", "note": "Why I wrote it."})
    assert response.json()["note"] == "Why I wrote it."


def test_the_note_reaches_the_page_with_the_claim():
    paper_with_a_claim("Worth checking against Table 4.")
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        state = client.get("/api/state").json()
    assert state["claims"][0]["note"] == "Worth checking against Table 4."


def test_writing_a_note_does_not_stale_a_pass():
    """A pass is asked about what the claims say. A note says nothing about
    the paper, so putting one on a claim must not put its topic back in the
    queue: the whole point of a note is that it costs no model call."""
    claim_id = paper_with_a_claim()
    rows = store.claim_rows()
    before = store.pass_signature("recovery", rows)
    store.update_claim("doe2026study", claim_id, {"note": "Mine, and only mine."})
    assert store.pass_signature("recovery", store.claim_rows()) == before


# --- the paper's note ------------------------------------------------------

def test_a_paper_carries_a_note_and_the_listing_shows_it():
    store.save_paper(store.new_paper("doe2026study", title="A Study"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.patch("/api/papers/doe2026study",
                            json={"notes": "Read this for the method, not the result."}).status_code == 200
        state = client.get("/api/state").json()
    assert state["papers"][0]["notes"] == "Read this for the method, not the result."
    assert store.load_paper("doe2026study")["notes"] == "Read this for the method, not the result."


def test_a_note_is_not_the_reason_a_pdf_is_missing():
    """`notes` used to hold both. A download failure goes to `error` now, so
    a note written by hand is not overwritten by the next failed retry, and a
    failure is not shown as something a person wrote."""
    paper = store.new_paper("doe2026study", title="A Study")
    paper["notes"] = "Mine."
    paper["error"] = "PDF download failed: 503"
    store.save_paper(paper)
    loaded = store.load_paper("doe2026study")
    assert loaded["notes"] == "Mine."
    assert loaded["error"] == "PDF download failed: 503"


def test_an_older_papers_notes_are_read_as_the_failure_they_were():
    """A paper written before the split has no `error` key at all, and
    nothing could have put a note in `notes` then, so what is there is the
    ingest's."""
    path = store.paper_path("doe2026study")
    paper = store.new_paper("doe2026study", title="A Study")
    paper.pop("error")
    paper["notes"] = "PDF download failed: 503"
    path.write_text(json.dumps(paper), encoding="utf-8")

    loaded = store.load_paper("doe2026study")
    assert loaded["error"] == "PDF download failed: 503"
    assert loaded["notes"] == ""


# --- the export ------------------------------------------------------------

def test_the_export_carries_both_notes():
    claim_id = paper_with_a_claim("The number is the best case <of three>.")
    paper = store.load_paper("doe2026study")
    paper["notes"] = "Kept for the method."
    store.save_paper(paper)
    assert claim_id

    html = export.render()
    assert "The number is the best case &lt;of three&gt;." in html
    assert "Kept for the method." in html
    assert html.count("my note") >= 2


def test_a_claim_is_found_in_the_export_by_its_note():
    """The export filters on one string per claim. A note is part of it, so a
    word only the reader wrote still finds the claim."""
    paper_with_a_claim("Contradicts Smith on the recovery window.")
    html = export.render()
    assert "contradicts smith on the recovery window." in html
