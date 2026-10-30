"""The library agent reads the corpus through Doxograph's MCP server, without personal notes."""

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from doxograph import agent, chat_api, claude_code, codex_cli, config, mcp_server, server, store
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
    return claim


def tools_called(without_notes, calls):
    """Run MCP tool calls against the real API in-process; return the results."""
    async def run():
        mcp = mcp_server.create_server(transport=httpx.ASGITransport(app=server.app), without_notes=without_notes)
        return [await mcp.call_tool(name, {"workspace": "default", **arguments}) for name, arguments in calls]
    return asyncio.run(run())


def test_without_notes_the_tools_return_nothing_personal_and_do_not_search_notes():
    seed()
    results = tools_called(True, [("get_paper", {"key": "doe2026"}),
                                  ("search_claims", {"q": "steering"}),
                                  ("search_claims", {"q": PRIVATE})])
    text = json.dumps([r.structured_content for r in results], default=str)
    assert "Models recover from steering." in text
    assert PRIVATE not in text and "mine-1" not in text
    assert results[2].structured_content["total"] == 0


def test_with_notes_the_same_tools_still_return_them():
    seed()
    [paper, claims] = tools_called(False, [("get_paper", {"key": "doe2026"}), ("search_claims", {"q": PRIVATE})])
    assert paper.structured_content["paper"]["notes"] == PRIVATE
    assert claims.structured_content["total"] == 1


def test_without_notes_no_notes_tools_are_registered():
    names = lambda without: {t.name for t in asyncio.run(mcp_server.create_server(without_notes=without).list_tools())}
    assert {"list_notes", "read_note"} <= names(False)
    assert not {"list_notes", "read_note"} & names(True)
    assert set(agent.TOOLS) <= names(True)


def test_command_allows_only_library_tools_and_runs_this_codes_server():
    args = agent.command("claude", "http://127.0.0.1:9", "default", "doe2026")
    assert args[args.index("--tools") + 1] == ""
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert allowed == [f"mcp__doxograph__{name}" for name in agent.TOOLS]
    assert not any("note" in name for name in allowed)
    assert "--strict-mcp-config" in args and "--no-session-persistence" in args
    system = args[args.index("--system-prompt") + 1]
    assert "doe2026" in system and "workspace='default'" in system
    entry = json.loads(args[args.index("--mcp-config") + 1])["mcpServers"]["doxograph"]
    assert entry["command"] == sys.executable
    assert entry["args"][-5:] == ["--url", "http://127.0.0.1:9", "--without-notes", "--workspace", "default"]
    assert Path(entry["env"]["PYTHONPATH"], "doxograph", "agent.py").is_file()


def fake_claude(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(claude_code, "executable", lambda: "claude")
    monkeypatch.setattr(claude_code, "environment", lambda: {})
    monkeypatch.setattr(claude_code, "require_subscription", lambda *a: None)
    seen = {}

    def run(args, *, env, cwd, input=None, timeout=600):
        seen["args"], seen["prompt"] = args, input
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)
    monkeypatch.setattr(claude_code, "_run", run)
    return seen


def stream(*events):
    return "".join(json.dumps(e) + "\n" for e in events)


def test_answer_reports_the_lookups_it_made(monkeypatch):
    config.set_ai_provider("claude_code")
    seen = fake_claude(monkeypatch, stream(
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "mcp__doxograph__search_claims", "input": {"q": "steering"}},
            {"type": "tool_use", "name": "mcp__doxograph__read_evidence", "input": {"kind": "tensions"}}]}},
        {"type": "result", "result": "One paper reports recovery."}))
    reply = agent.answer([{"role": "user", "content": "What recovers?"}], lambda: "http://127.0.0.1:8765")
    assert reply["answer"] == "One paper reports recovery."
    assert reply["activity"] == ["Searched claims steering", "Read tensions"]
    assert "What recovers?" in seen["prompt"]


def fake_codex(monkeypatch, stdout, *, answer="Two papers agree.", auth="Logged in using ChatGPT", returncode=0):
    monkeypatch.setattr(codex_cli, "executable", lambda: "codex")
    monkeypatch.setattr(codex_cli, "environment", lambda: {})
    seen = {}

    def run(args, *, env, cwd, input=None, timeout=600):
        if "status" in args:
            return SimpleNamespace(stdout=auth, stderr="", returncode=0)
        seen["args"], seen["prompt"] = args, input
        seen["instructions"] = Path(next(a.split("=", 1)[1].strip('"') for a in args
                                         if a.startswith("model_instructions_file="))).read_text()
        if answer is not None:
            Path(args[args.index("--output-last-message") + 1]).write_text(answer)
        return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(codex_cli, "_run", run)
    return seen


