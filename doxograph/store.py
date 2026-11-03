"""JSON-on-disk store.

One file per paper under `papers/`, holding the paper's metadata and its
claims. Keeping claims inside the paper file means provenance stays local and
every change shows up as a readable diff. The cross-paper views (by topic, by
ledger claim) are computed at read time rather than stored.

This module is the store's one public surface. It holds the core the rest
stands on — locks, keys, paths, atomic writes, and reading and writing a
paper — and re-exports everything from the `store_*` modules, so a caller
writes `store.add_claim` whichever file it lives in.

Those modules reach the core and each other through this module object
(`store.save_paper`, `store._save_tensions`), never by importing a name from
a sibling. A test that patches `store.save_paper` therefore still catches
every write a facet rewrite or a topic rename makes, as it did when the store
was one file. A call within one module is a plain call that patching here
does not reach; a test that means to intercept one patches the module that
defines it.
"""

from __future__ import annotations

import contextlib
import functools
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
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Concatenate

from pydantic import ValidationError

from . import code_links, config, records, storage, workspace_metadata

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
    # A paper added before code links has none recorded, and its abstract is
    # what an import now reads them from. A list the reader has emptied is
    # left empty: only a missing field is filled.
    if "code" not in paper:
        paper["code"] = code_links.find(paper.get("abstract") or "")
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
        "publication_date": None,
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
        # Where the paper's code is, as web addresses: found in its abstract
        # (and arXiv comment) when it is added, and edited by hand after that.
        "code": [],
        "claims": [],
        # What the reader writes about the paper: no model reads it and no pass
        # writes it. `error` is the other half of what `notes` used to be —
        # why the ingest has no PDF — kept apart so a note means one thing.
        "notes": "",
        "error": "",
    }
    paper.update(fields)
    if "code" not in fields:
        paper["code"] = code_links.find(str(paper["abstract"] or ""))
    return paper


# --- the rest of the store ------------------------------------------------
#
# Re-exported so that every caller, and every test that patches a name here,
# sees one namespace, as when this was one file. Imported last: those modules
# import this one back and use the core at import time (`_locked`), so it has
# to exist before they run. Import this module before any of them: importing
# one of them first is a circular import that fails.

