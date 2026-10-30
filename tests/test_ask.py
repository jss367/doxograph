"""Ask scope is enforced before retrieval and evidence survives follow-ups."""
import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp.server.mcpserver.exceptions import ToolError

from doxograph import agent, ask_context, chat, config, mcp_server, search, server, store


def seed(key, text=None):
    paper = store.new_paper(key, title=f"Study {key}", notes="PRIVATE")
    claim = store.new_claim(paper, text=text or f"Finding from {key}", quote="Original quote", tags=["recovery"], note="PRIVATE")
    paper["claims"].append(claim)
    store.save_paper(paper)
    return claim


def tool_server(keys, log=None):
    return mcp_server.create_server(workspace="default", papers=keys, without_notes=True,
        evidence_log=str(log) if log else None, transport=httpx.ASGITransport(app=server.app))


def call(mcp, name, **args):
    return asyncio.run(mcp.call_tool(name, {"workspace": "default", **args})).structured_content


def test_scoped_tools_filter_before_pagination_and_reject_direct_reads(tmp_path, monkeypatch):
    for i in range(25):
        seed(f"a{i:02}")
    claim = seed("z-selected")
    log = tmp_path / "evidence.jsonl"
    mcp = tool_server(["z-selected"], log)
    papers = call(mcp, "search_papers", limit=1)
    assert papers["total"] == 1 and papers["next_offset"] is None
    assert papers["items"][0]["key"] == "z-selected"
    claims = call(mcp, "search_claims", limit=1)
    assert claims["total"] == 1 and claims["items"][0]["id"] == claim["id"]
    assert "PRIVATE" not in json.dumps(claims)
    assert "list_topics" not in {t.name for t in asyncio.run(mcp.list_tools())}
    for name, args in [("get_paper", {"key": "a00"}), ("read_passage", {"key": "a00"}),
                       ("search_claims", {"paper": "a00"})]:
        with pytest.raises(ToolError, match="outside"):
            call(mcp, name, **args)
    monkeypatch.setattr(search, "paper_text", lambda key: "First page\fSecond page" if key == "z-selected" else pytest.fail("read excluded paper"))
    passage = call(mcp, "read_passage", key="z-selected", page_number=2, limit=6)
    assert passage["text"] == "Second" and passage["next_offset"] == 6
    assert passage["evidence"][0]["page"] == 2
    assert {e["paper"] for e in ask_context.read_log(log)} == {"z-selected"}
    assert call(mcp, "read_evidence", kind="syntheses")["items"] == []


def test_text_search_ranks_only_selected_papers(monkeypatch):
    seed("outside")
    seed("inside")
    calls = []
    monkeypatch.setattr(search, "folded_text", lambda key: calls.append(key) or "recovery recovery")
    monkeypatch.setattr(search, "paper_text", lambda key: "recovery recovery")
    result = call(tool_server(["inside"]), "search_paper_text", q="recovery")
    assert calls == ["inside"]
    assert [p["key"] for p in result["papers"]] == ["inside"]


def test_cross_scope_agreement_is_omitted_even_when_some_members_are_selected(monkeypatch):
    claims = {key: seed(key) for key in ("a", "b", "outside")}
    monkeypatch.setattr(store, "load_agreements", lambda: [{"id": "agreement", "claims": [c["id"] for c in claims.values()],
        "text": "Outside result affects this conclusion", "fingerprints": {c["id"]: "old" for c in claims.values()}}])
    result = call(tool_server(["a", "b"]), "read_evidence", kind="agreements")
    assert result["total"] == 0 and result["next_offset"] is None


def test_selected_source_deleted_mid_request_fails_explicitly():
    seed("a")
    mcp = tool_server(["a"])
    store.paper_path("a").unlink()
    with pytest.raises(ToolError, match="unavailable"):
        call(mcp, "search_claims")


