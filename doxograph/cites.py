"""Which papers in the corpus cite which, read out of their reference lists.

Two papers sharing a topic tag is a weak link: it says they are about the same
thing, which is often just that you filed them together. One paper citing
another is a strong one, and it is already written down at the back of the PDF.

The reference list is the text after the last "References" heading. A paper in
the corpus is cited by another when its arXiv id, its DOI, or its title turns
up in there. Nothing is fetched: this reads the text extracted for the quote
checks and the titles already on the papers, so it works offline and on a
paper no model has read.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading

from . import config, quotes, search, store

# A line that says the references start. Matched on its letters alone, which
# takes care of a section number in front of it, a colon after it, the case,
# and the spaces pypdf leaves inside a small-capitals heading — "R EFERENCES"
# is how half the conference papers come out.
_HEADINGS = {"references", "bibliography", "workscited", "literaturecited"}
_HEADING_LINE = 40

# How much of a title has to survive squashing before it can be looked for. A
# title of two short words — "Scaling Laws" — occurs in prose that is not a
# citation of it; twenty letters does not.
_TITLE_FLOOR = 20

_cache: dict[str, list[dict]] = {}
_cache_lock = threading.Lock()


def reference_text(text: str) -> str:
    """Everything after the last references heading, or empty when there is none.

    The last one: a paper can mention its references in the text, and what is
    wanted is the list itself. Appendices after it are kept — a title turning
    up in an appendix is not a false citation worth guarding against.
    """
    last = None
    at = 0
    # `splitlines` breaks on the page separator too, so a heading at the top of
    # a page is found like any other.
    for line in text.splitlines(keepends=True):
        at += len(line)
        if len(line.strip()) > _HEADING_LINE:
            continue
        if quotes.squash(line).lstrip("0123456789") in _HEADINGS:
            last = at
    return text[last:] if last is not None else ""


def fingerprints(paper: dict) -> list[str]:
    """What to look for in a reference list to find this paper named.

    An arXiv id and a DOI are near enough unique on their own. A title is not,
    strictly, but a twenty-character run of one paper's title inside another's
    bibliography is a citation and not a coincidence.
    """
    marks = []
    source = paper.get("source") or {}
    if source.get("kind") == "arxiv" and source.get("id"):
        marks.append(quotes.squash(re.sub(r"v\d+$", "", str(source["id"]), flags=re.I)))
    for value in (paper.get("doi"), source.get("id") if source.get("kind") == "doi" else ""):
        if value:
            marks.append(quotes.squash(str(value)))
    title = quotes.squash(paper.get("title") or "")
    if len(title) >= _TITLE_FLOOR:
        marks.append(title)
    return [mark for mark in marks if mark]


def edges(papers: list[dict] | None = None) -> list[dict]:
    """Every `{"from": key, "to": key}` where one paper's references name another.

    The answer is cached against the corpus and the extracted text, both of
    which it is read from, so the map can ask for it on every opening.
    """
    papers = store.all_papers() if papers is None else papers
    signature = f"{store.corpus_signature()}:{_text_signature()}"
    with _cache_lock:
        hit = _cache.get(signature)
    if hit is not None:
        return hit
    marks = {paper["key"]: fingerprints(paper) for paper in papers}
    found = []
    for paper in papers:
        text = search.paper_text(paper["key"])
        if not text:
            continue
        listing = quotes.squash(reference_text(text))
        if not listing:
            continue
        for other in papers:
            if other["key"] == paper["key"]:
                continue
            if any(mark in listing for mark in marks[other["key"]]):
                found.append({"from": paper["key"], "to": other["key"]})
    found.sort(key=lambda edge: (edge["from"], edge["to"]))
    with _cache_lock:
        _cache.clear()      # one corpus at a time is all the map ever asks for
        _cache[signature] = found
    return found


def _text_signature() -> str:
    """The extracted text as it stands. A PDF arriving for a paper already in
    the corpus changes this and nothing else, so the corpus signature alone
    would go on serving the citations worked out before it."""
    parts = []
    try:
        with os.scandir(config.text_dir()) as entries:
            for entry in entries:
                if entry.name.endswith(".txt"):
                    st = entry.stat()
                    parts.append((entry.name, st.st_size, st.st_mtime_ns))
    except FileNotFoundError:
        pass
    parts.sort()
    return hashlib.sha1(repr(parts).encode()).hexdigest()
