"""The agent reads and fills every workspace record, but never escapes its sources."""

import asyncio

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from doxograph import mcp_server, notebook, server, store


def seed(key):
    paper = store.new_paper(key, title=f"Study {key}", notes="Mine")
    paper["claims"].append(store.new_claim(paper, text="Monitors catch scheming."))
    store.save_paper(paper)
    return paper


def connection(**options):
    return mcp_server.create_server(workspace="default", allow_edits=True,
                                    transport=httpx.ASGITransport(app=server.app), **options)


def call(conn, tool, **arguments):
    return asyncio.run(conn.call_tool(tool, {"workspace": "default", **arguments})).structured_content


def test_a_paper_scoped_agent_lists_and_fills_datasets():
    seed("a")
    conn = connection(papers=["a"])
    assert call(conn, "list_datasets")["total"] == 0
    made = call(conn, "save_dataset", name="SHADE-Arena", source="https://example.org/shade",
                roles=["evaluation"], papers=["a"], custom_fields=[{"name": "Tasks", "value": "17"}])
    assert call(conn, "list_datasets", q="shade")["items"][0]["id"] == made["id"]
    updated = call(conn, "save_dataset", dataset_id=made["id"], license="MIT")
    assert updated["license"] == "MIT" and updated["source"] == "https://example.org/shade"
    assert updated["papers"] == ["a"] and updated["custom_fields"] == [{"name": "Tasks", "value": "17"}]
    with pytest.raises(ToolError, match="Name the new dataset"):
        call(conn, "save_dataset", description="Nameless")


def test_people_and_notes_are_readable_and_writable():
    conn = connection(papers=[seed("a")["key"]])
    person = call(conn, "save_person", name="Ada Lovelace", affiliation="Analytical Engines")
    assert call(conn, "save_person", person_id=person["id"], homepage="https://ada.example")["affiliation"] == "Analytical Engines"
    assert call(conn, "list_people", q="lovelace")["total"] == 1
    note = call(conn, "save_note", title="Datasets to fetch", body="SHADE-Arena")
    assert call(conn, "save_note", note_id=note["id"], body="SHADE-Arena, BashBench")["title"] == "Datasets to fetch"
    assert "BashBench" in call(conn, "read_note", note_id=note["id"])["body"]
    call(conn, "save_note", note_id="workspace", body="General notes")
    assert notebook.load()["notes"] == "General notes"
    assert call(conn, "list_notes", q="general")["total"] == 1


def test_paper_and_claim_edits_stay_inside_selected_sources():
    claim = seed("inside")["claims"][0]
    seed("outside")
    conn = connection(papers=["inside"])
    assert call(conn, "update_paper", key="inside", venue="ICML", year=2026)["venue"] == "ICML"
    assert store.load_paper("inside")["notes"] == "Mine"
    assert call(conn, "save_claim", paper="inside", claim_id=claim["id"], limitations="Small sample")["limitations"] == "Small sample"
    added = call(conn, "save_claim", paper="inside", text="A second claim.", quote="second")
    assert len(store.load_paper("inside")["claims"]) == 2 and added["text"] == "A second claim."
    for tool, arguments in (("update_paper", {"key": "outside", "venue": "X"}),
                            ("save_claim", {"paper": "outside", "text": "Escaped"})):
        with pytest.raises(ToolError, match="outside the selected sources"):
            call(conn, tool, **arguments)
    assert not store.load_paper("outside").get("venue") and len(store.load_paper("outside")["claims"]) == 1