def test_scoped_agent_command_binds_evidence_and_never_enables_topics(tmp_path):
    capture = ask_context.Capture(ask_context.Scope(mode="selected", papers=["a", "b"]), tmp_path / "evidence")
    token = ask_context.active.set(capture)
    try:
        command = agent.command("claude", "http://127.0.0.1:8765", "default", None)
        settings = json.loads(command[command.index("--mcp-config") + 1])["mcpServers"]["doxograph"]
        assert settings["args"][-4:] == ["--papers-file", str(tmp_path / "papers.json"), "--evidence-log", str(capture.log)]
        assert json.loads((tmp_path / "papers.json").read_text()) == ["a", "b"]
        assert "list_topics" not in command[command.index("--allowedTools") + 1]
        assert "[[e:REFERENCE]]" in command[command.index("--system-prompt") + 1]
    finally:
        ask_context.active.reset(token)


def test_ask_focus_is_loaded_without_notes_and_evidence_persists(monkeypatch):
    claim = seed("a")
    seed("b")
    config.set_ai_provider("codex")
    seen = []

    def answer(messages, callback):
        capture = ask_context.active.get()
        seen.append(capture)
        assert "PRIVATE" not in capture.prompt()
        ref = (capture.prior or capture.direct)[0]["ref"]
        return {"answer": f"A finding [[e:{ref}]] and [[e:invented]].", "assistant": "ChatGPT", "model": "test"}

    monkeypatch.setattr(agent, "answer", answer)
    body = {"agent": "codex", "scope": {"mode": "selected", "papers": ["a", "b"],
        "focus": [{"paper": "a", "claim": claim["id"], "text": "INJECTED"}]},
        "messages": [{"role": "user", "content": "Explain"}]}
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        first = client.post("/api/ask", json=body)
        assert first.status_code == 200, first.text
        result = first.json()
        assert seen[0].scope.focus[0].text == claim["text"]
        assert [c["valid"] for c in result["citations"]] == [True, False]
        assert result["evidence"][0]["text"] == claim["text"]
        conversation = uuid.uuid4().hex
        messages = [{**body["messages"][0], "scope": result["scope"]},
                    {"role": "assistant", "content": result["answer"], "evidence": result["evidence"],
                     "citations": result["citations"], "scope": result["scope"]}]
        saved = client.put(f"/api/chats/{conversation}", json={"agent": "codex", "scope": result["scope"],
            "messages": messages, "revision": 0, "operation": uuid.uuid4().hex})
        assert saved.status_code == 200, saved.text
        paper = store.load_paper("a"); paper["claims"][0]["text"] = "Edited later"; store.save_paper(paper)
        follow = {**body, "conversation_id": conversation,
            "scope": {"mode": "selected", "papers": ["a", "b"]},
            "messages": [{"role": m["role"], "content": m["content"]} for m in messages] + [{"role": "user", "content": "Limitations?"}]}
        second = client.post("/api/ask", json=follow)
        assert second.status_code == 200, second.text
        assert second.json()["evidence"][0]["text"] == claim["text"]
        assert second.json()["evidence"][0]["carried"] is True
        narrowed = client.post("/api/ask", json={**follow, "scope": {"mode": "selected", "papers": ["a"]}})
        assert narrowed.status_code == 409
        assert len(seen) == 2
        assert client.get('/api/chats?paper=b').json()['chats'][0]['id'] == conversation


def test_ask_does_not_switch_provider_and_rejects_empty_sources(monkeypatch):
    seed("a")
    monkeypatch.setattr(agent, "answer", lambda *args: pytest.fail("unsupported provider ran"))
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        body = {"agent": "api", "messages": [{"role": "user", "content": "Question"}],
                "scope": {"mode": "selected", "papers": ["a"]}}
        assert client.post('/api/ask', json=body).status_code == 422
        body['scope']['papers'] = []
        assert client.post('/api/ask', json=body).status_code == 422
        body['scope'] = {"mode": "paper", "papers": ["a"]}
        monkeypatch.setattr(chat, "answer", lambda key, messages: {"answer": "Paper answer"})
        assert client.post('/api/ask', json=body).status_code == 200
        config.set_ai_enabled(False)
        assert client.post('/api/ask', json=body).status_code == 403


