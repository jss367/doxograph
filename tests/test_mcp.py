"""The MCP boundary speaks stdio and can only issue workspace-scoped reads."""

import asyncio
import json
import socket
import sys

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

from doxograph import __main__, config, server, store
from doxograph.mcp_server import create_server, local_url


def body(result):
    assert not result.is_error, result
    data = json.loads(result.content[0].text)
    assert result.structured_content == data
    return data


def test_tools_are_read_only_and_require_explicit_workspaces():
    async def run():
        tools = await create_server().list_tools()
        assert tools
        for tool in tools:
            assert tool.annotations.read_only_hint and not tool.annotations.open_world_hint
            if tool.name != "list_workspaces":
                assert "workspace" in tool.input_schema["required"]
        s = create_server(transport=httpx.MockTransport(lambda r: pytest.fail("invalid input reached HTTP")))
        for arguments in [{}, {"workspace": ""}, {"workspace": "default", "limit": 101}]:
            with pytest.raises(ToolError):
                await s.call_tool("search_claims", arguments)
        with pytest.raises(ToolError):
            await s.call_tool("get_paper", {"workspace": "default", "key": "../settings"})
    asyncio.run(run())


def test_adapter_queries_real_api_without_state_dump_or_writes():
    other = config.create_workspace("Other")["id"]
    with config.use_workspace(other):
        store.save_paper(store.new_paper("example", title="Local recovery"))
        claim = store.add_claim("example", {"text": "Recovery happens", "reviewed": False})
    before = store.paper_path("example")  # Default workspace has no such paper.
    assert not before.exists()

    async def run():
        paths = []
        api = httpx.ASGITransport(app=server.app)

        class ReadTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                assert request.method == "GET" and request.url.path != "/api/state"
                paths.append(request.url.path)
                return await api.handle_async_request(request)

        s = create_server(transport=ReadTransport())
        data = body(await s.call_tool("search_claims", {"workspace": other, "q": "recovery", "reviewed": False}))
        assert data["workspace"] == other and data["items"][0]["id"] == claim["id"]
        assert body(await s.call_tool("search_claims", {"workspace": "default"}))["items"] == []
        assert body(await s.call_tool("get_paper", {"workspace": other, "key": "example"}))["paper"]["title"] == "Local recovery"
        with pytest.raises(ToolError, match="no workspace missing"):
            await s.call_tool("search_claims", {"workspace": "missing"})
        assert paths
    asyncio.run(run())


@pytest.mark.parametrize("without_notes", [False, True])
def test_bound_connection_rejects_other_workspaces_before_http(without_notes):
    async def run():
        requests = []

        def respond(request):
            requests.append(request)
            assert request.headers["X-Doxograph-Workspace"] == "selected"
            return httpx.Response(200, json={"workspace": "selected"})

        scoped = create_server(workspace="selected", without_notes=without_notes,
                               transport=httpx.MockTransport(respond))
        tools = {tool.name for tool in await scoped.list_tools()}
        assert "list_workspaces" not in tools
        calls = [("search_papers", {}), ("search_claims", {}),
                 ("search_paper_text", {"q": "recovery"}), ("get_paper", {"key": "example"}),
                 ("list_topics", {}), ("read_evidence", {"kind": "tensions"}), ("read_passage", {"key": "example"})]
        if not without_notes:
            calls += [("list_notes", {}), ("read_note", {"note_id": "workspace"})]
        assert tools == {name for name, _ in calls}
        for name, arguments in calls:
            with pytest.raises(ToolError, match="restricted to workspace 'selected'"):
                await scoped.call_tool(name, {"workspace": "another", **arguments})
        assert requests == []
        for name, arguments in calls:
            assert body(await scoped.call_tool(name, {"workspace": "selected", **arguments}))["workspace"] == "selected"
        assert len(requests) == len(calls)
        if without_notes:
            assert all(r.url.params["omit_notes"] == "true" for r in requests if r.url.path.startswith("/api/query/"))

    asyncio.run(run())


