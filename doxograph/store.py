"""JSON-on-disk store.

One file per paper under `papers/`, holding the paper's metadata and its
claims. Keeping claims inside the paper file means provenance stays local and
every change shows up as a readable diff. The cross-paper views (by topic, by
ledger claim) are computed at read time rather than stored.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import itertools
import json
import logging
import os
import re
import string
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Concatenate

import yaml
from pydantic import ValidationError

from . import config, quotes, records, storage, workspace_metadata

logger = logging.getLogger(__name__)

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
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None  # type: ignore[assignment]
try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]


@contextlib.contextmanager
def _file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
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
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
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
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not blocking:
                        raise BlockingIOError('The file lock is held by another process') from exc
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
def _reentrant_file_lock(path: Path, *, blocking: bool = True) -> Iterator[None]:
    """Share thread serialization and reentrancy by the physical lock path.

    A paper and a document can use the same legacy lock filename. Every wrapper
    must therefore share both its RLock and its per-thread file descriptor depth;
    separate semantic mutexes could invert around a nested physical acquisition.
    """
    identity = str(path.resolve())
    with _locks_guard:
        lock = _locks.setdefault(identity, threading.RLock())
    if not lock.acquire(blocking=blocking):
        raise BlockingIOError("The file lock is held by another thread")
    try:
        held = getattr(_depth, "held", None)
        if held is None:
            held = _depth.held = {}
        if held.get(identity):
            held[identity] += 1
            try:
                yield
            finally:
                held[identity] -= 1
            return
        with _file_lock(path, blocking=blocking):
            held[identity] = 1
            try:
                yield
            finally:
                del held[identity]
    finally:
        lock.release()


def document_lock(name: str, *, blocking: bool = True) -> contextlib.AbstractContextManager[None]:
    """Serialize access to a workspace document across threads and processes."""
    return _reentrant_file_lock(config.locks_dir() / f"{name}.lock", blocking=blocking)


# The tag vocabulary is a single shared file, so it gets one lock rather than
# one per paper. Lock order is vocabulary before paper, everywhere, so a tag
# rewrite (vocabulary then each paper) and an accept (vocabulary then that
# paper) cannot deadlock against each other.


@contextlib.contextmanager
def paper_lock(key: str, *, blocking: bool = True) -> Iterator[None]:
    with _reentrant_file_lock(config.locks_dir() / f"{key}.lock", blocking=blocking):
        yield


@contextlib.contextmanager
def extraction_lock(key: str) -> Iterator[None]:
    """Serialize extractions, paper chats, and deletion for one paper.

    This is deliberately separate from ``paper_lock``. An extraction holds it
    across the slow model call so another re-read cannot take a stale snapshot,
    while ordinary claim edits remain free to land and are reconciled by the
    merge under ``paper_lock``.
    """
    path = config.locks_dir() / f"{key}.extraction.lock"
    with _reentrant_file_lock(path):
        yield


@contextlib.contextmanager
def claim_lock() -> Iterator[None]:
    """Guard "is this paper already here, and if not what key does it get".

    Lock order is claim before paper: ingest holds this while reserving and then
    publishes a PDF under the paper lock.
    """
    with _reentrant_file_lock(config.locks_dir() / "claim.lock"):
        yield


@contextlib.contextmanager
def vocab_lock() -> Iterator[None]:
    with _reentrant_file_lock(config.locks_dir() / "vocabulary.lock"):
        _recover_tag_change()
        yield


def _locked[**P, R](func: Callable[Concatenate[str, P], R]) -> Callable[Concatenate[str, P], R]:
    """Run a paper mutator while holding that paper's lock (first arg is the key)."""
    @functools.wraps(func)
    def wrapper(key: str, /, *args: P.args, **kwargs: P.kwargs) -> R:
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


def key_candidates(base: str) -> Iterator[str]:
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
    return set(storage.read_model(retired_keys_path(), workspace_metadata.RetiredKeys).root)


def retire_key(key: str) -> None:
    """Record a key as spent. Uses its own lock, which nothing else nests inside.

    Deliberately not the vocabulary lock: `delete_paper` calls this while holding
    the paper lock, and a tag rename holds the vocabulary lock while taking paper
    locks, so sharing that lock would invert the order and deadlock.
    """
    # The file's reader refuses a blank entry, so one written here would make
    # every later key reservation and removal fail until it was edited out.
    if not key.strip():
        raise ValueError("a retired key must not be blank")
    with _reentrant_file_lock(config.locks_dir() / "retired-keys.lock"):
        known = retired_keys()
        if key not in known:
            known.add(key)
            write_atomic(retired_keys_path(), json.dumps(sorted(known), indent=2) + "\n")


def reserve_key(base: str, **fields: Any) -> str:
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
    with _reentrant_file_lock(config.locks_dir() / "retired-keys.lock"):
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


def text_path(key: str) -> Path:
    """Where the paper's extracted text is kept between runs."""
    return config.text_dir() / f"{key}.txt"


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def write_atomic(path: Path, text: str) -> None:
    """Replace a file's contents in one step.

    `write_text` truncates before writing, and the readers of `tags.yaml` —
    `/api/state`, the extraction prompt, retag — do not take the vocabulary
    lock, so a truncated read is reachable. A retag prompted with an empty
    vocabulary would return empty assignments and clear real tags.
    """
    from .backups import preserve

    preserve(path, text)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(staged, path)
        _sync_directory(path.parent)
    except BaseException:
        Path(staged).unlink(missing_ok=True)
        raise


def write_json(path: Path, payload: Any) -> None:
    """Write atomically so an interrupted or concurrent save cannot truncate a file.

    The temporary name is unique per write; a fixed `.tmp` sibling would be
    shared by two concurrent writers of the same paper.
    """
    write_atomic(path, json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n")


class PaperReadError(ValueError):
    """A paper exists but cannot safely be read or overwritten."""


# Scans can continue past damaged files, but must report them. A file removed
# between listing and reading is different: that is an ordinary concurrent delete.
VANISHED = (KeyError, OSError, json.JSONDecodeError, PaperReadError)


def load_paper(key: str) -> dict[str, Any]:
    path = paper_path(key)
    try:
        paper = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(paper, dict) or paper.get("key") != key:
            raise ValueError("expected a paper object whose key matches its filename")
        claims = paper.get("claims", [])
        if not isinstance(claims, list) or any(
            not isinstance(c, dict) or not isinstance(c.get("id"), str)
            or not isinstance(c.get("tags", []), list)
            or any(not isinstance(tag, str) for tag in c.get("tags", []))
            for c in claims
        ):
            raise ValueError("expected a list of claims with string identifiers and topic lists")
        paper = _migrate(paper)
        _validate_paper(paper)
        return paper
    except FileNotFoundError:
        raise KeyError(key) from None
    except (ValueError, TypeError, AttributeError) as exc:
        raise PaperReadError(f"Cannot read {path}: {exc}") from exc


def _validate_paper(paper: dict[str, Any]) -> None:
    """Validate known fields without filling defaults or discarding extras.

    Missing fields in older corpora remain missing. The migration handles the
    few historical representations we know; validation never coerces bad data.
    """
    try:
        records.Paper.model_validate(paper)
    except ValidationError as exc:
        error = exc.errors(include_input=False, include_url=False)[0]
        location = ".".join(str(part) for part in error["loc"])
        raise ValueError(f"invalid paper field {location}: {error['msg']}") from exc


#: The only thing the ingest ever wrote into the old `notes`. Matching on it
#: is what lets the migration below tell the machine's text from a person's.
LEGACY_ERROR = "PDF download failed:"


def _migrate(paper: dict[str, Any]) -> dict[str, Any]:
    """Bring a paper written by an older version up to the current shape.

    `notes` used to hold the reason a PDF never arrived, written by the
    ingest. It now holds what the reader writes about the paper, and the
    failure has `error` to itself. A file with no `error` key at all predates
    the split, and only the ingest's own message is moved across: the web app
    never wrote there, but `PATCH /api/papers/{key}` accepted `notes` before
    this, so a hand-edited corpus can hold a real note and anything that is
    not recognisably the ingest's is left as one.
    """
    if "error" not in paper:
        note = paper.get("notes") or ""
        moves = note.startswith(LEGACY_ERROR)
        paper["error"] = note if moves else ""
        paper["notes"] = "" if moves else note
    ensure_claim_seq(paper)
    return paper


def save_paper(paper: dict[str, Any]) -> None:
    from .notebook import record_history

    _validate_paper(paper)
    with paper_lock(paper["key"]):
        try:
            previous = load_paper(paper["key"])
        except KeyError:
            previous = None
        if previous is not None:
            record_history(previous, paper)
        paper["updated"] = now()
        write_json(paper_path(paper["key"]), paper)


def paper_keys() -> list[str]:
    return sorted(p.stem for p in config.papers_dir().glob("*.json"))


_reported_paper_issues: dict[Path, dict[str, str]] = {}
_paper_issues_lock = threading.Lock()


def _report_paper_issues(issues: list[records.StorageIssue]) -> None:
    """Log transitions, while callers continue retrying every unreadable file."""
    directory = config.papers_dir().absolute()
    current = {issue["path"]: issue["detail"] for issue in issues}
    with _paper_issues_lock:
        previous = _reported_paper_issues.get(directory, {})
        for path, detail in current.items():
            if previous.get(path) != detail:
                logger.warning("Paper omitted from the library: %s (%s)", path, detail)
        if current:
            _reported_paper_issues[directory] = current
        else:
            # A later recurrence is a new problem, including after deletion
            # and re-import. Healthy workspaces need no retained log state.
            _reported_paper_issues.pop(directory, None)


def all_papers(*, issues: list[records.StorageIssue] | None = None) -> list[dict[str, Any]]:
    papers = []
    unreadable: list[records.StorageIssue] = []
    for key in paper_keys():
        try:
            papers.append(load_paper(key))
        except (KeyError, FileNotFoundError):
            continue
        except (OSError, PaperReadError) as exc:
            issue: records.StorageIssue = {"paper": key, "path": str(paper_path(key)), "detail": str(exc)}
            unreadable.append(issue)
    if issues is not None:
        issues.extend(unreadable)
    _report_paper_issues(unreadable)
    papers.sort(key=lambda p: (p.get("added") or ""), reverse=True)
    return papers


@_locked
def delete_paper(key: str, *, before_remove: Callable[[], None] | None = None) -> None:
    """Retire a key, run optional cleanup, then remove its files under its lock.

    Cleanup may delete remote uploads, so retirement must succeed before it
    starts. If cleanup fails, keep local metadata for a retry. Retirement is
    deliberately not repeated after cleanup, when a new failure would strand
    local metadata referring to uploads that have already been removed.
    """
    # Retire before unlinking. The other order leaves a window where the key is
    # free but not yet recorded, and a concurrent ingest could take it.
    retire_key(key)
    if before_remove is not None:
        before_remove()
    paper_path(key).unlink(missing_ok=True)
    pdf_path(key).unlink(missing_ok=True)
    text_path(key).unlink(missing_ok=True)


def new_paper(key: str, **fields: Any) -> dict[str, Any]:
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
        # The PDF on file as `ingest.pdf_fingerprint` sees it: its size, its
        # mtime and its hash. The hash is what recognises the same file dropped
        # a second time; the other two are what say the hash is still the
        # file's. A paper with no PDF has no fingerprint.
        "pdf_file": {},
        "summary": "",
        "relevance": "",
        "status": "fetched",
        "extraction": None,
        "proposed_tags": [],
        # Labels are about the paper rather than about anything it claims: that
        # it is open source, that a model is on HuggingFace, that it is on the
        # reading list. They are set by hand and no pass touches them, which is
        # why they are not topics: the vocabulary goes into the extraction
        # prompt, and a topic there is something the model is asked to find in
        # the text. Nothing in a paper's text says it is on your reading list.
        "labels": [],
        "claims": [],
        # What the reader writes about the paper: no model reads it and no pass
        # writes it. `error` is the other half of what `notes` used to be —
        # why the ingest has no PDF — kept apart so a note means one thing.
        "notes": "",
        "error": "",
    }
    paper.update(fields)
    return paper


