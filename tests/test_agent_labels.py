"""Agents label papers through the same scoped bulk editor as the reader."""

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError
from doxograph import __main__, agent, config, mcp_server, server, store
from pdfs import minimal_pdf
from test_agent import fake_claude, fake_codex, stream


def seed(key):
    paper = store.new_paper(key, title=f"Study {key}", labels=["Reading list"], notes="PRIVATE")
    paper["claims"].append(store.new_claim(paper, text="Steering improves recovery.", tags=["recovery"]))
    store.save_paper(paper)
    return paper


def call(connection, **arguments):
    return asyncio.run(connection.call_tool("update_paper_labels", {"workspace": "default", **arguments})).structured_content


def connection(**options):
    return mcp_server.create_server(workspace="default", allow_labels=True, without_notes=True,
        transport=httpx.ASGITransport(app=server.app), **options)


def test_label_tool_is_opt_in_requires_workspace_and_is_annotated_as_a_write():
    with pytest.raises(ValueError, match="requires --workspace"):
        mcp_server.create_server(allow_labels=True)
    assert __main__.main(["mcp", "--allow-labels"]) == 1
    tools = {t.name: t for t in asyncio.run(connection().list_tools())}
    assert not tools["update_paper_labels"].annotations.read_only_hint
    assert tools["update_paper_labels"].annotations.destructive_hint
    assert tools["update_paper_labels"].annotations.idempotent_hint
    assert not tools["update_paper_labels"].annotations.open_world_hint
    assert all(t.annotations.read_only_hint for name, t in tools.items() if name != "update_paper_labels")


def test_batch_labels_preserve_claims_notes_existing_labels_and_analysis_basis():
    original = {key: seed(key) for key in ("a", "b")}
    before = store.synthesis_basis(store.claim_rows())
    scoped = connection(papers=["a", "b"])
    result = call(scoped, papers=["a", "b", "a"], labels=["Activation steering", "  Open   source  "])
    assert result["changed"] == 2 and result["papers"] == ["a", "b"]
    for key in original:
        paper = store.load_paper(key)
        assert paper["labels"] == ["Activation steering", "Open source", "Reading list"]
        assert paper["notes"] == original[key]["notes"]
        assert paper["claims"] == original[key]["claims"]
    assert "PRIVATE" not in json.dumps(result)
    assert store.synthesis_basis(store.claim_rows()) == before
    assert call(scoped, papers=["a", "b"], labels=["Activation steering", "Open source"])["changed"] == 0
    assert call(scoped, papers=["a"], labels=["ACTIVATION STEERING"], mode="remove")["changed"] == 1
    assert store.load_paper("a")["labels"] == ["Open source", "Reading list"]
    call(scoped, papers=["a"], labels=["Reviewed later"], mode="replace")
    assert store.load_paper("a")["labels"] == ["Reviewed later"]
    call(scoped, papers=["a"], labels=[], mode="replace")
    assert store.load_paper("a")["labels"] == []


@pytest.mark.parametrize("library_snapshot", [False, True])
def test_label_edits_cannot_escape_selected_papers_or_captured_library(library_snapshot):
    for key in ("inside", "outside"):
        seed(key)
    scoped = connection(papers=["inside"], library_snapshot=library_snapshot)
    for keys in (["outside"], ["inside", "outside"]):
        with pytest.raises(ToolError, match="outside the selected sources"):
            call(scoped, papers=keys, labels=["New label"])
    assert store.load_paper("inside")["labels"] == store.load_paper("outside")["labels"] == ["Reading list"]


def test_other_workspace_and_invalid_label_inputs_are_rejected_before_http():
    requests = []
    scoped = mcp_server.create_server(workspace="default", allow_labels=True, papers=["a"],
        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200, json={})))
    for extra in ({"workspace": "another"}, {"papers": ["../a"]}, {"papers": []},
                  {"papers": ["a"] * 201}, {"labels": ["review, theory"]},
                  {"labels": ["x" * 201]}, {"labels": ["label"] * 101},
                  {"labels": [12]}, {"mode": "delete"}):
        with pytest.raises(ToolError):
            asyncio.run(scoped.call_tool("update_paper_labels", {
                "workspace": "default", "papers": ["a"], "labels": ["Theory"], **extra}))
    assert requests == []


