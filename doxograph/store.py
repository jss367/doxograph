"""JSON-on-disk store.

One file per paper under `papers/`, holding the paper's metadata and its
claims. Keeping claims inside the paper file means provenance stays local and
every change shows up as a readable diff. The cross-paper views (by topic, by
ledger claim) are computed at read time rather than stored.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import config

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does", "for",
    "from", "how", "in", "is", "it", "its", "of", "on", "or", "que", "that",
    "the", "their", "there", "this", "to", "via", "we", "what", "when", "why",
    "with", "you", "your",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str, keep: str = "-") -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", keep, text).strip(keep).lower()
    return text


def _surname(author: str) -> str:
    author = author.strip()
    if "," in author:
        author = author.split(",", 1)[0]
    parts = [p for p in author.split() if p]
    return parts[-1] if parts else "anon"


def citekey(title: str, authors: list[str], year: int | str | None) -> str:
    """Build a BibTeX-style key: surname + year + first significant title word."""
    surname = slugify(_surname(authors[0]), keep="") if authors else "anon"
    words = [w for w in slugify(title or "").split("-") if w and w not in STOPWORDS]
    word = words[0] if words else "untitled"
    return f"{surname or 'anon'}{year or 'nd'}{word}"


def unique_key(base: str) -> str:
    if not paper_path(base).exists():
        return base
    for suffix in "abcdefghijklmnopqrstuvwxyz":
        candidate = f"{base}{suffix}"
        if not paper_path(candidate).exists():
            return candidate
    raise RuntimeError(f"cannot find an unused key for {base!r}")


def paper_path(key: str) -> Path:
    return config.papers_dir() / f"{key}.json"


def pdf_path(key: str) -> Path:
    return config.pdfs_dir() / f"{key}.pdf"


def write_json(path: Path, payload: Any) -> None:
    """Write atomically so an interrupted save cannot truncate a paper file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_paper(key: str) -> dict:
    path = paper_path(key)
    if not path.exists():
        raise KeyError(key)
    return json.loads(path.read_text(encoding="utf-8"))


def save_paper(paper: dict) -> None:
    paper["updated"] = now()
    write_json(paper_path(paper["key"]), paper)


def paper_keys() -> list[str]:
    return sorted(p.stem for p in config.papers_dir().glob("*.json"))


def all_papers() -> list[dict]:
    papers = []
    for key in paper_keys():
        try:
            papers.append(load_paper(key))
        except (OSError, json.JSONDecodeError):
            continue
    papers.sort(key=lambda p: (p.get("added") or ""), reverse=True)
    return papers


def delete_paper(key: str) -> None:
    paper_path(key).unlink(missing_ok=True)
    pdf_path(key).unlink(missing_ok=True)


def new_paper(key: str, **fields) -> dict:
    paper = {
        "key": key,
        "added": now(),
        "updated": now(),
        "title": "",
        "authors": [],
        "year": None,
        "venue": "",
        "doi": "",
        "abstract": "",
        "source": {},
        "summary": "",
        "relevance": "",
        "status": "fetched",
        "extraction": None,
        "proposed_tags": [],
        "claims": [],
        "notes": "",
    }
    paper.update(fields)
    return paper


def next_claim_id(paper: dict) -> str:
    used = {c.get("id", "") for c in paper.get("claims", [])}
    n = len(used) + 1
    while f"{paper['key']}-c{n}" in used:
        n += 1
    return f"{paper['key']}-c{n}"


def new_claim(paper: dict, **fields) -> dict:
    claim = {
        "id": next_claim_id(paper),
        "text": "",
        "kind": "finding",
        "strength": "supporting",
        "tags": [],
        "evidence": "",
        "quote": "",
        "locator": "",
        "ledger_links": [],
        "reviewed": False,
        "added": now(),
    }
    claim.update(fields)
    return claim


CLAIM_FIELDS = {
    "text", "kind", "strength", "tags", "evidence", "quote", "locator",
    "ledger_links", "reviewed",
}


def update_claim(key: str, claim_id: str, patch: dict) -> dict:
    paper = load_paper(key)
    for claim in paper.get("claims", []):
        if claim.get("id") == claim_id:
            for field, value in patch.items():
                if field in CLAIM_FIELDS:
                    claim[field] = value
            claim["updated"] = now()
            refresh_status(paper)
            save_paper(paper)
            return claim
    raise KeyError(claim_id)


def add_claim(key: str, patch: dict) -> dict:
    paper = load_paper(key)
    claim = new_claim(paper, **{k: v for k, v in patch.items() if k in CLAIM_FIELDS})
    # A hand-written claim needs no review, but a blank draft is not a claim yet.
    claim["reviewed"] = bool(claim["text"].strip())
    paper.setdefault("claims", []).append(claim)
    refresh_status(paper)
    save_paper(paper)
    return claim