def test_document_citations_require_supplied_pages_and_never_invent_claims():
    scope = ask_context.Scope(mode="paper", papers=["a"])
    document = ask_context.entry(kind="document", paper="a", page_count=2, format="PDF with page images")
    ref = document['ref']
    result = ask_context.citations(f"[[e:{ref}:2]] [[e:{ref}:3]] [[e:made-up-claim]]", [document], scope)
    assert [c['valid'] for c in result] == [True, False, False]
    assert ask_context.citations(f"[[e:{ref}:1]]", [document], ask_context.Scope(mode='paper', papers=['b']))[0]['valid'] is False


def test_full_paper_records_the_document_and_cited_text_before_it_changes(monkeypatch):
    from doxograph import extract
    from doxograph.providers import ModelResponse
    from pdfs import minimal_pdf
    seed('a')
    store.pdf_path('a').write_bytes(minimal_pdf(['Original first page.', 'Original second page.']))
    monkeypatch.setattr(extract, 'client', lambda: object())
    monkeypatch.setattr(extract, 'paper_document', lambda key, api: {'type':'text', 'text':'[Page 1] Original first page.\n[Page 2] Original second page.'})
    def respond(api, **kwargs):
        capture = ask_context.active.get()
        document = next(e for e in capture.direct if e['kind'] == 'document')
        assert document['page_count'] == 2
        assert '[[e:REFERENCE]]' in kwargs['messages'][0]['content'][1]['text']
        return ModelResponse.from_text(json.dumps({'answer': f"Second page [[e:{document['ref']}:2]]."}))
    monkeypatch.setattr(extract, '_create', respond)
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        result = client.post('/api/ask', json={'scope':{'mode':'paper', 'papers':['a']},
            'messages':[{'role':'user', 'content':'What does the second page say?'}], 'agent':'api'})
    assert result.status_code == 200, result.text
    evidence = result.json()['evidence']
    assert evidence[0]['kind'] == 'document' and evidence[0]['format'] == 'extracted text'
    assert any(e['kind'] == 'passage' and 'Original second page.' in e['text'] for e in evidence)
    assert result.json()['citations'][0]['valid'] is True


def test_library_snapshot_keeps_topics_and_records_analysis_without_admitting_new_papers(tmp_path):
    claims = {key: seed(key) for key in ('a', 'b')}
    live = {c['id']: c for c in store.claim_rows()}
    store.save_tags([{'name':'recovery', 'description':'Recovery studies'}])
    store.record_agreements('recovery', [{'claims':list(live), 'note':'Both report improvement'}], live)
    store.record_synthesis('recovery', 'Both improve under these conditions.', live)
    log = tmp_path / 'evidence'
    mcp = mcp_server.create_server(workspace='default', papers=['a', 'b'], library_snapshot=True,
        without_notes=True, evidence_log=str(log), transport=httpx.ASGITransport(app=server.app))
    seed('new-paper')
    topics = call(mcp, 'list_topics')
    assert topics['items'][0]['n_claims'] == len(claims)
    assert call(mcp, 'search_papers')['total'] == 2
    result = call(mcp, 'read_evidence', kind='syntheses')
    assert result['items'][0]['text'] == 'Both improve under these conditions.'
    evidence = result['evidence'][0]
    assert evidence['kind'] == 'analysis' and evidence['sources'] == ['a', 'b']
    assert 'new-paper' not in json.dumps(result)
    assert not ask_context.citations(f"[[e:{evidence['ref']}]]", [evidence],
        ask_context.Scope(mode='paper', papers=['a']))[0]['valid']


def test_ask_captures_model_before_resolving_sources(monkeypatch):
    seed('a')
    config.set_ai_provider('codex')
    config.save_codex_model('submitted-model')
    resolve = ask_context.resolve_scope
    def changed_settings(scope):
        config.save_codex_model('later-model')
        return resolve(scope)
    monkeypatch.setattr(ask_context, 'resolve_scope', changed_settings)
    monkeypatch.setattr(chat, 'answer', lambda key, messages: {'answer':'Answer', 'model':config.model()})
    with TestClient(server.app, base_url='http://127.0.0.1:8765') as client:
        result = client.post('/api/ask', json={'agent':'codex', 'scope':{'mode':'paper', 'papers':['a']},
            'messages':[{'role':'user', 'content':'Question'}]})
    assert result.status_code == 200
    assert result.json()['model'] == 'submitted-model'
    assert config.codex_model() == 'later-model'