def ensure_claim_seq(paper: dict[str, Any]) -> int:
    """Set and return the paper's claim high-water mark.

    Corpora written before `claim_seq` existed derive it from the ids they
    already hold, including deleted claims retained in edit history. Call this
    on the *whole* paper before filtering its claims:
    deriving it from a subset lets an id belonging to a filtered-out claim be
    issued again, and ids travel in flight.
    """
    seq = paper.get("claim_seq")
    if seq is None:
        used = []
        identifiers = [claim.get("id", "") for claim in paper.get("claims", [])]
        identifiers.extend(entry.get("claim") or "" for entry in paper.get("history", []))
        for identifier in identifiers:
            match = re.search(r"-c(\d+)$", identifier)
            if match:
                used.append(int(match.group(1)))
        seq = max(used, default=0)
        paper["claim_seq"] = seq
    return seq


def next_claim_id(paper: dict[str, Any]) -> str:
    """Allocate a claim id that has never been used on this paper.

    Counting the current claims recycles the id of a deleted one, and ids travel
    in flight: a retag response or a PATCH issued before the deletion is matched
    by id alone and would land on the unrelated replacement.
    """
    seq = ensure_claim_seq(paper) + 1
    paper["claim_seq"] = seq
    return f"{paper['key']}-c{seq}"


def new_claim(paper: dict[str, Any], **fields: Any) -> dict[str, Any]:
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
        # The reader's own note on this claim: what they make of it, what the
        # extraction got wrong, what to check. Never sent to a model, and not
        # part of `claim_fingerprint`, so writing one stales nothing and
        # cannot put a topic back in the queue for a pass.
        "note": "",
        "reviewed": False,
        # True when the quote was found in the PDF, False when it was not, None
        # when there was nothing to check (no quote, or no readable PDF).
        "quote_verified": None,
        # The page the quote was found on, counted from the front of the file,
        # which is what a reviewer needs to check a locator against.
        "quote_page": None,
        "added": now(),
    }
    claim.update(fields)
    return claim


def check_quote(key: str, claim: dict[str, Any]) -> bool | None:
    """Set and return `quote_verified` for one claim against the paper's PDF.

    The search knows where it found the quote, so the page comes along for
    free and is recorded too.
    """
    found = quotes.locate(pdf_path(key), claim.get("quote") or "", text_path(key),
                          guard=lambda: paper_lock(key))
    verified: bool | None = found["found"] if found else None
    claim["quote_verified"] = verified
    claim["quote_page"] = found["page"] if found else None
    return verified


def quote_context(key: str, claim_id: str) -> dict[str, Any]:
    """Where a claim's quote sits in the paper, in the paper's own words.

    The answer to "this quote was not found — then what does the paper say?",
    which is otherwise a hunt through the PDF. It is drawn from the same
    alignment that checks the quote, so the passage it shows is the one the
    check scored.
    """
    paper = load_paper(key)
    claim = next((c for c in paper.get("claims", []) if c.get("id") == claim_id), None)
    if claim is None:
        raise KeyError(claim_id)
    quote = claim.get("quote") or ""
    locator = claim.get("locator") or ""
    context: dict[str, Any] = {
        "claim": claim_id, "quote": quote, "locator": locator,
        "locator_page": quotes.locator_page(locator), "diff": [],
    }
    if not quote.strip():
        return {**context, "available": False, "reason": "This claim has no quote."}
    found = quotes.locate(pdf_path(key), quote, text_path(key), guard=lambda: paper_lock(key))
    if found is None:
        reason = ("This paper has no PDF to check against."
                  if not pdf_path(key).exists()
                  else "No text could be read from this PDF; it may be a scan.")
        return {**context, "available": False, "reason": reason}
    if found["suggestion"] and found["suggestion"] != quote:
        context["diff"] = quotes.word_diff(quote, found["suggestion"])
    return {**context, **found, "available": True, "reason": ""}


@_locked
def verify_quotes(key: str) -> dict[str, Any]:
    """Re-check every quote on a paper. For corpora extracted before quotes
    were checked, and for a PDF that arrived after its claims did."""
    paper = load_paper(key)
    for claim in paper.get("claims", []):
        check_quote(key, claim)
    save_paper(paper)
    return paper


def clean_ledger_links(links: Any) -> list[dict[str, Any]]:
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
    "text", "kind", "strength", "tags", "facets", "evidence", "quote", "locator",
    "ledger_links", "note", "reviewed", "conditions", "limitations", "evidence_origin", "evidence_relationship",
}


class TopicConflictError(ValueError):
    """A manual edit still names a topic that was renamed or deleted."""


@contextlib.contextmanager
def _claim_edit_lock(key: str, patch: dict[str, Any]) -> Iterator[None]:
    # Topic edits share the vocabulary lock with renames, before taking the
    # paper lock. Notes and review decisions can still land during recovery.
    # Facet edits take the facets lock first, as a facet's deletion does.
    with (facets_lock() if "facets" in patch else contextlib.nullcontext(),
          vocab_lock() if "tags" in patch else contextlib.nullcontext()):
        if "tags" in patch:
            try:
                retired = _retired_topics()
            except (OSError, ValueError) as exc:
                raise StorageRecoveryError(f"Cannot validate the selected topics: {exc}") from exc
            stale = set(patch["tags"]) & retired
            if stale:
                raise TopicConflictError(
                    f"Topics were renamed or deleted: {', '.join(sorted(stale))}. "
                    "Update the topic list in your draft and save again."
                )
        with paper_lock(key):
            yield