def test_codex_library_uses_selected_model_and_only_the_notes_free_library_tools(monkeypatch):
    import tomllib

    config.set_ai_provider("codex")
    config.save_codex_model("chosen-model")
    seen = fake_codex(monkeypatch, stream(
        {"type": "item.started", "item": {"type": "mcp_tool_call", "server": "doxograph", "tool": "search_claims"}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "doxograph",
                                            "tool": "search_claims", "arguments": {"q": "steering"}}},
        {"type": "item.completed", "item": {"type": "mcp_tool_call", "server": "doxograph",
                                            "tool": "read_evidence", "arguments": {"kind": "tensions"}}},
        {"type": "turn.completed"}))
    other = config.create_workspace("Other")
    messages = [{"role": "user", "content": "What agrees?"},
                {"role": "assistant", "content": "Two papers."},
                {"role": "user", "content": "And disagrees?"}]
    with config.use_workspace(other["id"]):
        reply = agent.answer(messages, lambda: "http://127.0.0.1:9000", focus="doe2026")
    assert reply == {"answer": "Two papers agree.", "activity": ["Searched claims steering", "Read tensions"],
                     "model": "chosen-model", "assistant": "ChatGPT"}
    assert all(message["content"] in seen["prompt"] for message in messages)
    assert f"workspace='{other['id']}'" in seen["instructions"] and "doe2026" in seen["instructions"]
    args = seen["args"]
    assert args[args.index("--model") + 1] == "chosen-model"
    overrides = tomllib.loads("\n".join(args[i + 1] for i, arg in enumerate(args) if arg == "-c"))
    entry = overrides["mcp_servers"]["doxograph"]
    assert entry["command"] == sys.executable
    assert entry["args"] == ["-m", "doxograph", "mcp", "--url", "http://127.0.0.1:9000",
                             "--without-notes", "--workspace", other["id"]]
    assert entry["enabled_tools"] == list(agent.TOOLS) and entry["required"]
    assert Path(entry["env"]["PYTHONPATH"], "doxograph", "agent.py").is_file()
    assert "--ignore-user-config" in args and "--ephemeral" in args
    assert overrides["tools"]["view_image"] is False
    assert "shell_tool" in args and "multi_agent" in args


@pytest.mark.parametrize("status", ["Not logged in", "Logged in using an API key"])
def test_codex_library_checks_chatgpt_login_before_callback(monkeypatch, status):
    config.set_ai_provider("codex")
    seen = fake_codex(monkeypatch, "", auth=status)
    with pytest.raises(ModelSetupError, match="will not fall back"):
        agent.answer([], lambda: pytest.fail("probed without ChatGPT login"))
    assert seen == {}


def test_codex_library_keeps_provider_and_model_if_settings_change_during_request(monkeypatch):
    config.set_ai_provider("codex")
    config.save_codex_model("submitted-model")
    seen = fake_codex(monkeypatch, stream({"type": "turn.completed"}))

    def callback():
        config.set_ai_provider("claude_code")
        config.save_codex_model("next-model")
        return "http://127.0.0.1:8765"

    reply = agent.answer([], callback)
    assert reply["model"] == "submitted-model" and reply["assistant"] == "ChatGPT"
    assert seen["args"][seen["args"].index("--model") + 1] == "submitted-model"
    assert config.ai_provider() == "claude_code" and config.codex_model() == "next-model"


@pytest.mark.parametrize("stdout,answer", [
    (stream({"type": "turn.failed", "error": {"message": "Usage limit"}}), "partial"),
    (stream({"type": "turn.started"}), "partial"),
    ("invalid JSON", "answer"),
    (stream({"type": "turn.completed"}), None),
    (stream({"type": "turn.completed"}), ""),
])
def test_codex_library_rejects_failed_or_incomplete_answers(monkeypatch, stdout, answer):
    config.set_ai_provider("codex")
    fake_codex(monkeypatch, stdout, answer=answer)
    with pytest.raises(RuntimeError, match="Codex"):
        agent.answer([], lambda: "http://127.0.0.1:8765")


def test_codex_endpoint_checks_callback_and_the_ai_off_switch(monkeypatch):
    config.set_ai_provider("codex")
    seen = fake_codex(monkeypatch, stream({"type": "turn.completed"}))
    monkeypatch.setattr(chat_api, "_reachable", lambda url: False)
    body = {"messages": [{"role": "user", "content": "Hi"}]}
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/agent/chat", json=body)
        assert response.status_code == 502 and "loopback" in response.json()["detail"]
        assert seen == {}
        monkeypatch.setattr(chat_api, "_reachable", lambda url: True)
        response = client.post("/api/agent/chat", json=body)
        assert response.status_code == 200 and response.json()["assistant"] == "ChatGPT"
        config.set_ai_enabled(False)
        monkeypatch.setattr(codex_cli, "_run", lambda *a, **kw: pytest.fail("Codex ran with AI off"))
        assert client.post("/api/agent/chat", json=body).status_code == 403


def test_answer_needs_a_subscription():
    config.set_ai_provider("api")
    with pytest.raises(ModelSetupError, match="Claude subscription"):
        agent.answer([{"role": "user", "content": "Hi"}], lambda: "http://127.0.0.1:8765")


def test_answer_surfaces_a_failed_run(monkeypatch):
    config.set_ai_provider("claude_code")
    fake_claude(monkeypatch, stream({"type": "result", "is_error": True, "result": "usage limit"}), returncode=1)
    with pytest.raises(RuntimeError, match="usage limit"):
        agent.answer([{"role": "user", "content": "Hi"}], lambda: "http://127.0.0.1:8765")


