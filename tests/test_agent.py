"""The library agent reads claims through read-only tools and never sees personal notes."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from doxograph import agent, agent_tools, claude_code, config, server, store
from doxograph.model_errors import ModelSetupError

PRIVATE = "PRIVATE-NOTE-SENTINEL"


def seed():
    paper = store.new_paper("doe2026", title="Steering Recovery", authors=["Doe"], year=2026,
                            summary="Models recover.", notes=PRIVATE)
    claim = store.new_claim(paper, text="Models recover from steering.", tags=["steering"],
                            quote="recovery was observed", note=PRIVATE,
                            ledger_links=[{"claim": "mine-1", "relation": "supports", "note": PRIVATE}])
    paper["claims"].append(claim)
    store.save_paper(paper)
    store.save_tags([{"name": "steering", "description": "Activation steering"}])
    store.save_context(PRIVATE)
    return claim


def test_snapshot_leaves_out_everything_personal():
    seed()
    text = json.dumps(agent.snapshot())
    assert "Models recover from steering." in text
    assert PRIVATE not in text
    assert "mine-1" not in text


def test_tools_search_and_read_topic():
    claim = seed()
    library = agent_tools.Library(agent.snapshot())
    found = library.call("search_claims", {"query": "STEERING recover"})
    assert [m["id"] for m in found["matches"]] == [claim["id"]]
    topic = library.call("get_topic", {"name": "steering"})
    assert topic["claims"][0]["paper"] == "doe2026"
    assert library.call("list_topics", {})["topics"][0]["claims"] == 1
    assert library.call("get_paper", {"key": "doe2026"})["claims"][0]["quote"] == "recovery was observed"
    with pytest.raises(ValueError):
        library.call("get_paper", {"key": "../etc/passwd"})


def two_paper_topics():
    """Topics A and B, with a relationship found only under B and a claim tagged A and B."""
    store.save_tags([{"name": "a", "description": ""}, {"name": "b", "description": ""}])
    ids = {}
    for key, tags in (("p1", ["a", "b"]), ("p2", ["b"]), ("p3", ["a"])):
        store.save_paper(store.new_paper(key, title=key))
        ids[key] = store.add_claim(key, {"text": f"{key} claim", "tags": tags, "reviewed": True})["id"]
    live = {r["id"]: r for r in store.claim_rows()}
    store.record_agreements("b", [{"claims": [ids["p1"], ids["p2"]], "note": "same"}], live)
    store.record_tensions("b", [{"claims": [ids["p1"], ids["p2"]], "kind": "tension", "note": "differ"}], live)
    return ids, live


def test_topic_lists_only_relationships_found_under_that_topic():
    two_paper_topics()
    library = agent_tools.Library(agent.snapshot())
    assert library.call("get_topic", {"name": "a"})["agreements"] == []
    assert library.call("get_topic", {"name": "a"})["tensions"] == []
    assert len(library.call("get_topic", {"name": "b"})["agreements"]) == 1
    assert len(library.call("get_topic", {"name": "b"})["tensions"]) == 1


def test_agreement_losing_a_paper_is_not_shown_and_an_edit_marks_it_stale():
    ids, live = two_paper_topics()
    store.update_claim("p2", ids["p2"], {"text": "p2 claim, reworded"})
    group = agent_tools.Library(agent.snapshot()).call("get_topic", {"name": "b"})["agreements"][0]
    assert group["stale"] is True
    store.delete_paper("p2")
    assert agent_tools.Library(agent.snapshot()).call("get_topic", {"name": "b"})["agreements"] == []


def test_synthesis_reports_when_claims_changed_after_it_was_written():
    claim = seed()
    live = {r["id"]: r for r in store.claim_rows()}
    store.record_synthesis("steering", "Recovery happens.", live)
    topic = lambda: agent_tools.Library(agent.snapshot()).call("get_topic", {"name": "steering"})
    assert topic()["synthesis"] == "Recovery happens." and topic()["synthesis_stale"] is False
    store.update_claim("doe2026", claim["id"], {"text": "Models rarely recover from steering."})
    assert topic()["synthesis_stale"] is True


def test_tool_server_speaks_mcp_over_stdio(tmp_path):
    seed()
    manifest = tmp_path / "library.json"
    manifest.write_text(json.dumps(agent.snapshot()))
    requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "list_papers", "arguments": {}}}]
    done = subprocess.run([sys.executable, agent_tools.__file__, str(manifest)], capture_output=True, text=True,
                          input="".join(json.dumps(r) + "\n" for r in requests), timeout=30)
    replies = [json.loads(line) for line in done.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]
    assert {t["name"] for t in replies[1]["result"]["tools"]} == set(agent_tools.TOOL_NAMES)
    assert all(t["annotations"]["readOnlyHint"] for t in replies[1]["result"]["tools"])
    assert json.loads(replies[2]["result"]["content"][0]["text"])["papers"][0]["key"] == "doe2026"


def test_command_allows_only_library_tools():
    args = agent.command("claude", Path("/tmp/library.json"), "doe2026")
    assert args[args.index("--tools") + 1] == ""
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__doxograph__{name}" for name in agent_tools.TOOL_NAMES]
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    assert "doe2026" in args[args.index("--system-prompt") + 1]


def fake_claude(monkeypatch, stdout, returncode=0, seen=None):
    monkeypatch.setattr(claude_code, "executable", lambda: "claude")
    monkeypatch.setattr(claude_code, "environment", lambda: {})
    monkeypatch.setattr(claude_code, "require_subscription", lambda *a: None)

    def run(args, *, env, cwd, input=None, timeout=600):
        if seen is not None:
            seen["manifest"] = (Path(cwd) / "library.json").read_text()
            seen["prompt"] = input
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    monkeypatch.setattr(claude_code, "_run", run)


def stream(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def test_answer_reports_the_lookups_it_made(monkeypatch):
    seed()
    config.set_ai_provider("claude_code")
    seen = {}
    fake_claude(monkeypatch, stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__doxograph__search_claims", "input": {"query": "steering"}}]}},
        {"type": "result", "result": "One paper reports recovery."}), seen=seen)
    reply = agent.answer([{"role": "user", "content": "What recovers?"}])
    assert reply["answer"] == "One paper reports recovery."
    assert reply["activity"] == ["Searched claims steering"]
    assert PRIVATE not in seen["manifest"] and "What recovers?" in seen["prompt"]


def test_answer_needs_a_claude_subscription():
    config.set_ai_provider("codex")
    with pytest.raises(ModelSetupError, match="Claude subscription"):
        agent.answer([{"role": "user", "content": "Hi"}])


def test_answer_surfaces_a_failed_run(monkeypatch):
    config.set_ai_provider("claude_code")
    fake_claude(monkeypatch, stream({"type": "result", "is_error": True, "result": "usage limit"}), returncode=1)
    with pytest.raises(RuntimeError, match="usage limit"):
        agent.answer([{"role": "user", "content": "Hi"}])


def test_endpoint_maps_setup_errors_and_the_off_switch():
    body = {"messages": [{"role": "user", "content": "Hi"}], "paper": "doe2026"}
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        config.set_ai_provider("codex")
        response = client.post("/api/agent/chat", json=body)
        assert response.status_code == 502 and "Claude subscription" in response.json()["detail"]
        config.set_ai_enabled(False)
        assert client.post("/api/agent/chat", json=body).status_code == 403