@pytest.mark.parametrize('provider', ['claude_code', 'codex'])
def test_large_library_snapshot_stays_out_of_cli_arguments(provider, monkeypatch, tmp_path):
    from test_agent import fake_claude, fake_codex, stream
    keys = [f'{i:05d}-' + 'x' * 194 for i in range(10000)]
    seed(keys[0])
    capture = ask_context.Capture(ask_context.Scope(mode='library', papers=keys), tmp_path / 'evidence.jsonl')
    config.set_ai_provider(provider)
    seen = (fake_claude(monkeypatch, stream({'type':'result', 'result':'Answer'})) if provider == 'claude_code'
            else fake_codex(monkeypatch, stream({'type':'turn.completed'})))
    token = ask_context.active.set(capture)
    try:
        agent.answer([{'role':'user', 'content':'What does the library say?'}], lambda: 'http://127.0.0.1:8765')
    finally:
        ask_context.active.reset(token)
    assert sum(len(arg.encode()) + 1 for arg in seen['args']) < 16000
    snapshot = tmp_path / 'papers.json'
    assert json.loads(snapshot.read_text()) == keys
    assert keys[-1] not in seen['prompt']
    # The actual CLI loader reads every key and installs the same read boundary.
    servers = []
    monkeypatch.setattr(mcp_server.MCPServer, 'run', lambda self, **kw: servers.append(self))
    mcp_server.run(workspace='default', papers_file=str(snapshot), library_snapshot=True)
    assert servers
    with pytest.raises(ToolError, match='outside'):
        call(servers[0], 'get_paper', key='excluded')


def test_large_focus_uses_stdin_and_combined_input_budget(monkeypatch, tmp_path):
    from test_agent import fake_claude, stream
    config.set_ai_provider('claude_code')
    text = '研究' * 10000
    capture = ask_context.Capture(ask_context.Scope(mode='selected', papers=['a'],
        focus=[ask_context.Focus(paper='a', claim=f'claim-{i}', text=text) for i in range(30)]), tmp_path / 'evidence.jsonl')
    capture.direct = [ask_context.entry(kind='claim', paper='a', claim=f'claim-{i}', text=text) for i in range(30)]
    seen = fake_claude(monkeypatch, stream({'type':'result', 'result':'Answer'}))
    token = ask_context.active.set(capture)
    try:
        agent.answer([{'role':'user', 'content':'Compare these claims'}], lambda: 'http://127.0.0.1:8765')
        assert sum(len(arg.encode()) + 1 for arg in seen['args']) < 16000
        assert seen['prompt'].count(text) == 30  # Focus identifiers do not duplicate evidence text.
        assert len(seen['prompt'].encode()) > 1_000_000
        from doxograph.model_errors import InputTooLargeError
        from doxograph import claude_code
        monkeypatch.setattr(claude_code, '_run', lambda *a, **kw: pytest.fail('oversized input reached CLI'))
        capture.direct *= 6
        with pytest.raises(InputTooLargeError, match='fewer sources'):
            agent.answer([{'role':'user', 'content':'Compare'}], lambda: 'http://127.0.0.1:8765')
    finally:
        ask_context.active.reset(token)


@pytest.mark.parametrize('payload', ['{}', '[1]', '[]', '["../outside"]', 'x' * 2_100_001])
def test_invalid_snapshot_file_never_starts_an_unscoped_server(payload, monkeypatch, tmp_path):
    path = tmp_path / 'papers.json'
    path.write_text(payload)
    monkeypatch.setattr(mcp_server.MCPServer, 'run', lambda *a, **kw: pytest.fail('invalid scope started server'))
    with pytest.raises(ValueError):
        mcp_server.run(workspace='default', papers_file=str(path))