def test_endpoint_calls_back_on_its_own_port_in_the_current_workspace(monkeypatch):
    config.set_ai_provider("claude_code")
    monkeypatch.setattr(chat_api, "_reachable", lambda url: True)
    seen = fake_claude(monkeypatch, stream({"type": "result", "result": "Ok."}))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        assert client.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "Hi"}]}).json()["answer"] == "Ok."
    config_arg = json.loads(seen["args"][seen["args"].index("--mcp-config") + 1])["mcpServers"]["doxograph"]
    assert "--url" in config_arg["args"] and config_arg["args"][config_arg["args"].index("--url") + 1].startswith("http://127.0.0.1:")


@pytest.mark.parametrize("host,expected", [("127.0.0.1", "http://127.0.0.1:8765"),
                                           ("127.0.0.2", "http://127.0.0.2:8765"),
                                           ("localhost", "http://localhost:8765")])
def test_agent_calls_back_on_the_address_the_request_arrived_on(monkeypatch, host, expected):
    config.set_ai_provider("claude_code")
    urls = []
    monkeypatch.setattr(chat_api, "_reachable", lambda url: True)
    monkeypatch.setattr(agent, "answer", lambda messages, callback_url, focus=None: urls.append(callback_url()) or {"answer": "Ok."})
    with TestClient(server.app, base_url=f"http://{host}:8765") as client:
        assert client.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "Hi"}]}).status_code == 200
    assert urls == [expected]


def test_callback_url_falls_back_to_loopback_for_other_addresses(monkeypatch):
    monkeypatch.setattr(chat_api, "_reachable", lambda url: True)
    request = SimpleNamespace(scope={"server": ("192.168.1.5", 9000)})
    assert chat_api._callback_url(request) == "http://127.0.0.1:9000"
    assert chat_api._callback_url(SimpleNamespace(scope={"server": ("::1", 9000)})) == "http://[::1]:9000"
    assert chat_api._callback_url(SimpleNamespace(scope={})) == "http://127.0.0.1:8765"


def test_unreachable_callback_is_rejected_with_setup_guidance_before_any_model_call(monkeypatch):
    config.set_ai_provider("claude_code")
    probed = []
    monkeypatch.setattr(chat_api, "_reachable", lambda url: probed.append(url) or False)
    fake_claude(monkeypatch, "")
    monkeypatch.setattr(claude_code, "_run", lambda *a, **k: pytest.fail("Claude Code ran without a reachable callback"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 502
    assert "loopback" in response.json()["detail"] and "--host 127.0.0.1" in response.json()["detail"]
    assert probed == ["http://127.0.0.1:8765"]


def test_reachable_checks_the_health_route_of_a_real_server():
    import socket
    import threading
    import time

    import uvicorn
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    port = sock.getsockname()[1]
    http = uvicorn.Server(uvicorn.Config(server.app, log_level="error", lifespan="off"))
    thread = threading.Thread(target=lambda: http.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not http.started and time.time() < deadline:
            time.sleep(0.05)
        assert chat_api._reachable(f"http://127.0.0.1:{port}")
    finally:
        http.should_exit = True
        thread.join(timeout=10)
    assert not chat_api._reachable(f"http://127.0.0.1:{port}")


def test_the_provider_is_checked_before_the_callback_is_probed(monkeypatch):
    config.set_ai_provider("api")
    monkeypatch.setattr(chat_api, "_reachable", lambda url: pytest.fail("probed before the cheap checks"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/agent/chat", json={"messages": [{"role": "user", "content": "Hi"}]})
    assert response.status_code == 502 and "Claude subscription" in response.json()["detail"]


@pytest.mark.parametrize("payload", [{"app": "doxograph", "instance": "someone-else"},
                                     {"app": "other", "instance": None},
                                     ["not", "a", "dict"]])
def test_reachable_requires_this_process_to_answer(monkeypatch, payload):
    from doxograph import fingerprint
    payload = payload if not isinstance(payload, dict) or payload.get("instance") is not None else {**payload, "instance": fingerprint.INSTANCE}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=payload, request=httpx.Request("GET", a[0])))
    assert chat_api._reachable("http://127.0.0.1:1") is False
    good = {"app": "doxograph", "instance": fingerprint.INSTANCE}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.Response(200, json=good, request=httpx.Request("GET", a[0])))
    assert chat_api._reachable("http://127.0.0.1:1") is True


def test_endpoint_maps_setup_errors_and_the_off_switch(monkeypatch):
    monkeypatch.setattr(chat_api, "_reachable", lambda url: pytest.fail("probed before the cheap checks"))
    body = {"messages": [{"role": "user", "content": "Hi"}], "paper": "doe2026"}
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        config.set_ai_provider("api")
        response = client.post("/api/agent/chat", json=body)
        assert response.status_code == 502 and "Claude subscription" in response.json()["detail"]
        config.set_ai_enabled(False)
        assert client.post("/api/agent/chat", json=body).status_code == 403