def test_bound_connection_reads_selected_corpus_without_personal_notes():
    private = "PRIVATE-NOTE-SENTINEL"
    store.save_paper(store.new_paper("shared-key", title="Selected paper", notes=private))
    store.add_claim("shared-key", {"text": "Selected finding", "note": private})
    other = config.create_workspace("Other")["id"]
    with config.use_workspace(other):
        store.save_paper(store.new_paper("shared-key", title="OTHER-CORPUS-SENTINEL"))
        store.add_claim("shared-key", {"text": "OTHER-CORPUS-SENTINEL"})

    async def run():
        scoped = create_server(workspace="default", without_notes=True,
                               transport=httpx.ASGITransport(app=server.app))
        paper = body(await scoped.call_tool("get_paper", {"workspace": "default", "key": "shared-key"}))
        claims = body(await scoped.call_tool("search_claims", {"workspace": "default"}))
        assert paper["paper"]["title"] == "Selected paper"
        assert claims["items"][0]["text"] == "Selected finding"
        combined = json.dumps([paper, claims])
        assert private not in combined and "OTHER-CORPUS-SENTINEL" not in combined
        for name, arguments in [("get_paper", {"key": "shared-key"}), ("search_claims", {})]:
            with pytest.raises(ToolError, match="restricted to workspace 'default'"):
                await scoped.call_tool(name, {"workspace": other, **arguments})

    asyncio.run(run())


@pytest.mark.parametrize("workspace", ["", "../other", "Other Space"])
def test_bound_connection_requires_a_valid_workspace_id(workspace):
    with pytest.raises(ValueError, match="valid workspace ID"):
        create_server(workspace=workspace)


@pytest.mark.parametrize("status,payload,message", [
    (404, {"detail": "Not Found"}, "restart its server"),
    (404, {"detail": "no paper gone"}, "no paper gone"),
    (500, {"detail": "Unreadable storage"}, "Unreadable storage"),
    (302, {}, "HTTP 302"),
])
def test_api_errors_are_actionable(status, payload, message):
    async def run():
        s = create_server(transport=httpx.MockTransport(lambda r: httpx.Response(status, json=payload)))
        with pytest.raises(ToolError, match=message):
            await s.call_tool("get_paper", {"workspace": "default", "key": "gone"})
    asyncio.run(run())


@pytest.mark.parametrize("tool,arguments,response,message", [
    ("get_paper", {"workspace": "default", "key": "gone"}, httpx.Response(404, text="not json"), "restart its server"),
    ("list_workspaces", {}, httpx.Response(404, text="not json"), "HTTP 404: request failed"),
    ("list_workspaces", {}, httpx.Response(200, text="not json"), "invalid JSON response"),
    ("list_workspaces", {}, httpx.Response(200, json=["not", "an", "object"]), "unexpected response"),
])
def test_malformed_responses_are_actionable(tool, arguments, response, message):
    async def run():
        s = create_server(transport=httpx.MockTransport(lambda r: response))
        with pytest.raises(ToolError, match=message):
            await s.call_tool(tool, arguments)
    asyncio.run(run())


def test_selected_sources_travel_in_the_request_body():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={})

    async def run():
        scoped = create_server(workspace="default", papers=["b", "a", "b"], library_snapshot=True,
                               transport=httpx.MockTransport(respond))
        for name, arguments in [("read_evidence", {"kind": "tensions"}), ("list_topics", {}), ("get_paper", {"key": "a"})]:
            await scoped.call_tool(name, {"workspace": "default", **arguments})

    asyncio.run(run())
    evidence, topics, paper = requests
    for request in (evidence, topics):
        assert request.method == "POST" and json.loads(request.content) == {"paper_keys": ["b", "a"]}
        assert request.url.params["omit_notes"] == "true"
    assert evidence.url.params["library_snapshot"] == "true" and "library_snapshot" not in topics.url.params
    assert paper.method == "GET" and paper.url.path == "/api/query/papers/a" and not paper.content


