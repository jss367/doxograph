"""Read-only MCP tools over a fixed snapshot of the library.

The agent names papers, claims, and topics by ID, never by file path. The only
disk read is the manifest Doxograph names when it starts this process. Calls
work on memory, and the manifest holds no personal notes or ledger claims.
This module uses only the standard library, so it runs as a bare script.
"""

import json
import sys
from pathlib import Path

SERVER_NAME = "doxograph"
TOOL_NAMES = ("list_papers", "get_paper", "search_claims", "list_topics", "get_topic")
LIST_SIZE = 40
MATCH_LIMIT = 30
EXCERPT = 400

DEFINITIONS = [
    ("list_papers", "List papers with title, year, labels, and claim count. Continue with next_start until null.",
     {"start": {"type": "integer", "minimum": 0}}),
    ("get_paper", "Read one paper: its summary, why it is in the library, and every claim with its quote.",
     {"key": {"type": "string"}}),
    ("search_claims", "Find claims whose text, quote, or topics contain every word of the query. Case-insensitive.",
     {"query": {"type": "string", "minLength": 1, "maxLength": 200}}),
    ("list_topics", "List topics with how many claims and papers each has, and whether a synthesis exists.", {}),
    ("get_topic", "Read one topic: its synthesis (synthesis_stale means claims changed after it was written), its claims, and the agreements and tensions found in it. Groups with stale true were judged before a claim changed.",
     {"name": {"type": "string"}}),
]
OPTIONAL = {"start"}


def _claim(claim: dict, paper: dict | None = None) -> dict:
    out = {k: claim.get(k) for k in ("id", "text", "kind", "strength", "tags", "evidence", "quote",
                                     "locator", "reviewed", "quote_verified")}
    if paper is not None:
        out["paper"] = paper["key"]
        out["paper_title"] = paper.get("title", "")
    return out


class Library:
    def __init__(self, snapshot: dict):
        self.papers = {p["key"]: p for p in snapshot.get("papers", [])}
        self.claims = {c["id"]: (c, p) for p in self.papers.values() for c in p.get("claims", [])}
        self.topics = {t["name"]: t for t in snapshot.get("topics", [])}
        self.syntheses = {t: s if isinstance(s, dict) else {"text": s, "stale": False}
                          for t, s in snapshot.get("syntheses", {}).items()}
        self.agreements = snapshot.get("agreements", [])
        self.tensions = snapshot.get("tensions", [])

    def _topic_claims(self, name: str) -> list[dict]:
        return [_claim(c, p) for c, p in self.claims.values() if name in (c.get("tags") or [])]

    def _group(self, record: dict) -> dict:
        ids = [i for i in record.get("claims", []) if i in self.claims]
        return {"id": record.get("id"), "kind": record.get("kind"), "note": record.get("note", ""),
                "status": record.get("status"), "claims": ids,
                "topics": record.get("topics", []), "stale": bool(record.get("stale")),
                "papers": sorted({self.claims[i][1]["key"] for i in ids})}

    def call(self, name: str, arguments: dict) -> dict:
        if name == "list_papers":
            start = arguments.get("start", 0)
            if type(start) is not int or start < 0:
                raise ValueError("start must be a nonnegative list offset.")
            papers = list(self.papers.values())
            end = start + LIST_SIZE
            return {
                "papers": [{"key": p["key"], "title": p.get("title", ""), "authors": p.get("authors", []),
                            "year": p.get("year"), "labels": p.get("labels", []),
                            "claims": len(p.get("claims", []))} for p in papers[start:end]],
                "total": len(papers),
                "next_start": end if end < len(papers) else None,
            }
        if name == "get_paper":
            paper = self.papers.get(arguments.get("key"))
            if paper is None:
                raise ValueError("Choose a paper key from list_papers.")
            return {**{k: paper.get(k) for k in ("key", "title", "authors", "year", "venue", "labels",
                                                  "summary", "relevance")},
                    "claims": [_claim(c) for c in paper.get("claims", [])]}
        if name == "search_claims":
            query = arguments.get("query")
            if not isinstance(query, str) or not query.strip() or len(query) > 200:
                raise ValueError("query must contain 1 to 200 characters.")
            words = query.casefold().split()
            matches = []
            for claim, paper in self.claims.values():
                haystack = " ".join([claim.get("text", ""), claim.get("quote", ""),
                                     *(claim.get("tags") or [])]).casefold()
                if all(w in haystack for w in words):
                    row = _claim(claim, paper)
                    row["quote"] = (row["quote"] or "")[:EXCERPT]
                    matches.append(row)
                    if len(matches) == MATCH_LIMIT:
                        return {"matches": matches, "more_possible": True}
            return {"matches": matches, "more_possible": False}
        if name == "list_topics":
            rows = []
            for topic, meta in sorted(self.topics.items()):
                claims = self._topic_claims(topic)
                rows.append({"name": topic, "description": meta.get("description", ""),
                             "claims": len(claims), "papers": len({c["paper"] for c in claims}),
                             "has_synthesis": topic in self.syntheses})
            return {"topics": rows}
        if name == "get_topic":
            topic = arguments.get("name")
            if topic not in self.topics:
                raise ValueError("Choose a topic name from list_topics.")
            synthesis = self.syntheses.get(topic, {})
            return {
                "name": topic,
                "description": self.topics[topic].get("description", ""),
                "synthesis": synthesis.get("text", ""),
                "synthesis_stale": bool(synthesis.get("stale")),
                "claims": self._topic_claims(topic),
                "agreements": [self._group(a) for a in self.agreements if topic in a.get("topics", [])],
                "tensions": [self._group(t) for t in self.tensions if topic in t.get("topics", [])],
            }
        raise ValueError("Unknown library tool.")

    def respond(self, message: dict) -> dict | None:
        identifier = message.get("id")
        if identifier is None:
            return None
        method = message.get("method")
        params = message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": SERVER_NAME, "version": "1"}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{
                "name": name, "description": description,
                "inputSchema": {"type": "object", "properties": properties,
                                "required": [k for k in properties if k not in OPTIONAL],
                                "additionalProperties": False},
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
            } for name, description, properties in DEFINITIONS]}
        elif method == "tools/call":
            try:
                data = self.call(params.get("name"), params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
            except (ValueError, TypeError, AttributeError) as error:
                result = {"isError": True, "content": [{"type": "text", "text": str(error)}]}
        else:
            return {"jsonrpc": "2.0", "id": identifier, "error": {"code": -32601, "message": "Method not found."}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}


def main(manifest: str) -> None:
    library = Library(json.loads(Path(manifest).read_text(encoding="utf-8")))
    for line in sys.stdin:
        try:
            message = json.loads(line)
            reply = library.respond(message) if isinstance(message, dict) else None
        except (ValueError, TypeError, AttributeError):
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid request."}}
        if reply is not None:
            print(json.dumps(reply, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main(sys.argv[1])
