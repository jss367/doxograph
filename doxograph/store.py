"""JSON-on-disk store.

One file per paper under `papers/`, holding the paper's metadata and its
claims. Keeping claims inside the paper file means provenance stays local and
every change shows up as a readable diff. The cross-paper views (by topic, by
ledger claim) are computed at read time rather than stored.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import json
import os
import re
import string
import tempfile
import threading
import time
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
_extraction_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_depth = threading.local()

try:
    import fcntl
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


@contextlib.contextmanager
def _file_lock(path: Path):
    """Hold an exclusive lock on `path` for the duration of the block.

    Silently doing nothing when no locking primitive exists would be worse than
    not locking at all: the corpus would look protected while two processes
    overwrote each other. If neither primitive is available this refuses to run.

    The Windows branch uses `msvcrt.locking`, which is untested here — this was
    developed and exercised on macOS.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        handle = open(path, "a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        return

    if msvcrt is not None:  # pragma: no cover - Windows only
        handle = open(path, "a+")
        try:
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)   # LK_LOCK retries ten times then raises
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()
        return

    raise RuntimeError(  # pragma: no cover - neither primitive exists
        "no file-locking primitive available (need fcntl or msvcrt); "
        "doxograph cannot protect the corpus against concurrent writers here"
    )


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
_claim = threading.RLock()


@contextlib.contextmanager
def paper_lock(key: str):
    with _locks_guard:
        lock = _locks.setdefault(key, threading.RLock())
    with lock, _reentrant_file_lock(f"paper:{key}", config.locks_dir() / f"{key}.lock"):
        yield


@contextlib.contextmanager
def extraction_lock(key: str):
    """Serialize model-backed extractions for one paper.

    This is deliberately separate from ``paper_lock``. An extraction holds it
    across the slow model call so another re-read cannot take a stale snapshot,
    while ordinary claim edits remain free to land and are reconciled by the
    merge under ``paper_lock``.
    """
    with _locks_guard:
        lock = _extraction_locks.setdefault(key, threading.RLock())
    path = config.locks_dir() / f"{key}.extraction.lock"
    with lock, _reentrant_file_lock(f"extraction:{key}", path):
        yield


@contextlib.contextmanager
def claim_lock():
    """Guard "is this paper already here, and if not what key does it get".

    Lock order is claim before paper: ingest holds this while reserving and then
    publishes a PDF under the paper lock.
    """
    with _claim, _reentrant_file_lock("claim", config.locks_dir() / "claim.lock"):
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


def cite_surname(authors: list[str] | None, fallback: str = "") -> str:
    """The surname a citation marker shows, or `fallback` when there is none.

    An author can be a blank string. Crossref records an institutional author
    under `name` rather than `given`/`family`, and anything arriving with
    neither reads as "". Guarding on the list being non-empty is not enough:
    `"".split()` is empty, and indexing it raised, which took out the tension
    pass, the synthesis pass, the HTML export and `tensions --list` for the
    whole corpus. Blank entries are skipped rather than shown, so a paper whose
    first author was lost still cites the next one who has a name.
    """
    for author in authors or []:
        parts = author.split()
        if parts:
            return parts[-1]
    return fallback


def citekey(title: str, authors: list[str], year: int | str | None) -> str:
    """Build a BibTeX-style key: surname + year + first significant title word."""
    surname = slugify(_surname(authors[0]), keep="") if authors else "anon"
    words = [w for w in slugify(title or "").split("-") if w and w not in STOPWORDS]
    word = words[0] if words else "untitled"
    return f"{surname or 'anon'}{year or 'nd'}{word}"


# Enough candidates that exhausting them means something is wrong, rather than
# a corpus that legitimately reused one coarse key too many times.
MAX_KEY_CANDIDATES = 10_000


def key_candidates(base: str):
    """`base`, then base+a … base+z, then base+aa, base+ab, and onward.

    A fixed a–z ceiling became a permanent wall once keys were retired: enough
    delete-and-re-add cycles on one coarse key, or a few metadata-free uploads
    all called paper.pdf, and every later ingest for that base would fail.
    """
    yield base
    produced = 1
    for width in itertools.count(1):
        for combo in itertools.product(string.ascii_lowercase, repeat=width):
            yield base + "".join(combo)
            produced += 1
            if produced >= MAX_KEY_CANDIDATES:
                return