def test_same_key_in_another_workspace_is_untouched_and_missing_batch_fails_before_writes():
    seed("a")
    other = config.create_workspace("Other")["id"]
    with config.use_workspace(other):
        seed("a")
    scoped = connection()
    with pytest.raises(ToolError, match="no longer exists"):
        call(scoped, papers=["a", "gone"], labels=["New label"])
    assert store.load_paper("a")["labels"] == ["Reading list"]
    call(scoped, papers=["a"], labels=["New label"])
    with config.use_workspace(other):
        assert store.load_paper("a")["labels"] == ["Reading list"]


def test_label_write_does_not_consume_evidence_budget_or_invent_evidence(tmp_path):
    seed("a")
    log = tmp_path / "evidence.jsonl"
    scoped = connection(papers=["a"], evidence_log=str(log), evidence_limit=0)
    result = call(scoped, papers=["a"], labels=["Content label"])
    assert result["changed"] == 1 and "evidence" not in result
    assert not log.exists()


@pytest.mark.parametrize('arguments', [
    {'papers': ['a'], 'labels': [12]}, {'papers': 'a', 'labels': None},
])
def test_invalid_label_requests_can_still_be_reported_as_activity(arguments):
    events = [{'type': 'item.completed', 'item': {'type': 'mcp_tool_call', 'server': 'doxograph',
        'tool': 'update_paper_labels', 'arguments': arguments}}]
    assert agent.activity(events)[0].startswith('Requested label update')


@pytest.mark.parametrize("provider", ["claude_code", "codex"])
@pytest.mark.parametrize("mode", ["paper", "selected", "library", "legacy"])
def test_ask_enables_label_tool_for_both_agents_in_every_scope(provider, mode, monkeypatch):
    import tomllib

    paper = seed("a")
    store.pdf_path("a").write_bytes(minimal_pdf(["Full paper text about activation steering."]))
    config.set_ai_provider(provider)
    tool_args = {"workspace": "default", "papers": [paper["key"]], "labels": ["Activation steering"]}
    answer = json.dumps({"answer": "Labeled the paper."}) if mode in ("paper", "legacy") else "Labeled the paper."
    if provider == "claude_code":
        events = stream({"type": "assistant", "message": {"content": [{"type": "tool_use",
            "name": "mcp__doxograph__update_paper_labels", "input": tool_args}]}},
            {"type": "result", "result": answer, "structured_output": {"answer": "Labeled the paper."}})
        seen = fake_claude(monkeypatch, events)
    else:
        events = stream({"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "doxograph",
            "tool": "update_paper_labels", "arguments": tool_args}}, {"type": "turn.completed"})
        seen = fake_codex(monkeypatch, events, answer=answer)
    from doxograph import chat_api
    monkeypatch.setattr(chat_api, "_reachable", lambda url: True)
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = {"agent": provider, "messages": [{"role": "user", "content": "Label this paper based on its content."}]}
        if mode != "legacy":
            body["scope"] = {"mode": mode, "papers": ["a"]}
        response = client.post("/api/papers/a/chat" if mode == "legacy" else "/api/ask", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["activity"] == ["Requested label update (add): Activation steering · a"]
    if provider == "claude_code":
        entry = json.loads(seen["args"][seen["args"].index("--mcp-config") + 1])["mcpServers"]["doxograph"]
        assert "mcp__doxograph__update_paper_labels" in seen["args"][seen["args"].index("--allowedTools") + 1]
    else:
        args = seen["args"]
        overrides = tomllib.loads("\n".join(args[i + 1] for i, arg in enumerate(args) if arg == "-c"))
        entry = overrides["mcp_servers"]["doxograph"]
        assert "update_paper_labels" in entry["enabled_tools"]
    assert "--allow-edits" in entry["args"] and "--workspace" in entry["args"]
    assert "--without-notes" not in entry["args"]
    if mode == "legacy":
        assert entry["args"][-2:] == ["--papers", "a"]
    else:
        assert "--papers-file" in entry["args"]
    if mode in ("paper", "legacy"):
        if provider == "codex":
            assert "Full paper text" in seen["prompt"]
        else:
            content = json.loads(seen["prompt"])["message"]["content"]
            assert content[0]["type"] == "document" and content[0]["source"]["media_type"] == "application/pdf"
    # The real tool persistence is exercised above; these fake provider events only
    # verify tool wiring and activity, not an actual model or classification.