def update_claim(key: str, claim_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _claim_edit_lock(key, patch):
        paper = load_paper(key)
        claim: dict[str, Any]
        for claim in paper.get("claims", []):
            if claim.get("id") == claim_id:
                for field, value in patch.items():
                    if field == "facets":
                        set_claim_facets_by_hand(claim, value)
                    elif field in CLAIM_FIELDS:
                        claim[field] = clean_ledger_links(value) if field == "ledger_links" else value
                if "quote" in patch:
                    check_quote(key, claim)
                claim["updated"] = now()
                refresh_status(paper)
                save_paper(paper)
                return claim
        raise KeyError(claim_id)


def add_claim(key: str, patch: dict[str, Any]) -> dict[str, Any]:
    with _claim_edit_lock(key, patch):
        paper = load_paper(key)
        fields = {k: v for k, v in patch.items() if k in CLAIM_FIELDS and k != "facets"}
        if "ledger_links" in fields:
            fields["ledger_links"] = clean_ledger_links(fields["ledger_links"])
        claim = new_claim(paper, **fields)
        if "facets" in patch:
            set_claim_facets_by_hand(claim, patch["facets"])
        # A hand-written claim needs no review, but a blank one is not a claim yet.
        # An explicit `reviewed` in the patch still wins.
        if "reviewed" not in patch:
            claim["reviewed"] = bool(claim["text"].strip())
        if claim.get("quote"):
            check_quote(key, claim)
        paper.setdefault("claims", []).append(claim)
        refresh_status(paper)
        save_paper(paper)
        return claim


@_locked
def review_claims(key: str, reviewed: bool, claim_ids: list[str] | None = None) -> list[str]:
    """Mark a paper's claims reviewed, or unreviewed, in one write.

    Returns the ids it actually changed, which is what an undo needs: setting
    them back must not also unreview a claim the person had already reviewed
    before. `claim_ids` is that undo's other half — a list restricts the change
    to those claims, and one not on the paper is ignored rather than refused,
    since the alternative is failing a bulk action over a claim deleted
    meanwhile.
    """
    paper = load_paper(key)
    wanted = set(claim_ids) if claim_ids is not None else None
    changed = []
    for claim in paper.get("claims", []):
        if wanted is not None and claim.get("id") not in wanted:
            continue
        if bool(claim.get("reviewed")) == reviewed:
            continue
        claim["reviewed"] = reviewed
        claim["updated"] = now()
        changed.append(claim["id"])
    if changed:
        refresh_status(paper)
        save_paper(paper)
    return changed


@_locked
def delete_claim(key: str, claim_id: str) -> None:
    paper = load_paper(key)
    before = len(paper.get("claims", []))
    paper["claims"] = [c for c in paper.get("claims", []) if c.get("id") != claim_id]
    if len(paper["claims"]) == before:
        raise KeyError(claim_id)
    refresh_status(paper)
    save_paper(paper)


def refresh_status(paper: dict[str, Any]) -> None:
    claims = paper.get("claims", [])
    if not claims:
        paper["status"] = "extracted" if paper.get("extraction") else "fetched"
    elif all(c.get("reviewed") for c in claims):
        paper["status"] = "reviewed"
    else:
        paper["status"] = "extracted"


# --- controlled vocabulary -------------------------------------------------

DEFAULT_TAGS: list[dict[str, Any]] = []


def _vocabulary() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.tags_path(), workspace_metadata.Vocabulary,
    ).model_dump(exclude_unset=True)


def load_tags() -> list[dict[str, Any]]:
    return _vocabulary().get("tags") or []


def _retired_topics() -> set[str]:
    return set(_vocabulary().get("retired_topics", []))


def save_tags(tags: list[dict[str, Any]], *, retire: str | None = None) -> None:
    """Caller must hold `vocab_lock`; every public mutator below does."""
    ordered = sorted(tags, key=lambda t: t["name"])
    payload = _vocabulary()
    retired = set(payload.get("retired_topics", []))
    if retire is not None:
        retired.add(retire)
    # Explicitly recreating a vocabulary entry makes its name usable again.
    retired.difference_update(t["name"] for t in ordered)
    payload["tags"] = ordered
    if retired:
        payload["retired_topics"] = sorted(retired)
    else:
        payload.pop("retired_topics", None)
    workspace_metadata.Vocabulary.model_validate(payload, strict=True)
    write_atomic(
        config.tags_path(),
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
    )


def tag_names() -> list[str]:
    return [t["name"] for t in load_tags()]


def add_tag(name: str, description: str = "") -> list[dict[str, Any]]:
    name = slugify(name)
    with vocab_lock():
        tags = load_tags()
        if name and name not in {t["name"] for t in tags}:
            tags.append({"name": name, "description": description})
            save_tags(tags)
        return tags


class StorageRecoveryError(RuntimeError):
    """An interrupted topic or facet change still needs to finish."""

    def __init__(self, message: str, path: Path | None = None):
        super().__init__(message)
        # The intent file the reader is told to keep.
        self.path = path if path is not None else tag_change_path()


def tag_change_path() -> Path:
    return config.data_dir() / "pending-topic-change.json"


def _recover_tag_change() -> None:
    """Finish a durable intent under the vocabulary lock, including on restart.

    Reapply the topic transformation to current records, never replay old paper
    snapshots: notes and review decisions written since the failure must survive.
    Each step is idempotent. Keep the intent until every replacement is durable.
    """
    path = tag_change_path()
    try:
        try:
            change = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (not isinstance(change, dict) or not {"version", "old", "new", "tags"} <= change.keys()
                or change.get("version") != 1
                or not isinstance(change.get("old"), str)
                or not (change.get("new") is None or isinstance(change.get("new"), str))
                or not isinstance(change.get("tags"), list)
                or any(not isinstance(t, dict) or not isinstance(t.get("name"), str)
                       for t in change["tags"])):
            raise ValueError("invalid pending topic change")
        # A damaged vocabulary must block recovery before any papers move.
        _vocabulary()
        workspace_metadata.Vocabulary.model_validate({"tags": change["tags"]}, strict=True)
        _retag_all(change["old"], change["new"])
        # Publish the vocabulary last. Until then, the durable intent explains
        # any partially moved claims and prevents a competing vocabulary edit.
        save_tags(change["tags"], retire=change["old"])
        path.unlink()
        _sync_directory(path.parent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StorageRecoveryError(
            f"A topic change is unfinished: {exc}. Doxograph will retry automatically. "
            f"Keep {path} until recovery finishes."
        ) from exc


def recover_storage() -> None:
    """Called before serving library state and before CLI operations."""
    # Ordinary reads do not need write access to a lock file. Only take the
    # lock when there is recovery work; its holder rechecks the intent.
    try:
        if tag_change_path().exists():
            with vocab_lock():
                pass
    except OSError as exc:
        raise StorageRecoveryError(
            f"Cannot resume the unfinished topic change: {exc}. "
            f"Keep {tag_change_path()}; Doxograph will retry automatically."
        ) from exc
    try:
        if facet_change_path().exists():
            with facets_lock():
                pass
    except OSError as exc:
        raise StorageRecoveryError(
            f"Cannot resume the unfinished facet change: {exc}. "
            f"Keep {facet_change_path()}; Doxograph will retry automatically.",
            path=facet_change_path(),
        ) from exc


def _change_tag(old: str, new: str | None, tags: list[dict[str, Any]]) -> None:
    """Start a recoverable topic change; caller holds the vocabulary lock."""
    # Refuse known damage before starting a multi-file operation. A paper
    # deleted during the scan is fine; an unreadable one must not be skipped.
    for key in paper_keys():
        try:
            load_paper(key)
        except KeyError:
            continue
    _read_tensions()
    _read_agreements()
    _read_syntheses()
    _retired_topics()
    write_json(tag_change_path(), {"version": 1, "old": old, "new": new, "tags": tags})
    _recover_tag_change()


def rename_tag(old: str, new: str) -> None:
    """Rename across the vocabulary and every claim. Merges if `new` exists.

    A name with nothing left once slugged ("!!", "日本語") is refused: renaming
    to "" would take the topic off every claim and drop its synthesis, which
    is a delete, and the caller asked for a rename.
    """
    new = slugify(new)
    if not new:
        raise ValueError("a topic name needs at least one letter or digit")
    with vocab_lock():
        current = load_tags()
        tags = [t for t in current if t["name"] != old]
        if new not in {t["name"] for t in tags}:
            renamed = next((dict(t) for t in current if t["name"] == old), {"description": ""})
            renamed["name"] = new
            tags.append(renamed)
        _change_tag(old, new, tags)


def _move_pass(data: dict[str, Any], old: str, new: str | None) -> bool:
    """Carry a topic's recorded pass to its new name, or drop it with the topic.

    A deleted topic loses the pairs it was attached to. Recreating the tag over
    the same unchanged claims produces the same signature, so a pass left on
    file would be skipped and the topic never put back on those pairs.

    Where the new name is a topic of its own — a merge rather than a rename —
    its own pass stands. It was recorded against its own claims and its own
    name, and the old topic's signature, which carries the old name, could
    only say the destination had never been asked. If the merge brings claims
    the destination did not have, its signature no longer matches what a pass
    would compute now, and the pass runs again of its own accord.
    """
    passes = data.get("passes") or {}
    if old not in passes:
        return False
    signature = passes.pop(old)
    if new and new not in passes:
        passes[new] = signature
    data["passes"] = passes
    return True


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
            except KeyError:
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
        touched = _move_pass(data, old, new)
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
    with agreements_lock():
        data = _read_agreements()
        touched = _move_pass(data, old, new)
        for record in data["agreements"]:
            if old not in record.get("topics", []):
                continue
            kept = {t for t in record["topics"] if t != old}
            if new:
                kept.add(new)
            record["topics"] = sorted(kept)
            touched = True
        if touched:
            _save_agreements(data)
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
        _change_tag(name, None, [t for t in load_tags() if t["name"] != name])


# --- your own claims, and what the research is about ----------------------

def context_path() -> Path:
    return config.data_dir() / "context.md"


def load_context() -> str:
    """The free-text description of the research, given to every model pass."""
    return storage.read_text(context_path()).strip()


def save_context(text: str) -> None:
    with document_lock("context"):
        load_context()
        write_atomic(context_path(), (text or "").strip() + "\n")


def _ledger() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.ledger_path(), workspace_metadata.ResearchLedger,
    ).model_dump(exclude_unset=True)


def load_ledger() -> list[dict[str, Any]]:
    return _ledger().get("claims") or []