def retired_keys_path() -> Path:
    return config.data_dir() / "retired-keys.json"


def retired_keys() -> set[str]:
    """Citekeys that have belonged to a paper and must never be issued again.

    Reusing a key means a key no longer identifies one paper for all time, and
    everything that carries a key across a slow operation — an extraction job, a
    PDF download, a queued PATCH, a retag reply — then has to prove which
    incarnation it meant. Retiring the key removes that whole class instead of
    guarding each carrier.
    """
    path = retired_keys_path()
    if not path.exists():
        return set()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return set(loaded) if isinstance(loaded, list) else set()


def retire_key(key: str) -> None:
    """Record a key as spent. Uses its own lock, which nothing else nests inside.

    Deliberately not the vocabulary lock: `delete_paper` calls this while holding
    the paper lock, and a tag rename holds the vocabulary lock while taking paper
    locks, so sharing that lock would invert the order and deadlock.
    """
    with _reentrant_file_lock("retired", config.locks_dir() / "retired-keys.lock"):
        known = retired_keys()
        if key not in known:
            known.add(key)
            write_atomic(retired_keys_path(), json.dumps(sorted(known), indent=2) + "\n")


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
    # The retirement lock spans the check and the create. Reading the retired set
    # and then creating separately leaves a gap in which a concurrent
    # `delete_paper` can retire and unlink a key, after which this caller still
    # believes it is free and recreates it.
    with _reentrant_file_lock("retired", config.locks_dir() / "retired-keys.lock"):
        spent = retired_keys()
        for candidate in key_candidates(base):
            if candidate in spent:
                continue
            try:
                handle = os.open(paper_path(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                continue
            paper = new_paper(candidate, **fields)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump(paper, fh, indent=2, ensure_ascii=False)
                fh.write("\n")
            return candidate
    raise RuntimeError(
        f"cannot find an unused key for {base!r} after {MAX_KEY_CANDIDATES} candidates"
    )


def paper_path(key: str) -> Path:
    return config.papers_dir() / f"{key}.json"


def pdf_path(key: str) -> Path:
    return config.pdfs_dir() / f"{key}.pdf"


def write_atomic(path: Path, text: str) -> None:
    """Replace a file's contents in one step.

    `write_text` truncates before writing, and the readers of `tags.yaml` —
    `/api/state`, the extraction prompt, retag — do not take the vocabulary
    lock, so a truncated read is reachable. A retag prompted with an empty
    vocabulary would return empty assignments and clear real tags.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(staged, path)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def write_json(path: Path, payload: Any) -> None:
    """Write atomically so an interrupted or concurrent save cannot truncate a file.

    The temporary name is unique per write; a fixed `.tmp` sibling would be
    shared by two concurrent writers of the same paper.
    """
    write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


# What a scan over `paper_keys()` tolerates. The keys are a snapshot of the
# directory, so a paper can be deleted between the listing and the read: the
# file is gone (`KeyError`), half-written or unreadable (`OSError`), or replaced
# under us (`JSONDecodeError`). None of that should fail a listing or an export.
VANISHED = (KeyError, OSError, json.JSONDecodeError)


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
        except VANISHED:
            continue
    papers.sort(key=lambda p: (p.get("added") or ""), reverse=True)
    return papers


@_locked
def delete_paper(key: str) -> None:
    """Remove a paper under its lock, so an in-flight save cannot resurrect it."""
    # Retire before unlinking. The other order leaves a window where the key is
    # free but not yet recorded, and a concurrent ingest could take it.
    retire_key(key)
    paper_path(key).unlink(missing_ok=True)
    pdf_path(key).unlink(missing_ok=True)


def new_paper(key: str, **fields) -> dict:
    paper = {
        "key": key,
        "claim_seq": 0,
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


def ensure_claim_seq(paper: dict) -> int:
    """Set and return the paper's claim high-water mark.

    Corpora written before `claim_seq` existed derive it from the ids they
    already hold. Call this on the *whole* paper before filtering its claims:
    deriving it from a subset lets an id belonging to a filtered-out claim be
    issued again, and ids travel in flight.
    """
    seq = paper.get("claim_seq")
    if seq is None:
        used = []
        for claim in paper.get("claims", []):
            match = re.search(r"-c(\d+)$", claim.get("id", ""))
            if match:
                used.append(int(match.group(1)))
        seq = max(used, default=0)
        paper["claim_seq"] = seq
    return seq


def next_claim_id(paper: dict) -> str:
    """Allocate a claim id that has never been used on this paper.

    Counting the current claims recycles the id of a deleted one, and ids travel
    in flight: a retag response or a PATCH issued before the deletion is matched
    by id alone and would land on the unrelated replacement.
    """
    seq = ensure_claim_seq(paper) + 1
    paper["claim_seq"] = seq
    return f"{paper['key']}-c{seq}"


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


def clean_ledger_links(links: Any) -> list[dict]:
    """Keep only links that name a claim currently in the ledger.

    A model can invent an ID that looks right, and `ledger.yaml` can change
    during a long extraction. Either way the corpus would record a relationship
    to a claim that does not exist and the UI would show the bare ID.
    """
    known = {c["id"] for c in load_ledger()}
    cleaned = []
    for link in links or []:
        if not isinstance(link, dict):
            continue
        claim = (link.get("claim") or "").strip()
        if claim not in known:
            continue
        cleaned.append({
            "claim": claim,
            "relation": link.get("relation") or config.LEDGER_RELATIONS[-1],
            "note": (link.get("note") or "").strip(),
        })
    return cleaned


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
                    claim[field] = clean_ledger_links(value) if field == "ledger_links" else value
            claim["updated"] = now()
            refresh_status(paper)
            save_paper(paper)
            return claim
    raise KeyError(claim_id)


@_locked
def add_claim(key: str, patch: dict) -> dict:
    paper = load_paper(key)
    fields = {k: v for k, v in patch.items() if k in CLAIM_FIELDS}
    if "ledger_links" in fields:
        fields["ledger_links"] = clean_ledger_links(fields["ledger_links"])
    claim = new_claim(paper, **fields)
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
    ordered = sorted(tags, key=lambda t: t["name"])
    write_atomic(
        config.tags_path(),
        yaml.safe_dump({"tags": ordered}, sort_keys=False, allow_unicode=True, width=100),
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
    """Rewrite or drop a tag across every paper, every tension's topics, and
    the syntheses. Called holding `vocab_lock`; takes `tensions_lock` and then
    `syntheses_lock` inside it, one after the other, which is the only order
    those are ever held in.

    Papers first, tensions last, and `record_tensions` waits on `vocab_lock`
    for the whole of it: between the two writes the claims carry the new name
    while the tensions still carry the old, and a merge landing there would
    read the old name as one the claims dropped and take it off every tension
    before this loop could convert it. A result that lands after sees the new
    tags and attaches only what the claims still carry."""
    for key in paper_keys():
        with paper_lock(key):
            try:
                paper = load_paper(key)
            except VANISHED:
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
    with tensions_lock():
        data = _read_tensions()
        touched = False
        for tension in data["tensions"]:
            if old not in tension.get("topics", []):
                continue
            kept = {t for t in tension["topics"] if t != old}
            if new:
                kept.add(new)
            tension["topics"] = sorted(kept)   # a tension with no topics left is still a tension
            touched = True
        if touched:
            _save_tensions(data)
    with syntheses_lock():
        data = _read_syntheses()
        record = data["syntheses"].pop(old, None)
        if record is not None:
            # A synthesis is about the topic under whichever name; it moves
            # with a rename and goes with a deletion. Merging into a name that
            # already has one would put two answers to one question on file, so
            # the existing record wins there.
            if new and new not in data["syntheses"]:
                record["topic"] = new
                data["syntheses"][new] = record
            _save_syntheses(data)


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
    write_atomic(
        config.ledger_path(),
        yaml.safe_dump({"claims": claims}, sort_keys=False, allow_unicode=True, width=100),
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
    except VANISHED:
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


# --- tensions between papers ---------------------------------------------
#
# A tension is a pair of claims from two different papers that pull against
# each other on the same question. They are found by a model pass over each
# topic and then reviewed like claims: open until somebody confirms or
# dismisses them. They live in one file rather than inside the paper files
# because each one belongs to two papers at once.

TENSION_KINDS = ["contradiction", "tension"]
TENSION_STATUSES = ["open", "confirmed", "dismissed"]

_tensions = threading.RLock()


def tensions_path() -> Path:
    return config.data_dir() / "tensions.json"


@contextlib.contextmanager
def tensions_lock():
    """Guard `tensions.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a tension merge
    takes both so it cannot land halfway through a rename. The pass itself
    reads papers without their locks and writes only here."""
    with _tensions, _reentrant_file_lock("tensions", config.locks_dir() / "tensions.lock"):
        yield


def _read_tensions() -> dict:
    """Read the ledger, reporting where it is malformed rather than reading it
    as empty. Every writer starts here, so an empty reading would be written
    back over the file: every decision gone and ids restarting at t1. Only a
    missing file is an empty ledger. Writes are atomic, so a half-written file
    is never seen; anything unreadable was edited by hand or damaged."""
    path = tensions_path()
    if not path.exists():
        return {"seq": 0, "tensions": []}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} should hold an object, not {type(loaded).__name__}")
    loaded.setdefault("seq", 0)
    loaded.setdefault("tensions", [])
    return loaded


def load_tensions() -> list[dict]:
    return list(_read_tensions()["tensions"])


def _save_tensions(data: dict) -> None:
    """Caller holds `tensions_lock`."""
    write_json(tensions_path(), data)


def _pair(ids) -> tuple[str, str]:
    a, b = sorted(ids)
    return (a, b)


def _shared_topics(tension: dict, live: dict[str, dict]) -> list[str]:
    """The stored topics both claims still carry. A topic removed from either
    claim in the editor leaves the tension with it: a pass under that topic
    can no longer return the pair, since the edited claim is no longer in the
    prompt, and nothing else would ever take the name off. A tension left
    with no topics is still a tension."""
    tags = [set(live[i].get("tags") or []) for i in tension.get("claims", [])]
    return sorted(t for t in tension.get("topics", []) if all(t in s for s in tags))


def claim_fingerprint(claim: dict) -> str:
    """What a tension's judgment rests on: everything `_tension_listing` shows
    the model about a claim. If the text, evidence or kind changes, the
    judgment was made about a claim that no longer exists."""
    return json.dumps([claim.get("text", ""), claim.get("evidence", ""), claim.get("kind", "finding")])


def record_tensions(topic: str, found: list[dict], claims_by_id: dict[str, dict]) -> dict:
    """Merge one topic's model output into the file.

    `found` is a list of `{"claims": [id, id], "kind", "note"}`; `claims_by_id`
    is every claim the model was shown, as it stood when the prompt was built.

    Rules, in order:
    - A pair whose claims no longer both exist is dropped, wherever it came from.
    - A pair already on file whose claims are unchanged is left exactly as it
      is: its status is a decision somebody made and the model does not get to
      remake it. A repeat run costs the reviewer nothing.
    - A pair already on file whose claims have changed is refreshed and set
      back to open: the old verdict was about different text.
    - Unless the change landed while the call was in flight. Then the answer
      describes text nobody can see any more, and the record on file is left
      alone, as extraction leaves a claim edited during its call alone: a
      decision the reviewer made meanwhile against the new text is newer than
      this answer and must not be reopened by it. `tension_rows` says stale if
      nobody has re-judged the pair.
    - A new pair is added as open.
    - A pair the model did not return this time is kept. The pass is per topic
      and a claim can carry several topics, so absence from one topic's answer
      says nothing; and a confirmed tension is the reviewer's, not the model's.
    - `topic` is attached only if both claims still carry it now. The tag can
      be renamed or deleted while the model call is in flight; `_retag_all`
      has already rewritten the tensions on file by then, and this late result
      must not put the old name back. The pair itself is still recorded, and
      the next pass under the new name attaches that.
    - Stored topics that either claim no longer carries are dropped from every
      tension on file, whether or not the model returned it this time. A claim
      edit does not touch this file, so `tension_rows` applies the same filter
      at read time; this write is where the file catches up.

    The merge holds `vocab_lock` so that last rule cannot fire halfway through
    a rename, when the claims already carry the new name and the tensions
    still carry the old: it would read the old name as dropped and take it
    off before `_retag_all` could convert it. Vocabulary before tensions, as
    `_retag_all` takes them.

    Returns `{"added": n, "reopened": n, "kept": n}`.
    """
    with vocab_lock(), tensions_lock():
        data = _read_tensions()
        # Prune against the corpus as it is now, not as the prompt saw it: a
        # claim deleted during the call must not come back as half a tension.
        live = {c["id"]: c for c in claim_rows()}
        existing = [t for t in data["tensions"]
                    if len(t.get("claims", [])) == 2 and all(i in live for i in t["claims"])]
        for tension in existing:
            tension["topics"] = _shared_topics(tension, live)
        by_pair = {_pair(t["claims"]): t for t in existing}
        added = reopened = kept = 0
        for item in found:
            ids = [i for i in item.get("claims", []) if i in claims_by_id and i in live]
            if len(set(ids)) != 2:
                continue
            a, b = _pair(ids)
            if live[a].get("paper") == live[b].get("paper"):
                continue    # a paper in tension with itself is not what this is for
            # Fingerprint what the model was shown, not what is on disk now. The
            # judgment is about the prompt text; if a claim was edited during
            # the call, `tension_rows` must be able to see that and say stale.
            fingerprints = {a: claim_fingerprint(claims_by_id[a]), b: claim_fingerprint(claims_by_id[b])}
            note = (item.get("note") or "").strip()
            kind = item.get("kind") if item.get("kind") in TENSION_KINDS else TENSION_KINDS[-1]
            topic_live = all(topic in (live[i].get("tags") or []) for i in (a, b))
            current = by_pair.get((a, b))
            if current is not None:
                if topic_live and topic not in current.setdefault("topics", []):
                    current["topics"].append(topic)
                    current["topics"].sort()
                if current.get("fingerprints") == fingerprints:
                    kept += 1
                    continue
                if any(fingerprints[i] != claim_fingerprint(live[i]) for i in (a, b)):
                    kept += 1   # changed during the call: see the docstring
                    continue
                current.update(kind=kind, note=note, fingerprints=fingerprints,
                               status="open", found=now())
                reopened += 1
                continue
            data["seq"] = int(data.get("seq") or 0) + 1
            record = {
                "id": f"t{data['seq']}",
                "claims": [a, b],
                "topics": [topic] if topic_live else [],
                "kind": kind,
                "note": note,
                "status": "open",
                "found": now(),
                "fingerprints": fingerprints,
            }
            existing.append(record)
            by_pair[(a, b)] = record
            added += 1
        data["tensions"] = existing
        _save_tensions(data)
        return {"added": added, "reopened": reopened, "kept": kept}


def set_tension_status(tension_id: str, status: str) -> dict:
    """Record the reviewer's decision.

    Confirming or dismissing is a judgment about the claims as they read now,
    so it also refreshes the fingerprints: a tension that went stale because a
    claim was edited stops being stale once someone has decided it against the
    current text. Reopening is not a judgment and leaves the fingerprints
    alone, so a reopened tension stays stale until it is re-judged.
    """
    if status not in TENSION_STATUSES:
        raise ValueError(f"status must be one of {TENSION_STATUSES}, not {status!r}")
    with tensions_lock():
        data = _read_tensions()
        for tension in data["tensions"]:
            if tension.get("id") == tension_id:
                tension["status"] = status
                tension["decided"] = now()
                if status != "open":
                    live = {c["id"]: c for c in claim_rows()}
                    ids = tension.get("claims", [])
                    if all(i in live for i in ids):
                        tension["fingerprints"] = {i: claim_fingerprint(live[i]) for i in ids}
                _save_tensions(data)
                return tension
    raise KeyError(tension_id)


def delete_tension(tension_id: str) -> None:
    with tensions_lock():
        data = _read_tensions()
        before = len(data["tensions"])
        data["tensions"] = [t for t in data["tensions"] if t.get("id") != tension_id]
        if len(data["tensions"]) == before:
            raise KeyError(tension_id)
        _save_tensions(data)


def tension_rows(rows: list[dict] | None = None) -> list[dict]:
    """Every tension whose claims still exist, joined to those claims.

    Each row carries `claims` as the two claim rows (not ids), plus `stale`,
    true when either claim has been edited since the tension was found. A stale
    tension is still shown: the reviewer decides whether the edit settled it.
    `topics` is the stored list filtered to what both claims still carry, so a
    topic taken off a claim in the editor disappears here at once even though
    the file is only rewritten by the next `record_tensions`.
    """
    live = {c["id"]: c for c in (rows if rows is not None else claim_rows())}
    out = []
    for tension in load_tensions():
        ids = tension.get("claims", [])
        if len(ids) != 2 or not all(i in live for i in ids):
            continue
        fingerprints = tension.get("fingerprints") or {}
        row = dict(tension)
        row["claims"] = [live[i] for i in ids]
        row["topics"] = _shared_topics(tension, live)
        row["stale"] = any(fingerprints.get(i) != claim_fingerprint(live[i]) for i in ids)
        out.append(row)
    order = {s: n for n, s in enumerate(TENSION_STATUSES)}
    out.sort(key=lambda t: (order.get(t.get("status"), 9), t.get("found") or ""), reverse=False)
    return out


def tension_topics(rows: list[dict] | None = None) -> list[str]:
    """Topics where a tension is possible: claims from at least two papers."""
    papers_by_tag: dict[str, set[str]] = {}
    for row in rows if rows is not None else claim_rows():
        for tag in row.get("tags", []):
            papers_by_tag.setdefault(tag, set()).add(row["paper"])
    return sorted(tag for tag, papers in papers_by_tag.items() if len(papers) >= 2)


# --- what the papers hold, by topic ---------------------------------------
#
# A synthesis is one topic's state of the question, written from its claims.
# They live in one file rather than on the papers, since each one draws on
# every paper in the topic. Written by the model, corrected by hand.

SYNTHESIS_SOURCES = ["model", "hand"]

_syntheses = threading.RLock()


def syntheses_path() -> Path:
    return config.data_dir() / "syntheses.json"


@contextlib.contextmanager
def syntheses_lock():
    """Guard `syntheses.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a write from
    the model takes both so it cannot land halfway through a rename. It is
    never held together with `tensions_lock`."""
    with _syntheses, _reentrant_file_lock("syntheses", config.locks_dir() / "syntheses.lock"):
        yield


def _read_syntheses() -> dict:
    """Read the file, reporting where it is malformed rather than reading it
    as empty: every writer starts here and would write the empty reading back
    over it. Only a missing file is empty."""
    path = syntheses_path()
    if not path.exists():
        return {"syntheses": {}}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("syntheses", {}), dict):
        raise ValueError(f"{path} should hold an object with a 'syntheses' object")
    loaded.setdefault("syntheses", {})
    return loaded


def load_syntheses() -> dict[str, dict]:
    return dict(_read_syntheses()["syntheses"])


def _save_syntheses(data: dict) -> None:
    """Caller holds `syntheses_lock`."""
    write_json(syntheses_path(), data)


def topic_claims(topic: str, rows: list[dict] | None = None) -> list[dict]:
    return [r for r in (rows if rows is not None else claim_rows()) if topic in (r.get("tags") or [])]


def synthesis_basis(rows: list[dict]) -> dict[str, str]:
    """Half of what a synthesis rests on: the set of claims and what each
    said. A claim added, removed, or edited (text, evidence, kind) changes it.
    Reviewing a claim does not: the prompt marks unreviewed claims, but a
    review pass over a corpus does not change what any claim says, and staling
    every synthesis while it runs would leave the mark meaning nothing."""
    return {r["id"]: claim_fingerprint(r) for r in rows}


def synthesis_tensions(topic: str, tensions: list[dict]) -> dict[str, list]:
    """The other half: the topic's tensions as the prompt shows them, by id,
    with kind, status, and whether the prompt said a claim had changed since
    it was judged. Dismissed ones are left out of the prompt, so dismissing
    one changes this as much as confirming one, or a pass finding a new pair,
    does; so does re-judging a stale one against the current text. `tensions`
    is `tension_rows` output, whose `topics` are already filtered to what both
    claims still carry. A record from before the stale flag was kept holds
    two-element lists, which never compare equal to these, so it reads as
    stale until rewritten: what the model was told is not known."""
    return {t["id"]: [t.get("kind"), t.get("status"), bool(t.get("stale"))]
            for t in sorted(tensions, key=lambda t: t["id"])
            if topic in t.get("topics", []) and t.get("status") != "dismissed"}


# `before` for a `record_synthesis` call that has no snapshot to check against:
# a direct write, with no model call in between to be overtaken.
UNCHECKED = object()


def record_synthesis(topic: str, text: str, claims_by_id: dict[str, dict],
                     tensions: list[dict] | None = None, source: str = "model",
                     before: dict | None | object = UNCHECKED) -> dict | None:
    """Write one topic's synthesis.

    `claims_by_id` is every claim the model was shown, as it stood when the
    prompt was built, and `tensions` the `tension_rows` it was shown alongside
    them; the basis is fingerprinted from those, not from disk, so a claim
    edited or a tension confirmed during the call leaves the synthesis stale
    rather than silently current. With no `tensions` given the ones on file
    now are used: a direct write, with no call in between to be overtaken.

    `before` is the record on file for this topic when the prompt was built,
    or None if there was none. The web app leaves edit and delete enabled
    while a synthesis job runs, so a reviewer can correct or delete the text
    while the model is thinking. That decision is newer than this answer and
    must not be written over, as extraction leaves a claim edited during its
    call alone: if the record on file no longer matches `before`, whether it
    was edited, deleted, or created meanwhile, nothing is written. A record
    found exactly as the call left it is not a change, so a repeat run writes
    as usual.

    Holds `vocab_lock` so a rename cannot interleave: `_retag_all` moves the
    record to the new name under that lock, and a result arriving after sees
    that no live claim carries the old name and is dropped rather than
    recreating the topic. Returns the record, or None when it was dropped.
    """
    if source not in SYNTHESIS_SOURCES:
        raise ValueError(f"source must be one of {SYNTHESIS_SOURCES}, not {source!r}")
    # The response schema admits an empty string, so a model that answers
    # with nothing would otherwise be written as a blank record and, on a
    # rewrite, replace a useful synthesis. Refuse it as the hand-edit path
    # does: the caller reports a failure and the saved text stands.
    text = (text or "").strip()
    if not text:
        raise ValueError("the model returned an empty synthesis; nothing written")
    with vocab_lock(), syntheses_lock():
        live = topic_claims(topic)
        if not live:
            return None
        data = _read_syntheses()
        if before is not UNCHECKED and data["syntheses"].get(topic) != before:
            return None
        record = {
            "topic": topic,
            "text": text,
            "source": source,
            "written": now(),
            "claims": {i: claim_fingerprint(c) for i, c in claims_by_id.items()
                       if topic in (c.get("tags") or [])},
            "tensions": synthesis_tensions(topic, tension_rows() if tensions is None else tensions),
        }
        data["syntheses"][topic] = record
        _save_syntheses(data)
        return record


def set_synthesis_text(topic: str, text: str) -> dict:
    """Record a correction by hand. It is a judgment against the claims and
    tensions as they stand now, so the basis is refreshed and the synthesis
    stops being stale."""
    text = (text or "").strip()
    if not text:
        raise ValueError("a synthesis cannot be empty; delete it instead")
    with vocab_lock(), syntheses_lock():
        data = _read_syntheses()
        if topic not in data["syntheses"]:
            raise KeyError(topic)
        record = data["syntheses"][topic]
        record.update(text=text, source="hand", written=now(),
                      claims=synthesis_basis(topic_claims(topic)),
                      tensions=synthesis_tensions(topic, tension_rows()))
        _save_syntheses(data)
        return record


def delete_synthesis(topic: str) -> None:
    with syntheses_lock():
        data = _read_syntheses()
        if data["syntheses"].pop(topic, None) is None:
            raise KeyError(topic)
        _save_syntheses(data)


def synthesis_rows(rows: list[dict] | None = None) -> list[dict]:
    """Every synthesis whose topic still has claims, with `stale` set when a
    claim in the topic was added, removed, or edited since it was written, or
    a tension in it was found, confirmed, dismissed, or deleted. A stale
    synthesis is still shown: it may still be right, and the reader decides
    whether to rewrite it. One whose topic has no claims left is dropped from
    view; `_retag_all` drops it from the file when the tag goes. A record
    written before tensions were part of the basis has none on file and reads
    as stale while the topic has any: what the model saw is not known."""
    rows = rows if rows is not None else claim_rows()
    tensions = tension_rows(rows)
    out = []
    for topic, record in sorted(load_syntheses().items()):
        live = topic_claims(topic, rows)
        if not live:
            continue
        row = dict(record)
        row["topic"] = topic
        row["stale"] = (synthesis_basis(live) != (record.get("claims") or {})
                        or synthesis_tensions(topic, tensions) != (record.get("tensions") or {}))
        row["n_claims"] = len(live)
        row["n_papers"] = len({r["paper"] for r in live})
        out.append(row)
    return out


def synthesis_topics(rows: list[dict] | None = None) -> list[str]:
    """Topics worth a synthesis by default: claims from at least two papers.
    One paper's claims can be synthesized too, by naming the topic."""
    return tension_topics(rows)