def test_evidence_log_refuses_to_grow_past_its_size_cap(tmp_path):
    log = tmp_path / "evidence.jsonl"
    log.write_text("[]\n" * 666_667)
    before = log.stat().st_size

    async def run():
        s = create_server(workspace="default", evidence_log=str(log),
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        with pytest.raises(ToolError, match=r"already retrieved\.$"):
            await s.call_tool("search_claims", {"workspace": "default"})
    asyncio.run(run())
    assert log.stat().st_size == before


def test_offline_server_error_explains_how_to_start_it():
    def offline(request):
        raise httpx.ConnectError("offline", request=request)

    async def run():
        with pytest.raises(ToolError, match="doxograph serve"):
            await create_server(transport=httpx.MockTransport(offline)).call_tool("list_workspaces", {})
    asyncio.run(run())


@pytest.mark.parametrize("url", ["https://example.com", "http://192.168.1.2:8765", "file:///tmp/data",
                                "http://user:secret@localhost:8765", "http://localhost:8765/api",
                                "http://localhost:0", "http://localhost:8765?workspace=other"])
def test_adapter_only_connects_to_local_http_origins(url):
    with pytest.raises(ValueError, match="local HTTP URL"):
        local_url(url)


def test_mcp_command_does_not_initialize_or_recover_a_local_corpus(monkeypatch):
    from doxograph import mcp_server

    urls = []
    monkeypatch.setattr(mcp_server, "run", lambda url, **options: urls.append((url, options)))
    monkeypatch.setattr(config, "ensure_dirs", lambda: pytest.fail("adapter initialized local corpus"))
    monkeypatch.setattr(store, "recover_storage", lambda: pytest.fail("adapter recovered local corpus"))
    assert __main__.main(["mcp", "--url", "http://localhost:8766"]) == 0
    assert urls == [("http://localhost:8766", {"without_notes": False, "workspace": None, "papers": None, "papers_file": None, "evidence_log": None, "library_snapshot": False, "evidence_limit": 500, "allow_labels": False, "allow_edits": False})]
    assert __main__.main(["mcp", "--without-notes"]) == 0
    assert urls[-1] == ("http://127.0.0.1:8765", {"without_notes": True, "workspace": None, "papers": None, "papers_file": None, "evidence_log": None, "library_snapshot": False, "evidence_limit": 500, "allow_labels": False, "allow_edits": False})
    assert __main__.main(["mcp", "--without-notes", "--workspace", "selected"]) == 0
    assert urls[-1] == ("http://127.0.0.1:8765", {"without_notes": True, "workspace": "selected", "papers": None, "papers_file": None, "evidence_log": None, "library_snapshot": False, "evidence_limit": 500, "allow_labels": False, "allow_edits": False})


@pytest.mark.parametrize("workspace", [None, "default"])
def test_real_stdio_handshake_and_workspace_query(tmp_path, workspace):
    store.save_paper(store.new_paper("wire", title="A paper over MCP"))
    store.add_claim("wire", {"text": "Evidence over stdio", "locator": "p. 2"})
    adapter_data = tmp_path / "must-not-be-created"

    async def run():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            http_server = uvicorn.Server(uvicorn.Config(server.app, log_level="error", lifespan="off"))
            task = asyncio.create_task(http_server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(20):
                    while not http_server.started:
                        await asyncio.sleep(0.01)
                    params = StdioServerParameters(
                        command=sys.executable, args=["-m", "doxograph", "mcp", "--url", f"http://127.0.0.1:{port}"]
                        + (["--workspace", workspace] if workspace is not None else []),
                        env={"DOXOGRAPH_DATA": str(adapter_data)},
                    )
                    async with stdio_client(params) as streams:
                        async with ClientSession(*streams) as session:
                            await session.initialize()
                            tools = await session.list_tools()
                            assert "search_claims" in {t.name for t in tools.tools}
                            if workspace is None:
                                assert body(await session.call_tool("list_workspaces", {}))["items"][0]["id"] == "default"
                            else:
                                assert "list_workspaces" not in {t.name for t in tools.tools}
                            result = await session.call_tool("search_claims", {"workspace": "default", "q": "stdio"})
                            [claim] = body(result)["items"]
                            assert claim["paper"] == "wire" and claim["locator"] == "p. 2"
                            error = await session.call_tool("search_claims", {"workspace": "missing"})
                            assert error.is_error
                            assert ("no workspace missing" if workspace is None else "restricted to workspace") in error.content[0].text
            finally:
                http_server.should_exit = True
                await task
    asyncio.run(run())
    assert not adapter_data.exists()