def save_ledger(claims: list[dict[str, Any]]) -> None:
    with document_lock("ledger"):
        payload = _ledger()
        existing = {claim["id"]: claim for claim in payload.get("claims") or []}
        payload["claims"] = [{**existing.get(claim["id"], {}), **claim} for claim in claims]
        workspace_metadata.ResearchLedger.model_validate(payload, strict=True)
        write_atomic(
            config.ledger_path(),
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
        )


# --- cross-paper views ----------------------------------------------------

def corpus_signature() -> str:
    """A string that changes whenever anything `/api/state` shows would.

    Built from directory listings and file stats rather than file contents:
    a few hundred stats cost a millisecond or two, where reading every paper
    costs a good fraction of a second, and the page asks twice a second. The
    writes are all atomic replaces, so a changed file has a new inode and
    mtime; a CLI process writing the corpus is seen the same way as this one.
    Ctime also detects ACL changes that leave contents and mode bits unchanged.
    """
    parts: list[tuple[Any, ...]] = []
    for directory, suffix in ((config.papers_dir(), ".json"), (config.pdfs_dir(), ".pdf")):
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.endswith(suffix) and not entry.name.startswith("."):
                        st = entry.stat()
                        parts.append((entry.name, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode, st.st_ctime_ns))
        except FileNotFoundError:
            pass
    for path in (config.tags_path(), config.facets_path(), facet_change_path(), config.ledger_path(),
                 tensions_path(), syntheses_path(),
                 agreements_path(), context_path(), config.data_dir() / "notebook.json",
                 tag_change_path(), retired_keys_path()):
        try:
            st = path.stat()
            parts.append((path.name, st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode, st.st_ctime_ns))
        except FileNotFoundError:
            parts.append((path.name,))
        except OSError as exc:
            # The reader will report the failed path. A failed stat must not
            # prevent the library from reaching those independent readers.
            parts.append((path.name, "unreadable", exc.errno, str(exc)))
    parts.sort()
    return hashlib.sha1(repr(parts).encode()).hexdigest()


def claim_rows(papers: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Flatten every claim with the paper fields needed to display it."""
    rows = []
    for paper in papers if papers is not None else all_papers():
        for claim in paper.get("claims", []):
            row = dict(claim)
            row["paper"] = paper["key"]
            row["paper_title"] = paper.get("title", "")
            # Older imports record unknown authors as null; every reader takes a list.
            row["paper_authors"] = paper.get("authors") or []
            row["paper_year"] = paper.get("year")
            # The labels travel with the claim so that a query matching a
            # paper matches its claims too, as the title and the year already
            # do. Nothing a model pass reads includes them: `claim_fingerprint`
            # and `synthesis_claim_basis` name their fields one by one, so
            # labelling a paper does not stale a synthesis.
            row["paper_labels"] = paper.get("labels") or []
            rows.append(row)
    return rows


def label_key(label: str) -> str:
    """What makes two labels one: "RLHF" and "rlhf" are the same label written
    two ways, so they share a count, a filter, and a color.

    Lowering, raising, and lowering again makes Σ, σ, and ς one key, and ß, ẞ,
    and SS another, as `casefold` would. It is spelled out because the browser
    has no `casefold` and has to reach the same key by the same steps. Dotless
    ı is left out of the round trip: raising it gives I, which would join it
    to i, and `casefold` keeps the two apart. No other letter differs.
    """
    return "ı".join(part.upper().lower() for part in label.lower().split("ı"))


def label_keys(papers: list[dict[str, Any]]) -> dict[str, str]:
    """Each stored spelling's key, for the browser to use in place of its own.

    The browser folds case with its own Unicode tables, which can be newer or
    older than Python's. A label the server holds apart must stay apart in the
    page, or saving the label editor unchanged could merge two labels into one.
    """
    return {label: label_key(label) for paper in papers for label in paper.get("labels") or []}


def has_label(paper: dict[str, Any], label: str) -> bool:
    return label_key(label) in {label_key(name) for name in paper.get("labels") or []}


def normalize_labels(values: list[str]) -> list[str]:
    """Labels as written, with whitespace normalized and spaces preserved.

    Each value is one label. Case is kept for display; deduplication ignores
    it and keeps the first spelling, so the sidebar counts a label once per
    paper. Commas are reserved for separating labels in the editor.
    """
    if any("," in value for value in values):
        raise ValueError("Labels cannot contain commas. Provide each label separately.")
    labels: dict[str, str] = {}
    for value in values:
        if label := " ".join(value.split()):
            labels.setdefault(label_key(label), label)
    return sorted(labels.values(), key=lambda label: (label_key(label), label))


def set_labels(key: str, labels: list[str]) -> dict[str, Any]:
    """Replace one paper's labels, under its lock."""
    with paper_lock(key):
        paper = load_paper(key)
        paper["labels"] = normalize_labels(labels)
        save_paper(paper)
        return paper


def label_counts(papers: list[dict[str, Any]]) -> dict[str, int]:
    """How many papers carry each label, commonest first, ties by name.

    The same shape and order as `tag_counts`, which counts claims: the two
    lists sit next to each other in the sidebar and should not be read
    differently. Spellings that differ only in case count as one label, shown
    as the spelling most papers use, the one with more capitals on a tie.
    """
    spellings: dict[str, dict[str, int]] = {}
    for paper in papers:
        # One spelling per paper, the first, as `normalize_labels` keeps.
        mine: dict[str, str] = {}
        for label in paper.get("labels") or []:
            mine.setdefault(label_key(label), label)
        for key, label in mine.items():
            seen = spellings.setdefault(key, {})
            seen[label] = seen.get(label, 0) + 1
    # On a tie, the spelling with more capitals in any script, then code points.
    counts = {min(seen, key=lambda name: (-seen[name], -sum(c.isupper() for c in name), name)):
              sum(seen.values()) for seen in spellings.values()}
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], label_key(kv[0]), kv[0])))


def tag_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for tag in row.get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# --- facets ----------------------------------------------------------------
#
# A facet is a question asked of every claim whose answer is a short list of
# names: which models a result was measured on, which datasets, which
# intervention. The reader defines the facets a workspace has, each with a
# description that goes into the extraction prompt, and the values are filled
# in by the model and corrected by hand.
#
# Values are open: a facet has no fixed list, since a corpus about language
# models names a new one with every other paper. What it has instead is
# aliases, written when a reader merges two spellings, so "gpt-4-0613" read
# from the next paper lands on "GPT-4" without anyone asking twice.
#
# Lock order is facets before vocabulary before paper.
#
# Deleting a facet and rewriting its values touch every paper and then the
# definitions, so each is recorded first as an intent, as a topic change is.
# Whoever takes the facets lock next finishes one that was cut short.

def facet_change_path() -> Path:
    return config.data_dir() / "pending-facet-change.json"


@contextlib.contextmanager
def facets_lock() -> Iterator[None]:
    with document_lock("facets"):
        _recover_facet_change()
        yield


FACET_CHANGES = ("values", "delete")


def _recover_facet_change() -> None:
    """Finish a recorded facet change; caller holds the facets lock.

    The change is applied to the papers as they are now rather than replayed
    from snapshots, so edits made since survive, and each step is idempotent.
    The definitions are written last and the intent removed after them:
    until then the intent is what explains values the papers already carry.
    """
    path = facet_change_path()
    try:
        try:
            change = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if (not isinstance(change, dict) or change.get("version") != 1
                or change.get("kind") not in FACET_CHANGES
                or not isinstance(change.get("facet"), str)
                or not isinstance(change.get("facets"), list)
                or not isinstance(change.get("old"), list)
                or any(not isinstance(key, str) for key in change["old"])
                or not isinstance(change.get("new"), str)):
            raise ValueError("invalid pending facet change")
        # A damaged definitions file must block recovery before papers move.
        _facets_file()
        workspace_metadata.Facets.model_validate({"facets": change["facets"]}, strict=True)
        if change["kind"] == "delete":
            _rewrite_papers(lambda paper: _strip_facet(paper, change["facet"]))
        else:
            old, new = set(change["old"]), change["new"]
            _rewrite_papers(lambda paper: _rewrite_values(paper, change["facet"], old, new))
        _save_facets(change["facets"])
        path.unlink()
        _sync_directory(path.parent)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StorageRecoveryError(
            f"A facet change is unfinished: {exc}. Doxograph will retry automatically. "
            f"Keep {path} until recovery finishes.",
            path=path,
        ) from exc


def _change_facets(kind: str, name: str, facets: list[dict[str, Any]], old: list[str] | None = None,
                   new: str = "") -> None:
    """Record a facet change, then carry it out; caller holds the facets lock."""
    # Refuse known damage before starting: an unreadable paper part way
    # through would leave the change recorded and stuck.
    for key in paper_keys():
        try:
            load_paper(key)
        except KeyError:
            continue
    workspace_metadata.Facets.model_validate({"facets": facets}, strict=True)
    write_json(facet_change_path(), {"version": 1, "kind": kind, "facet": name, "facets": facets,
                                     "old": sorted(old or []), "new": new})
    _recover_facet_change()


def _facets_file() -> dict[str, Any]:
    return storage.read_yaml_model(
        config.facets_path(), workspace_metadata.Facets,
    ).model_dump(exclude_unset=True)


def load_facets() -> list[dict[str, Any]]:
    return _facets_file().get("facets") or []


