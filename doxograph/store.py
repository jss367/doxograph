"""JSON-on-disk store.

One file per paper under `papers/`, holding the paper's metadata and its
claims. Keeping claims inside the paper file means provenance stays local and
every change shows up as a readable diff. The cross-paper views (by topic, by
ledger claim) are computed at read time rather than stored.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import re
import tempfile
import threading
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


# Two layers of locking. The thread lock serializes the request handlers and the
# upload pool inside one process; the file lock extends that to other processes,
# because `doxograph serve` and a `doxograph extract` run from a shell are two
# processes writing the same corpus and a thread lock says nothing about that.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_depth = threading.local()

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None


@contextlib.contextmanager
def _file_lock(path: Path):
    """Hold an exclusive lock on `path` for the duration of the block."""
    if fcntl is None:  # pragma: no cover - single-process fallback
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextlib.contextmanager
def _reentrant_file_lock(name: str, path: Path):
    """Take the file lock once per thread, however deeply the block nests.

    `flock` is held per open file description, so a nested acquire from the same
    thread would open a second descriptor and block on itself.
    """
    held = getattr(_depth, "held", None)
    if held is None:
        held = _depth.held = {}
    if held.get(name):
        held[name] += 1
        try:
            yield
        finally:
            held[name] -= 1
        return
    held[name] = 1
    try:
        with _file_lock(path):
            yield
    finally:
        held[name] -= 1


# The tag vocabulary is a single shared file, so it gets one lock rather than
# one per paper. Lock order is vocabulary before paper, everywhere, so a tag
# rewrite (vocabulary then each paper) and an accept (vocabulary then that
# paper) cannot deadlock against each other.
_vocab = threading.RLock()


@contextlib.contextmanager
def paper_lock(key: str):
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock, _reentrant_file_lock(f"paper:{key}", config.locks_dir() / f"{key}.lock"):
        yield


@contextlib.contextmanager
def vocab_lock():
    with _vocab, _reentrant_file_lock("vocab", config.locks_dir() / "vocabulary.lock"):
        yield


def _locked(func):
    """Run a paper mutator while holding that paper's lock (first arg is the key)."""
    @functools.wraps(func)
    def wrapper(key: str, *args, **kwargs):
        with paper_lock(key):
            return func(key, *args, **kwargs)
    return wrapper


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


def reserve_key(base: str, **fields) -> str:
    """Claim an unused key by creating its file atomically, identity included.

    Checking `exists()` and writing later is not enough: several papers are
    ingested concurrently, two of them can produce the same coarse key, and both
    would see it unused. `O_EXCL` makes the claim itself the check, so the loser
    moves on to the next candidate.

    `fields` are written into the placeholder, so the reserved paper already
    carries its arXiv ID or DOI. A reservation with an empty source would be
    invisible to `find_existing`, and a second request for the same paper would
    reserve a second key while the first was still fetching its PDF.
    """
    config.papers_dir().mkdir(parents=True, exist_ok=True)
    # O_EXCL is already atomic across processes, so this needs no extra lock.
    for candidate in [base] + [f"{base}{s}" for s in "abcdefghijklmnopqrstuvwxyz"]:
        try:
            handle = os.open(paper_path(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(new_paper(candidate, **fields), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        return candidate
    raise RuntimeError(f"cannot find an unused key for {base!r}")


def paper_path(key: str) -> Path:
    return config.papers_dir() / f"{key}.json"


def pdf_path(key: str) -> Path:
    return config.pdfs_dir() / f"{key}.pdf"


def write_json(path: Path, payload: Any) -> None:
    """Write atomically so an interrupted or concurrent save cannot truncate a file.

    The temporary name is unique per write; a fixed `.tmp` sibling would be
    shared by two concurrent writers of the same paper.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n")
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


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


@_locked
def delete_paper(key: str) -> None:
    """Remove a paper under its lock, so an in-flight save cannot resurrect it."""
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


@_locked
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


@_locked
def add_claim(key: str, patch: dict) -> dict:
    paper = load_paper(key)
    claim = new_claim(paper, **{k: v for k, v in patch.items() if k in CLAIM_FIELDS})
    # A hand-written claim needs no review, but a blank one is not a claim yet.
    # An explicit `reviewed` in the patch still wins.
    if "reviewed" not in patch:
        claim["reviewed"] = bool(claim["text"].strip())
    paper.setdefault("claims", []).append(claim)
    refresh_status(paper)
    save_paper(paper)
    return claim


@_locked
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
    """Caller must hold `vocab_lock`; every public mutator below does."""
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
    with vocab_lock():
        tags = load_tags()
        if name and name not in {t["name"] for t in tags}:
            tags.append({"name": name, "description": description})
            save_tags(tags)
        return tags


def rename_tag(old: str, new: str) -> None:
    """Rename across the vocabulary and every claim. Merges if `new` exists."""
    new = slugify(new)
    with vocab_lock():
        current = load_tags()
        tags = [t for t in current if t["name"] != old]
        if new not in {t["name"] for t in tags}:
            description = next((t.get("description", "") for t in current if t["name"] == old), "")
            tags.append({"name": new, "description": description})
        save_tags(tags)
        _retag_all(old, new)


def _retag_all(old: str, new: str | None) -> None:
    """Rewrite or drop a tag across every paper. Called holding `vocab_lock`."""
    for key in paper_keys():
        with paper_lock(key):
            try:
                paper = load_paper(key)
            except (KeyError, json.JSONDecodeError):
                continue
            touched = False
            for claim in paper.get("claims", []):
                if old not in claim.get("tags", []):
                    continue
                kept = {t for t in claim["tags"] if t != old}
                if new:
                    kept.add(new)
                claim["tags"] = sorted(kept)
                touched = True
            if touched:
                save_paper(paper)


def delete_tag(name: str) -> None:
    with vocab_lock():
        save_tags([t for t in load_tags() if t["name"] != name])
        _retag_all(name, None)


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


def needs_extraction(key: str) -> bool:
    """A paper with its PDF but no claims is ready to be read.

    Callers used to gate on "was this paper just created", which skipped a paper
    whose PDF arrived later — after a failed download was retried, for instance.
    """
    try:
        paper = load_paper(key)
    except (KeyError, json.JSONDecodeError):
        return False
    return pdf_path(key).exists() and not paper.get("claims")


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
        "updated": paper.get("updated"),
        "n_claims": len(claims),
        "n_unreviewed": sum(1 for c in claims if not c.get("reviewed")),
        "n_proposed_tags": len(paper.get("proposed_tags", [])),
        "schema_version": (paper.get("extraction") or {}).get("schema_version"),
        "has_pdf": pdf_path(paper["key"]).exists(),
    }