def delete_claim(key: str, claim_id: str) -> None:
    paper = load_paper(key)
    before = len(paper.get("claims", []))
    paper["claims"] = [c for c in paper.get("claims", []) if c.get("id") != claim_id]
    if len(paper["claims"]) == before:
        raise KeyError(claim_id)
    refresh_status(paper)
    save_paper(paper)


def refresh_status(paper: dict) -> None:
    claims = paper.get("claims", [])
    if not claims:
        paper["status"] = "extracted" if paper.get("extraction") else "fetched"
    elif all(c.get("reviewed") for c in claims):
        paper["status"] = "reviewed"
    else:
        paper["status"] = "extracted"


# --- controlled vocabulary -------------------------------------------------

DEFAULT_TAGS: list[dict] = []


def _read_yaml(path: Path) -> dict:
    """Read a hand-edited YAML file, reporting where it is malformed."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc


def load_tags() -> list[dict]:
    path = config.tags_path()
    if not path.exists():
        return list(DEFAULT_TAGS)
    tags = _read_yaml(path).get("tags") or []
    return [t for t in tags if isinstance(t, dict) and t.get("name")]


def save_tags(tags: list[dict]) -> None:
    path = config.tags_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(tags, key=lambda t: t["name"])
    path.write_text(
        yaml.safe_dump({"tags": ordered}, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


def tag_names() -> list[str]:
    return [t["name"] for t in load_tags()]


def add_tag(name: str, description: str = "") -> list[dict]:
    name = slugify(name)
    tags = load_tags()
    if name and name not in {t["name"] for t in tags}:
        tags.append({"name": name, "description": description})
        save_tags(tags)
    return tags


def rename_tag(old: str, new: str) -> None:
    """Rename across the vocabulary and every claim. Merges if `new` exists."""
    new = slugify(new)
    tags = [t for t in load_tags() if t["name"] != old]
    if new not in {t["name"] for t in tags}:
        tags.append({"name": new, "description": next((t.get("description", "") for t in load_tags() if t["name"] == old), "")})
    save_tags(tags)
    for paper in all_papers():
        touched = False
        for claim in paper.get("claims", []):
            if old in claim.get("tags", []):
                claim["tags"] = sorted({new if t == old else t for t in claim["tags"]})
                touched = True
        if touched:
            save_paper(paper)


def delete_tag(name: str) -> None:
    save_tags([t for t in load_tags() if t["name"] != name])
    for paper in all_papers():
        touched = False
        for claim in paper.get("claims", []):
            if name in claim.get("tags", []):
                claim["tags"] = [t for t in claim["tags"] if t != name]
                touched = True
        if touched:
            save_paper(paper)


# --- your own claims ------------------------------------------------------

def load_ledger() -> list[dict]:
    path = config.ledger_path()
    if not path.exists():
        return []
    claims = _read_yaml(path).get("claims") or []
    return [c for c in claims if isinstance(c, dict) and c.get("id")]


def save_ledger(claims: list[dict]) -> None:
    path = config.ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"claims": claims}, sort_keys=False, allow_unicode=True, width=100),
        encoding="utf-8",
    )


# --- cross-paper views ----------------------------------------------------

def claim_rows(papers: list[dict] | None = None) -> list[dict]:
    """Flatten every claim with the paper fields needed to display it."""
    rows = []
    for paper in papers if papers is not None else all_papers():
        for claim in paper.get("claims", []):
            row = dict(claim)
            row["paper"] = paper["key"]
            row["paper_title"] = paper.get("title", "")
            row["paper_authors"] = paper.get("authors", [])
            row["paper_year"] = paper.get("year")
            rows.append(row)
    return rows


def tag_counts(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for tag in row.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def summarize(paper: dict) -> dict:
    claims = paper.get("claims", [])
    return {
        "key": paper["key"],
        "title": paper.get("title", ""),
        "authors": paper.get("authors", []),
        "year": paper.get("year"),
        "venue": paper.get("venue", ""),
        "status": paper.get("status", "fetched"),
        "summary": paper.get("summary", ""),
        "relevance": paper.get("relevance", ""),
        "source": paper.get("source", {}),
        "added": paper.get("added"),
        "n_claims": len(claims),
        "n_unreviewed": sum(1 for c in claims if not c.get("reviewed")),
        "n_proposed_tags": len(paper.get("proposed_tags", [])),
        "schema_version": (paper.get("extraction") or {}).get("schema_version"),
        "has_pdf": pdf_path(paper["key"]).exists(),
    }
