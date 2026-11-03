"""Views computed across papers at read time, and one paper's listing.

Part of the store: import it through `doxograph.store`, which re-exports
every name here and says why the rest of the store is reached as
`store.X` rather than imported.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from . import config, store


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
    for path in (config.tags_path(), config.facets_path(), store.facet_change_path(), config.ledger_path(),
                 store.tensions_path(), store.syntheses_path(),
                 store.agreements_path(), store.context_path(), config.data_dir() / "notebook.json",
                 store.tag_change_path(), store.retired_keys_path()):
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
    for paper in papers if papers is not None else store.all_papers():
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
    with store.paper_lock(key):
        paper = store.load_paper(key)
        paper["labels"] = normalize_labels(labels)
        store.save_paper(paper)
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

def needs_extraction(key: str) -> bool:
    """A paper with its PDF but no claims is ready to be read.

    Callers used to gate on "was this paper just created", which skipped a paper
    whose PDF arrived later — after a failed download was retried, for instance.
    """
    try:
        paper = store.load_paper(key)
    except store.VANISHED:
        return False
    return store.pdf_path(key).exists() and not paper.get("claims")


def summarize(paper: dict[str, Any]) -> dict[str, Any]:
    claims = paper.get("claims", [])
    return {
        "key": paper["key"],
        "title": paper.get("title", ""),
        "authors": paper.get("authors") or [],
        "year": paper.get("year"),
        "publication_date": paper.get("publication_date"),
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
        "has_pdf": store.pdf_path(paper["key"]).exists(),
        # What `refresh_status` reads when a paper has no claims: read once
        # rather than inferred from `schema_version`, which a paper extracted
        # before that field existed does not carry. The page needs it to work
        # out the status of a paper whose last claim it is holding.
        "has_extraction": bool(paper.get("extraction")),
        "error": paper.get("error", ""),
    }