def _save_facets(facets: list[dict[str, Any]]) -> None:
    """Caller must hold `facets_lock`."""
    payload = _facets_file()
    payload["facets"] = facets
    workspace_metadata.Facets.model_validate(payload, strict=True)
    write_atomic(
        config.facets_path(),
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=100),
    )


def facet_basis(facet: dict[str, Any]) -> str:
    """What a facet's values were filled in against. Changing the description
    changes the question, so the values on every paper go stale; aliases do
    not, since they rewrite values already on file as they are added."""
    return hashlib.sha1(json.dumps([facet["name"], facet.get("description", "")]).encode()).hexdigest()[:16]


def facet_value_key(value: str) -> str:
    """What makes two values one: case and spacing, as for labels."""
    return label_key(facet_value_text(value))


def facet_spellings(rows: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """For each facet, every value on file by its key, in the spelling the
    corpus uses most. A value read from a new paper takes that spelling, so
    "gpt-4" and "GPT-4" are one value without anyone merging them."""
    counts = facet_counts(rows, facets)
    return {name: {facet_value_key(value): value for value in values} for name, values in counts.items()}


def facet_value_text(value: str) -> str:
    """A value as it is stored. Commas separate values in the editor, as they
    separate labels, so a value cannot hold one: a comma inside a value read
    from a paper becomes a space, and "Llama 2, 7B" is kept as one value
    rather than split into two by the next save."""
    return " ".join(value.replace(",", " ").split())


def clean_facet_values(values: Any, facet: dict[str, Any], known: dict[str, str] | None = None) -> list[str]:
    """A facet's values as they are stored: commas and whitespace collapsed,
    aliases applied, one spelling per value, sorted."""
    aliases: dict[str, str] = facet.get("aliases") or {}
    cleaned: dict[str, str] = {}
    for raw in values if isinstance(values, list) else []:
        if not isinstance(raw, str) or not (value := facet_value_text(raw)):
            continue
        key = facet_value_key(value)
        if key in aliases:
            value = aliases[key]
            if not value:
                continue
            key = facet_value_key(value)
        cleaned.setdefault(key, known.get(key, value) if known else value)
    return sorted(cleaned.values(), key=lambda value: (facet_value_key(value), value))


def clean_claim_facets(raw: Any, facets: list[dict[str, Any]],
                       known: dict[str, dict[str, str]] | None = None) -> dict[str, list[str]]:
    """The values for each defined facet; a name no facet has is dropped, and
    so is an empty list, which says no more than a missing one."""
    raw = raw if isinstance(raw, dict) else {}
    cleaned = {}
    for facet in facets:
        values = clean_facet_values(raw.get(facet["name"]), facet, (known or {}).get(facet["name"]))
        if values:
            cleaned[facet["name"]] = values
    return cleaned


def set_claim_facets_by_hand(claim: dict[str, Any], raw: Any) -> None:
    """Apply a person's facet values to a claim. Only the facets named in
    `raw` are set, and an empty list clears one; the rest keep what they
    hold, which may be newer than anything the person was shown. A facet
    whose values they changed is recorded as theirs, and no later pass
    overwrites it."""
    facets = load_facets()
    raw = raw if isinstance(raw, dict) else {}
    before = claim.get("facets") or {}
    given = clean_claim_facets(raw, facets)
    after = {name: values for name, values in before.items()
             if name not in raw and any(facet["name"] == name for facet in facets)}
    after.update(given)
    after = {facet["name"]: after[facet["name"]] for facet in facets if facet["name"] in after}
    hand = set(claim.get("facets_hand") or [])
    for facet in facets:
        name = facet["name"]
        if before.get(name, []) != after.get(name, []):
            hand.add(name)
    claim["facets"] = after
    defined = {facet["name"] for facet in facets}
    claim["facets_hand"] = sorted(hand & defined)
    if not claim["facets_hand"]:
        claim.pop("facets_hand")


def claim_needs_facet(claim: dict[str, Any], facet: dict[str, Any]) -> bool:
    """Whether no pass has given this claim values for `facet` under its
    current description, and no person has set them."""
    if facet["name"] in (claim.get("facets_hand") or []):
        return False
    return (claim.get("facet_basis") or {}).get(facet["name"]) != facet_basis(facet)


def mark_facets_filled(claim: dict[str, Any], facets: list[dict[str, Any]]) -> None:
    """Record that a pass gave this claim values for `facets` as they are."""
    claim["facet_basis"] = {**(claim.get("facet_basis") or {}),
                            **{facet["name"]: facet_basis(facet) for facet in facets}}


def stale_facets(paper: dict[str, Any], facets: list[dict[str, Any]]) -> list[str]:
    """The facets some claim on this paper still needs values for. Tracked
    claim by claim rather than for the paper: a claim a pass left alone, such
    as one edited while the model ran or one a re-read kept, stays stale
    until a pass reaches it."""
    claims = paper.get("claims", [])
    return [facet["name"] for facet in facets if any(claim_needs_facet(c, facet) for c in claims)]


def reconcile_claim_facets(claim: dict[str, Any], facets: list[dict[str, Any]]) -> None:
    """Bring facet data from an older copy of a claim, from history or a
    backup, into line with the facets as they are now: values for a deleted
    facet are dropped, aliases and removals made since are applied, and
    ownership and fill records are kept only for facets still defined."""
    defined = {facet["name"] for facet in facets}
    claim["facets"] = clean_claim_facets(claim.get("facets"), facets)
    hand = [name for name in claim.get("facets_hand") or [] if name in defined]
    if hand:
        claim["facets_hand"] = sorted(set(hand))
    else:
        claim.pop("facets_hand", None)
    basis = {name: value for name, value in (claim.get("facet_basis") or {}).items() if name in defined}
    if basis:
        claim["facet_basis"] = basis
    else:
        claim.pop("facet_basis", None)


def facet_counts(rows: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """How many claims carry each value of each facet, commonest first.
    Spellings that differ only in case count as one value, shown as the
    spelling most claims use."""
    result = {}
    for facet in facets:
        name = facet["name"]
        spellings: dict[str, dict[str, int]] = {}
        for row in rows:
            for value in (row.get("facets") or {}).get(name, []):
                seen = spellings.setdefault(facet_value_key(value), {})
                seen[value] = seen.get(value, 0) + 1
        counts = {min(seen, key=lambda value: (-seen[value], value)): sum(seen.values())
                  for seen in spellings.values()}
        result[name] = dict[str, Any](sorted(counts.items(), key=lambda kv: (-kv[1], facet_value_key(kv[0]), kv[0])))
    return result


def stale_facet_papers(papers: list[dict[str, Any]], facets: list[dict[str, Any]]) -> dict[str, int]:
    """For each facet, how many papers have claims it has not been filled on."""
    counts = {facet["name"]: 0 for facet in facets}
    for paper in papers:
        for name in stale_facets(paper, facets):
            counts[name] += 1
    return counts


def add_facet(name: str, description: str = "") -> list[dict[str, Any]]:
    name = slugify(name)
    if not name:
        raise ValueError("Name the facet with at least one letter or digit.")
    with facets_lock():
        facets = load_facets()
        if any(facet["name"] == name for facet in facets):
            raise ValueError(f"There is already a facet named {name}.")
        facets.append({"name": name, "description": " ".join(description.split())})
        _save_facets(facets)
        return facets


def describe_facet(name: str, description: str) -> list[dict[str, Any]]:
    """Change what a facet asks for. Every paper's values for it go stale."""
    with facets_lock():
        facets = load_facets()
        facet = next((facet for facet in facets if facet["name"] == name), None)
        if facet is None:
            raise KeyError(name)
        facet["description"] = " ".join(description.split())
        _save_facets(facets)
        return facets


def _rewrite_papers(rewrite: Callable[[dict[str, Any]], bool]) -> None:
    """Apply `rewrite` to every paper under its lock, saving the ones it
    changed. Caller holds `facets_lock`."""
    for key in paper_keys():
        with paper_lock(key):
            try:
                paper = load_paper(key)
            except KeyError:
                continue
            if rewrite(paper):
                save_paper(paper)


def delete_facet(name: str) -> list[dict[str, Any]]:
    """Remove a facet and its values from every claim.

    The values go first and the definition last, so a delete cut short
    leaves the facet defined with fewer values, which a fill puts right,
    rather than values for a facet nobody can see."""
    with facets_lock():
        if not any(facet["name"] == name for facet in load_facets()):
            raise KeyError(name)
        _change_facets("delete", name, [facet for facet in load_facets() if facet["name"] != name])
        return load_facets()


def _strip_facet(paper: dict[str, Any], name: str) -> bool:
    changed = False
    for claim in paper.get("claims", []):
        changed |= (claim.get("facets") or {}).pop(name, None) is not None
        changed |= (claim.get("facet_basis") or {}).pop(name, None) is not None
        hand = claim.get("facets_hand") or []
        if name in hand:
            claim["facets_hand"] = [n for n in hand if n != name]
            changed = True
    return changed


def _rewrite_values(paper: dict[str, Any], name: str, old_keys: set[str], new: str) -> bool:
    """Put `new` in place of the values `old_keys` names, or drop them when
    `new` is empty. Running it twice changes nothing more."""
    changed = False
    for claim in paper.get("claims", []):
        values = (claim.get("facets") or {}).get(name)
        if not values or not any(facet_value_key(v) in old_keys for v in values):
            continue
        kept = [v for v in values if facet_value_key(v) not in old_keys] + ([new] if new else [])
        cleaned = clean_facet_values(kept, {"name": name})
        if cleaned:
            claim["facets"][name] = cleaned
        else:
            claim["facets"].pop(name)
        changed = True
    return changed


def rewrite_facet_values(name: str, old: list[str], new: str) -> list[dict[str, Any]]:
    """Rename one value, merge several into one, or with an empty `new`
    remove them, on every claim. The old spellings become aliases, so a later
    read writes the new value in their place, or drops them."""
    new = facet_value_text(new)
    with facets_lock():
        facets = load_facets()
        facet = next((facet for facet in facets if facet["name"] == name), None)
        if facet is None:
            raise KeyError(name)
        old_keys = {facet_value_key(value) for value in old if value.strip()}
        new_key = facet_value_key(new) if new else None
        old_keys.discard(new_key)
        if not old_keys:
            return facets
        aliases = dict[str, Any](facet.get("aliases") or {})
        # An alias that pointed at a value being renamed follows it.
        for key, target in aliases.items():
            if target and facet_value_key(target) in old_keys:
                aliases[key] = new
        for key in old_keys:
            aliases[key] = new
        if new_key is not None:
            aliases.pop(new_key, None)
        facet["aliases"] = dict[str, Any](sorted(aliases.items()))
        _change_facets("values", name, facets, sorted(old_keys), new)
        return load_facets()


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


def summarize(paper: dict[str, Any]) -> dict[str, Any]:
    claims = paper.get("claims", [])
    return {
        "key": paper["key"],
        "title": paper.get("title", ""),
        "authors": paper.get("authors") or [],
        "year": paper.get("year"),
        "venue": paper.get("venue", ""),
        "status": paper.get("status", "fetched"),
        "labels": paper.get("labels") or [],
        "summary": paper.get("summary", ""),
        "relevance": paper.get("relevance", ""),
        # The reader's note travels with the listing rather than behind a fetch
        # of its own: the page shows it in the paper's header and searches it,
        # both of which happen on every keystroke.
        "notes": paper.get("notes", ""),
        "source": paper.get("source", {}),
        "added": paper.get("added"),
        "updated": paper.get("updated"),
        "n_claims": len(claims),
        "n_unreviewed": sum(1 for c in claims if not c.get("reviewed")),
        "n_unverified": sum(1 for c in claims if c.get("quote_verified") is False),
        "n_proposed_tags": len(paper.get("proposed_tags", [])),
        "schema_version": (paper.get("extraction") or {}).get("schema_version"),
        "has_pdf": pdf_path(paper["key"]).exists(),
        # What `refresh_status` reads when a paper has no claims: read once
        # rather than inferred from `schema_version`, which a paper extracted
        # before that field existed does not carry. The page needs it to work
        # out the status of a paper whose last claim it is holding.
        "has_extraction": bool(paper.get("extraction")),
        "error": paper.get("error", ""),
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



def tensions_path() -> Path:
    return config.data_dir() / "tensions.json"


@contextlib.contextmanager
def tensions_lock() -> Iterator[None]:
    """Guard `tensions.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a tension merge
    takes both so it cannot land halfway through a rename. The pass itself
    reads papers without their locks and writes only here."""
    with _reentrant_file_lock(config.locks_dir() / "tensions.lock"):
        yield


def _read_tensions() -> dict[str, Any]:
    """Read the ledger, reporting where it is malformed rather than reading it
    as empty. Every writer starts here, so an empty reading would be written
    back over the file: every decision gone and ids restarting at t1. Only a
    missing file is an empty ledger. Writes are atomic, so a half-written file
    is never seen; anything unreadable was edited by hand or damaged."""
    loaded = storage.read_model(tensions_path(), records.Tensions).model_dump(exclude_unset=True)
    loaded.setdefault("seq", 0)
    loaded.setdefault("tensions", [])
    loaded.setdefault("passes", {})
    return loaded


def load_tensions() -> list[dict[str, Any]]:
    return list(_read_tensions()["tensions"])


def _save_tensions(data: dict[str, Any]) -> None:
    """Caller holds `tensions_lock`."""
    write_json(tensions_path(), data)


def _pair(ids: Iterable[str]) -> tuple[str, str]:
    a, b = sorted(ids)
    return (a, b)


def _live_claims() -> tuple[dict[str, dict[str, Any]], bool]:
    """Every claim on disk by id, and whether any paper could not be read.

    A merge prunes records against the corpus as it is now, and a claim whose
    paper is unreadable is not gone: the file is there, and the reader refuses
    it so a writer cannot replace it. Dropping the tensions and agreements
    that rest on it would be the loss the reader guards against, so while a
    paper cannot be read, a record with a claim that is not in `live` is left
    exactly as the file has it. A claim id names its paper only by convention
    (`next_claim_id`); an older or hand-written id need not, so the missing
    claim cannot be told from the unreadable paper's, and neither is pruned.
    """
    issues: list[records.StorageIssue] = []
    papers = all_papers(issues=issues)
    return {c["id"]: c for c in claim_rows(papers)}, bool(issues)


def _held_back(ids: Iterable[str], live: dict[str, dict[str, Any]], unreadable: bool) -> bool:
    """Whether a record's claims include one that cannot be seen right now."""
    return unreadable and any(i not in live for i in ids)


def _shared_topics(tension: dict[str, Any], live: dict[str, dict[str, Any]]) -> list[str]:
    """The stored topics both claims still carry. A topic removed from either
    claim in the editor leaves the tension with it: a pass under that topic
    can no longer return the pair, since the edited claim is no longer in the
    prompt, and nothing else would ever take the name off. A tension left
    with no topics is still a tension."""
    tags = [set(live[i].get("tags") or []) for i in tension.get("claims", [])]
    return sorted(t for t in tension.get("topics", []) if all(t in s for s in tags))


def claim_fingerprint(claim: dict[str, Any]) -> str:
    """What a tension's judgment rests on: everything `_tension_listing` shows
    the model about a claim, whose paper included. If the text, evidence or
    kind changes the judgment was made about a claim that no longer exists,
    and if the paper's author, year or title changes the note the model wrote
    may name the wrong people."""
    return json.dumps([claim.get("text", ""), claim.get("evidence", ""),
                       claim.get("kind", "finding"), cite_head(claim)])


def cite_head(claim: dict[str, Any]) -> str:
    """How a cross-paper prompt names a claim's paper.

    `_tension_listing` writes the cited surname, "et al." where there is more
    than one author, the year and the title. Adding a coauthor changes the
    heading without changing any one of those fields, so the count is in here
    too.
    """
    authors = claim.get("paper_authors") or []
    key = claim.get("paper") or ""
    head = cite_surname(authors, key)
    if len(authors) > 1:
        head += " et al."
    return f"{head} ({claim.get('paper_year') or 'n.d.'}): {claim.get('paper_title') or key}"


def _forget_passes(data: dict[str, Any], topics: set[str]) -> None:
    """Drop the recorded pass for each of `topics`. Caller saves."""
    passes = data.get("passes") or {}
    for topic in topics:
        passes.pop(topic, None)
    data["passes"] = passes


def _topic_unchanged(topic: str, shown: dict[str, dict[str, Any]], live: dict[str, dict[str, Any]]) -> bool:
    """Whether a topic still holds the claims a pass was asked about, as it
    was asked about them. Anything else and the answer describes a question
    that is no longer being put."""
    def basis(claims: dict[str, dict[str, Any]]) -> dict[str, str]:
        return {i: claim_fingerprint(c) for i, c in claims.items()
                if topic in (c.get("tags") or [])}
    was = basis(shown)
    return bool(was) and was == basis(live)


def pass_signature(topic: str, rows: list[dict[str, Any]], extra: str = "") -> str:
    """What a per-topic pass was asked about, in one string.

    Everything the prompt carries: which claims are in the topic, what each one
    says, and which paper it is from — the listing names the authors, year and
    title, and correcting any of those is a correction the model should see —
    plus `extra` for the parts that are not claims at all: the topic's
    description, the research context, the prompt itself. A pass whose
    signature is unchanged would be asked exactly what it was asked last time,
    and the merge would keep the answer it already has, so it is not asked.
    """
    payload = [topic, extra] + sorted(
        f"{r['id']} {r.get('paper')} {claim_fingerprint(r)}" for r in rows
    )
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()[:16]


def tension_pass(topic: str) -> str | None:
    """The signature of the last tensions pass over `topic`, if any."""
    return (_read_tensions().get("passes") or {}).get(topic)


def agreement_pass(topic: str) -> str | None:
    """The signature of the last agreements pass over `topic`, if any."""
    return (_read_agreements().get("passes") or {}).get(topic)


def record_tensions(topic: str, found: list[dict[str, Any]], claims_by_id: dict[str, dict[str, Any]],
                    signature: str | None = None) -> dict[str, Any]:
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
        live, unreadable = _live_claims()
        existing = [t for t in data["tensions"]
                    if len(t.get("claims", [])) == 2
                    and (all(i in live for i in t["claims"]) or _held_back(t["claims"], live, unreadable))]
        pruned: set[str] = set()
        for tension in existing:
            if _held_back(tension["claims"], live, unreadable):
                continue
            was = set(tension.get("topics") or [])
            tension["topics"] = _shared_topics(tension, live)
            pruned |= was - set(tension["topics"])
        # A topic taken off a record here is a topic whose pairs have to be
        # found again when it comes back. Its recorded pass goes with it, or
        # restoring the tag would reproduce the signature and skip the run.
        _forget_passes(data, pruned)
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
                    # The same pair over the same text: the finding stands, and
                    # keeps the day it was found and whatever was decided about
                    # it. A newer model or a newer prompt can still put it in
                    # better words — unless a reviewer has ruled on the words
                    # it has, which is a decision about what it says.
                    if current.get("status") == "open":
                        current.update(kind=kind, note=note)
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
        # Recorded only on the way out, so a failed call is asked again, and
        # only while the topic still holds the claims it was asked about. A
        # tag taken off one of them during the call — or off all of them —
        # means this answer was about a topic that no longer exists in that
        # shape; writing its signature down would skip the pass that has to
        # run when the tag comes back.
        if signature and _topic_unchanged(topic, claims_by_id, live):
            data.setdefault("passes", {})[topic] = signature
        _save_tensions(data)
        return {"added": added, "reopened": reopened, "kept": kept}


def set_tension_status(tension_id: str, status: str) -> dict[str, Any]:
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
        tension: dict[str, Any]
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
    """Remove a tension, and forget the passes that found it.

    Deleting is not dismissing: a dismissed pair stays on file and a repeat
    pass leaves the decision alone, but a deleted one is gone, and the topics
    it was found under have to be asked again for it to come back. Their
    signatures would otherwise match and the pass would skip them.
    """
    with tensions_lock():
        data = _read_tensions()
        before = len(data["tensions"])
        gone = [t for t in data["tensions"] if t.get("id") == tension_id]
        data["tensions"] = [t for t in data["tensions"] if t.get("id") != tension_id]
        if len(data["tensions"]) == before:
            raise KeyError(tension_id)
        _forget_passes(data, {topic for t in gone for topic in (t.get("topics") or [])})
        _save_tensions(data)


def tension_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
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
    out.sort(key=lambda t: (order.get(t.get("status", ""), 9), t.get("found") or ""), reverse=False)
    return out


def tension_topics(rows: list[dict[str, Any]] | None = None) -> list[str]:
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



def syntheses_path() -> Path:
    return config.data_dir() / "syntheses.json"


@contextlib.contextmanager
def syntheses_lock() -> Iterator[None]:
    """Guard `syntheses.json`. Nothing else nests inside it. It nests inside
    `vocab_lock` alone: a tag rename rewrites topics here, and a write from
    the model takes both so it cannot land halfway through a rename. It is
    never held together with `tensions_lock`."""
    with _reentrant_file_lock(config.locks_dir() / "syntheses.lock"):
        yield


def _read_syntheses() -> dict[str, Any]:
    """Read the file, reporting where it is malformed rather than reading it
    as empty: every writer starts here and would write the empty reading back
    over it. Only a missing file is empty."""
    loaded = storage.read_model(syntheses_path(), records.Syntheses).model_dump(exclude_unset=True)
    loaded.setdefault("syntheses", {})
    return loaded


def load_syntheses() -> dict[str, dict[str, Any]]:
    return dict(_read_syntheses()["syntheses"])


def _save_syntheses(data: dict[str, Any]) -> None:
    """Caller holds `syntheses_lock`."""
    write_json(syntheses_path(), data)


def topic_claims(topic: str, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [r for r in (rows if rows is not None else claim_rows()) if topic in (r.get("tags") or [])]


def synthesis_prompt_basis(topic: str, tags: list[dict[str, Any]] | None = None,
                           context: str | None = None) -> str:
    """What a synthesis was asked, apart from its claims and tensions.

    The prompt version, the model asked, the topic's name, its description and
    the research context — all of them decide what comes back, and none of
    them is part of what makes a synthesis stale, so without this a rename, a
    reworded description, a rewritten context or a change of model would never
    be written again.

    `context` is what the caller is putting in the prompt, so that the
    synthesis is filed under the context the model was actually given; read
    here only for a write that has no prompt behind it. A list rather than
    five lines run together, since the description and the context both hold
    newlines of their own.
    """
    tags = load_tags() if tags is None else tags
    description = next((t.get("description", "") for t in tags if t["name"] == topic), "")
    context = load_context() if context is None else context
    return json.dumps([config.PASS_VERSION, config.analysis_identity(), topic, description, context])


def synthesis_basis(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Half of what a synthesis rests on: the set of claims, what each said,
    and which paper it came from. A claim added, removed, or edited (text,
    evidence, kind) changes it, and so does a correction to the author, year
    or title — the listing names those and the model is told to cite papers by
    author and year. Reviewing a claim does not: the prompt marks unreviewed
    claims, but a review pass over a corpus does not change what any claim
    says, and staling every synthesis while it runs would leave the mark
    meaning nothing."""
    return {r["id"]: synthesis_claim_basis(r) for r in rows}


def synthesis_claim_basis(claim: dict[str, Any]) -> str:
    """One claim as a synthesis rests on it: what it says, and whose paper it
    is. The same string the tensions merge compares."""
    return claim_fingerprint(claim)


def synthesis_tensions(topic: str, tensions: list[dict[str, Any]]) -> dict[str, list[Any]]:
    """The other half: the topic's tensions as the prompt shows them, by id,
    with kind, status, whether the prompt said a claim had changed since it was
    judged, and the note. Dismissed ones are left out of the prompt, so
    dismissing one changes this as much as confirming one, or a pass finding a
    new pair, does; so does re-judging a stale one against the current text.
    The note is in because the prompt carries it: a rerun that says the same
    disagreement in different words is a different thing to write a synthesis
    from. It goes in whole rather than as a digest, because the page works the
    same comparison out for itself while a delete is held, and it cannot hash
    anything without waiting for `crypto.subtle`. `tensions` is
    `tension_rows` output, whose `topics` are already filtered to what both
    claims still carry. A record from before a field was kept holds a shorter
    list, which never compares equal to these, so it reads as stale until
    rewritten: what the model was told is not known."""
    return {t["id"]: [t.get("kind"), t.get("status"), bool(t.get("stale")),
                      t.get("note") or ""]
            for t in sorted(tensions, key=lambda t: t["id"])
            if topic in t.get("topics", []) and t.get("status") != "dismissed"}


# `before` for a `record_synthesis` call that has no snapshot to check against:
# a direct write, with no model call in between to be overtaken.
UNCHECKED = object()


def record_synthesis(topic: str, text: str, claims_by_id: dict[str, dict[str, Any]],
                     tensions: list[dict[str, Any]] | None = None, source: str = "model",
                     before: dict[str, Any] | None | object = UNCHECKED,
                     basis: str | None = None) -> dict[str, Any] | None:
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
            # The prompt it was written under, topic name and description
            # included: both are in what the model was given, and a rename or
            # a reworded description changes the question. Taken as the prompt
            # was built, not as things stand now — a description edited during
            # the call describes a question this answer was never asked. A
            # record with none of this is older than the field and counts as
            # older than the prompt.
            "prompt_basis": basis if basis is not None else synthesis_prompt_basis(topic),
            "claims": {i: synthesis_claim_basis(c) for i, c in claims_by_id.items()
                       if topic in (c.get("tags") or [])},
            "tensions": synthesis_tensions(topic, tension_rows() if tensions is None else tensions),
        }
        data["syntheses"][topic] = record
        _save_syntheses(data)
        return record


def set_synthesis_text(topic: str, text: str) -> dict[str, Any]:
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
        record: dict[str, Any] = data["syntheses"][topic]
        record.update(text=text, source="hand", written=now(),
                      prompt_basis=synthesis_prompt_basis(topic),
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


def synthesis_rows(rows: list[dict[str, Any]] | None = None,
                   tensions: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every synthesis whose topic still has claims, with `stale` set when a
    claim in the topic was added, removed, or edited since it was written, or
    a tension in it was found, confirmed, dismissed, or deleted. A stale
    synthesis is still shown: it may still be right, and the reader decides
    whether to rewrite it. One whose topic has no claims left is dropped from
    view; `_retag_all` drops it from the file when the tag goes. A record
    written before tensions were part of the basis has none on file and reads
    as stale while the topic has any: what the model saw is not known."""
    rows = rows if rows is not None else claim_rows()
    tensions = tension_rows(rows) if tensions is None else tensions
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


def synthesis_topics(rows: list[dict[str, Any]] | None = None) -> list[str]:
    """Topics worth a synthesis by default: claims from at least two papers.
    One paper's claims can be synthesized too, by naming the topic."""
    return tension_topics(rows)


# --- where the papers agree ------------------------------------------------
#
# An agreement is a group of claims from two or more papers that assert the
# same finding. Topics group claims by subject and tensions find where they
# differ; this is the case in between, and the common one: several papers
# saying the same thing, which is what "how much evidence do I have for X"
# needs counted. Found by a model pass per topic, reviewed like tensions, and
# kept in one file since each group belongs to several papers at once.

AGREEMENT_STATUSES = TENSION_STATUSES



def agreements_path() -> Path:
    return config.data_dir() / "agreements.json"


@contextlib.contextmanager
def agreements_lock() -> Iterator[None]:
    """Guard `agreements.json`. Nests inside `vocab_lock` alone, as the
    tensions lock does, and is never held with it."""
    with _reentrant_file_lock(config.locks_dir() / "agreements.lock"):
        yield


def _read_agreements() -> dict[str, Any]:
    loaded = storage.read_model(agreements_path(), records.Agreements).model_dump(exclude_unset=True)
    loaded.setdefault("seq", 0)
    loaded.setdefault("agreements", [])
    loaded.setdefault("passes", {})
    return loaded


def load_agreements() -> list[dict[str, Any]]:
    return list(_read_agreements()["agreements"])


def _save_agreements(data: dict[str, Any]) -> None:
    write_json(agreements_path(), data)


def _agreement_papers(ids: Iterable[str], live: dict[str, dict[str, Any]]) -> set[str | None]:
    return {live[i].get("paper") for i in ids if i in live}


def record_agreements(topic: str, found: list[dict[str, Any]], claims_by_id: dict[str, dict[str, Any]],
                      signature: str | None = None) -> dict[str, Any]:
    """Merge one topic's model output into the file.

    `found` is a list of `{"claims": [id, ...], "note"}`. The rules follow
    `record_tensions`, with one addition for groups, which can grow: a group
    the model returns that contains an existing one is that agreement with
    more members, and it goes back to open, since the new member is new
    evidence nobody has looked at. If it contains several existing ones,
    as when two pairs found separately turn out to be one finding, the
    earliest of them grows and the rest fold into it, topics included, so
    one finding is one card. A returned group contained in an existing
    one adds nothing and is kept. Members whose claims no longer exist are
    dropped, and a group left with claims from fewer than two papers goes.
    A dropped member's fingerprint is kept, so the reduced group reads as
    stale until someone judges it: the note and decision on file were about
    a group that no longer exists.

    Returns `{"added": n, "grown": n, "reopened": n, "kept": n}`.
    """
    with vocab_lock(), agreements_lock():
        data = _read_agreements()
        live, unreadable = _live_claims()
        existing = []
        pruned: set[str] = set()
        for record in data["agreements"]:
            if _held_back(record.get("claims", []), live, unreadable):
                existing.append(record)
                continue
            ids = [i for i in record.get("claims", []) if i in live]
            if len(_agreement_papers(ids, live)) < 2:
                continue
            record["claims"] = ids
            was = set(record.get("topics") or [])
            record["topics"] = sorted(t for t in record.get("topics", [])
                                      if all(t in (live[i].get("tags") or []) for i in ids))
            pruned |= was - set(record["topics"])
            existing.append(record)
        added = grown = reopened = kept = 0
        unattached = False
        for item in found:
            ids = sorted({i for i in item.get("claims", []) if i in claims_by_id and i in live})
            if len(_agreement_papers(ids, live)) < 2:
                continue
            note = (item.get("note") or "").strip()
            fingerprints = {i: claim_fingerprint(claims_by_id[i]) for i in ids}
            topic_live = all(topic in (live[i].get("tags") or []) for i in ids)
            wanted = set(ids)
            # The answer describes the claims as the prompt showed them. If one
            # was edited during the call, the answer is about text that no
            # longer exists, and the record on file, decided against text a
            # reviewer could see, stands: nothing grows, folds, or reopens.
            stale = any(fingerprints[i] != claim_fingerprint(live[i]) for i in ids)
            # Every record the returned group covers, in file order; the first
            # is the one that stays. Failing that, a record that covers it.
            # A record held back for an unreadable paper is not on the table:
            # its members are not all in `live`, and nothing can be decided
            # about a group whose claims cannot be seen.
            on_file = [r for r in existing if not _held_back(r["claims"], live, unreadable)]
            contained = [r for r in on_file if set(r["claims"]) <= wanted]
            current = contained[0] if contained else next(
                (r for r in on_file if wanted < set(r["claims"])), None)
            if current is not None and stale:
                kept += 1
                continue
            if current is not None:
                have = set(current["claims"])
                # The members the record will hold: the returned group when it
                # grows the record, the record's own when it is a part of it.
                members = wanted | have
                topics = {topic} | set(current.get("topics", []))
                for other in contained[1:]:
                    topics |= set(other.get("topics", []))
                    existing.remove(other)
                current["topics"] = sorted(t for t in topics
                                           if all(t in (live[i].get("tags") or []) for i in members))
                # Growing a group can drop a topic the new member does not
                # carry, which is as much a pruning as the one above.
                pruned |= topics - set(current["topics"])
                if wanted > have:
                    current["claims"] = ids
                    current["fingerprints"] = fingerprints
                    current.update(note=note or current.get("note", ""), status="open", found=now())
                    grown += 1
                    continue
                if wanted < have:
                    kept += 1   # a part of what is already on file adds nothing
                    # But the topic goes unattached where a member of the
                    # larger record does not carry it, and this answer is not
                    # on file under the topic at all. Recording the signature
                    # would skip the pass that has to run once that member
                    # goes, and nothing else would ever put the topic back.
                    unattached = unattached or topic not in (current.get("topics") or [])
                    continue
                if current.get("fingerprints") == fingerprints:
                    # As the tensions pass: the group stands, and takes the new
                    # wording only while nobody has ruled on the old. Whatever
                    # the rerun said, including nothing: the words on file were
                    # written under a prompt this answer was not given, and
                    # keeping them would put them in this answer's mouth.
                    if current.get("status") == "open":
                        current.update(note=note)
                    kept += 1
                    continue
                current.update(note=note, fingerprints=fingerprints, status="open", found=now())
                reopened += 1
                continue
            data["seq"] = int(data.get("seq") or 0) + 1
            record = {
                "id": f"a{data['seq']}",
                "claims": ids,
                "topics": [topic] if topic_live else [],
                "note": note,
                "status": "open",
                "found": now(),
                "fingerprints": fingerprints,
            }
            existing.append(record)
            added += 1
        data["agreements"] = existing
        # Every topic taken off a record on the way through, whether before the
        # answer was read or while a group grew past it: each has to be found
        # again when it comes back, so its recorded pass goes with it.
        _forget_passes(data, pruned)
        # Recorded only on the way out, only while the topic still holds the
        # claims it was asked about — see `record_tensions` — and only if
        # everything the answer said is on file under the topic.
        if signature and not unattached and _topic_unchanged(topic, claims_by_id, live):
            data.setdefault("passes", {})[topic] = signature
        _save_agreements(data)
        return {"added": added, "grown": grown, "reopened": reopened, "kept": kept}


def set_agreement_status(agreement_id: str, status: str) -> dict[str, Any]:
    """Record the reviewer's decision; as for tensions, deciding refreshes
    the fingerprints and reopening does not. A decision is about the members
    that still exist, so it also drops deleted ones from the record, as long
    as two papers remain; that is what the reviewer saw and judged."""
    if status not in AGREEMENT_STATUSES:
        raise ValueError(f"status must be one of {AGREEMENT_STATUSES}, not {status!r}")
    with agreements_lock():
        data = _read_agreements()
        record: dict[str, Any]
        for record in data["agreements"]:
            if record.get("id") == agreement_id:
                record["status"] = status
                record["decided"] = now()
                if status != "open":
                    live, unreadable = _live_claims()
                    ids = [i for i in record.get("claims", []) if i in live]
                    if not _held_back(record.get("claims", []), live, unreadable) \
                            and len(_agreement_papers(ids, live)) >= 2:
                        record["claims"] = ids
                        record["fingerprints"] = {i: claim_fingerprint(live[i]) for i in ids}
                _save_agreements(data)
                return record
    raise KeyError(agreement_id)


def delete_agreement(agreement_id: str) -> None:
    """Remove an agreement, and forget the passes that found it; see
    `delete_tension` for why."""
    with agreements_lock():
        data = _read_agreements()
        before = len(data["agreements"])
        gone = [r for r in data["agreements"] if r.get("id") == agreement_id]
        data["agreements"] = [r for r in data["agreements"] if r.get("id") != agreement_id]
        if len(data["agreements"]) == before:
            raise KeyError(agreement_id)
        _forget_passes(data, {topic for r in gone for topic in (r.get("topics") or [])})
        _save_agreements(data)


def agreement_rows(rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Every agreement still backed by claims from two papers, joined to
    those claims, with `stale`, `n_papers`, and topics filtered to what every
    member still carries. Stale when a member was edited or removed since the
    agreement was found or decided: the fingerprints name the members the
    judgment was about, so a set of them other than the live members is stale.

    `topics_on_file` is the unfiltered set the filtering starts from. A group
    can lose a member and keep going — two papers is all it needs — and the
    topics that member alone had dropped become good again for the ones left.
    The page has to work that out for itself while a delete waits out its undo,
    and it cannot recover a topic from a list this function has already taken
    it out of, so the set it filters from is sent along with the result."""
    live = {c["id"]: c for c in (rows if rows is not None else claim_rows())}
    out = []
    for record in load_agreements():
        ids = [i for i in record.get("claims", []) if i in live]
        if len(_agreement_papers(ids, live)) < 2:
            continue
        fingerprints = record.get("fingerprints") or {}
        row = dict(record)
        row["claims"] = [live[i] for i in ids]
        row["n_papers"] = len(_agreement_papers(ids, live))
        row["topics_on_file"] = sorted(record.get("topics", []))
        row["topics"] = [t for t in row["topics_on_file"]
                         if all(t in (live[i].get("tags") or []) for i in ids)]
        row["stale"] = (set(fingerprints) != set(ids)
                        or any(fingerprints[i] != claim_fingerprint(live[i]) for i in ids))
        out.append(row)
    order = {s: n for n, s in enumerate(AGREEMENT_STATUSES)}
    out.sort(key=lambda r: (order.get(r.get("status", ""), 9), -r["n_papers"], r.get("found") or ""))
    return out