from .store_claims import (
    ensure_claim_seq as ensure_claim_seq,
    next_claim_id as next_claim_id,
    new_claim as new_claim,
    check_quote as check_quote,
    quote_context as quote_context,
    verify_quotes as verify_quotes,
    clean_ledger_links as clean_ledger_links,
    CLAIM_FIELDS as CLAIM_FIELDS,
    TopicConflictError as TopicConflictError,
    EditConflictError as EditConflictError,
    paper_revision as paper_revision,
    check_paper_revision as check_paper_revision,
    _claim_edit_lock as _claim_edit_lock,
    update_claim as update_claim,
    add_claim as add_claim,
    review_claims as review_claims,
    delete_claim as delete_claim,
    refresh_status as refresh_status,
)
from .store_vocab import (
    DEFAULT_TAGS as DEFAULT_TAGS,
    _vocabulary as _vocabulary,
    load_tags as load_tags,
    _retired_topics as _retired_topics,
    save_tags as save_tags,
    tag_names as tag_names,
    add_tag as add_tag,
    StorageRecoveryError as StorageRecoveryError,
    tag_change_path as tag_change_path,
    _recover_tag_change as _recover_tag_change,
    recover_storage as recover_storage,
    _change_tag as _change_tag,
    rename_tag as rename_tag,
    _move_pass as _move_pass,
    _retag_all as _retag_all,
    delete_tag as delete_tag,
)
from .store_ledger import (
    context_path as context_path,
    load_context as load_context,
    save_context as save_context,
    _ledger as _ledger,
    load_ledger as load_ledger,
    save_ledger as save_ledger,
)
from .store_views import (
    corpus_signature as corpus_signature,
    claim_rows as claim_rows,
    label_key as label_key,
    label_keys as label_keys,
    has_label as has_label,
    normalize_labels as normalize_labels,
    set_labels as set_labels,
    label_counts as label_counts,
    tag_counts as tag_counts,
    needs_extraction as needs_extraction,
    summarize as summarize,
)
from .store_facets import (
    facet_change_path as facet_change_path,
    facets_lock as facets_lock,
    FACET_CHANGES as FACET_CHANGES,
    _recover_facet_change as _recover_facet_change,
    _change_facets as _change_facets,
    _facets_file as _facets_file,
    load_facets as load_facets,
    _save_facets as _save_facets,
    facet_basis as facet_basis,
    facet_value_key as facet_value_key,
    facet_spellings as facet_spellings,
    FACET_SPACE as FACET_SPACE,
    facet_value_text as facet_value_text,
    facet_value_keys as facet_value_keys,
    facet_aliases as facet_aliases,
    resolve_facet_value as resolve_facet_value,
    clean_facet_values as clean_facet_values,
    clean_claim_facets as clean_claim_facets,
    set_claim_facets_by_hand as set_claim_facets_by_hand,
    claim_needs_facet as claim_needs_facet,
    mark_facets_filled as mark_facets_filled,
    stale_facets as stale_facets,
    reconcile_claim_facets as reconcile_claim_facets,
    facet_counts as facet_counts,
    stale_facet_papers as stale_facet_papers,
    add_facet as add_facet,
    describe_facet as describe_facet,
    _rewrite_papers as _rewrite_papers,
    delete_facet as delete_facet,
    _strip_facet as _strip_facet,
    _rewrite_values as _rewrite_values,
    rewrite_facet_values as rewrite_facet_values,
)
from .store_tensions import (
    TENSION_KINDS as TENSION_KINDS,
    TENSION_STATUSES as TENSION_STATUSES,
    tensions_path as tensions_path,
    tensions_lock as tensions_lock,
    _read_tensions as _read_tensions,
    load_tensions as load_tensions,
    _save_tensions as _save_tensions,
    _pair as _pair,
    _live_claims as _live_claims,
    _held_back as _held_back,
    _shared_topics as _shared_topics,
    claim_fingerprint as claim_fingerprint,
    cite_head as cite_head,
    _forget_passes as _forget_passes,
    _topic_unchanged as _topic_unchanged,
    pass_signature as pass_signature,
    tension_pass as tension_pass,
    record_tensions as record_tensions,
    set_tension_status as set_tension_status,
    delete_tension as delete_tension,
    tension_rows as tension_rows,
    tension_topics as tension_topics,
)
from .store_syntheses import (
    SYNTHESIS_SOURCES as SYNTHESIS_SOURCES,
    syntheses_path as syntheses_path,
    syntheses_lock as syntheses_lock,
    _read_syntheses as _read_syntheses,
    load_syntheses as load_syntheses,
    _save_syntheses as _save_syntheses,
    topic_claims as topic_claims,
    synthesis_prompt_basis as synthesis_prompt_basis,
    synthesis_basis as synthesis_basis,
    synthesis_claim_basis as synthesis_claim_basis,
    synthesis_tensions as synthesis_tensions,
    UNCHECKED as UNCHECKED,
    record_synthesis as record_synthesis,
    set_synthesis_text as set_synthesis_text,
    delete_synthesis as delete_synthesis,
    synthesis_rows as synthesis_rows,
    synthesis_topics as synthesis_topics,
)
from .store_agreements import (
    AGREEMENT_STATUSES as AGREEMENT_STATUSES,
    agreements_path as agreements_path,
    agreements_lock as agreements_lock,
    _read_agreements as _read_agreements,
    load_agreements as load_agreements,
    _save_agreements as _save_agreements,
    _agreement_papers as _agreement_papers,
    agreement_pass as agreement_pass,
    record_agreements as record_agreements,
    set_agreement_status as set_agreement_status,
    delete_agreement as delete_agreement,
    agreement_rows as agreement_rows,
)
