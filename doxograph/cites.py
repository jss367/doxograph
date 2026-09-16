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
# is how half the conference papers come out. A heading may carry more than
# the word: "References and Notes", "References Cited", "Bibliography (Primary
# Sources)" all start a reference list, so the word only has to begin the line.
# Plural on purpose: "referenced" and "references" part company at the eighth
# letter, so a line of prose beginning "Referenced work…" is not a heading.
_HEADINGS = ("references", "bibliography", "workscited", "literaturecited")
# What a heading may carry after the word. A short body line — "References to
# Figure 2 show…" — begins with one of the words above too, and reading the
# rest of the paper as a reference list invents citations out of its prose.
_HEADING_TAIL = ("", "cited", "andnotes", "notes", "andfurtherreading", "andbibliography",
                 "primary", "primarysources", "secondary", "secondarysources",
                 "consulted", "list")
# How long a heading may be once it is down to its letters.
_HEADING_LETTERS = 40

# How much of a title has to survive squashing before it can be looked for. A
# title of two short words — "Scaling Laws" — occurs in prose that is not a
# citation of it; twenty letters does not.
_TITLE_FLOOR = 20

_cache: dict[str, list[dict]] = {}
_cache_lock = threading.Lock()


def reference_text(text: str) -> str:
    """Everything after the first references heading, or empty when there is none.

    The first: a paper with a supplement has a bibliography for the article and
    another for the appendix, and taking only the last would drop every work
    the article itself cites. What lies between them is the appendix, and a
    title turning up in an appendix is not a false citation worth guarding
    against — over-reading here costs nothing that under-reading does not cost
    twice.
    """
    at = 0
    # `splitlines` breaks on the page separator too, so a heading at the top of
    # a page is found like any other.
    for line in text.splitlines(keepends=True):
        at += len(line)
        squashed = quotes.squash(line)
        # Measured on the letters, not the line: a small-capitals heading can
        # come out of pypdf with a space between every letter, and "R E F E R
        # E N C E S  A N D  F U R T H E R  R E A D I N G" is long as a line
        # and short as a heading.
        if len(squashed) > _HEADING_LETTERS:
            continue
        if _is_heading(squashed):
            return text[at:]
    return ""


def _is_heading(squashed: str) -> bool:
    """Whether a line's letters say a reference list starts here.

    A section number comes off first, in either notation: IEEE numbers its
    sections in Roman, so a bibliography can open with "VI. REFERENCES" and
    squash to "vireferences". The Roman run is only taken off when what is
    left is a heading, since "literaturecited" starts with one of its letters.
    """
    for candidate in (squashed.lstrip("0123456789"), squashed.lstrip("ivxlcdm0123456789")):
        head = next((h for h in _HEADINGS if candidate.startswith(h)), None)
        if head is not None and candidate[len(head):] in _HEADING_TAIL:
            return True
    return False


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
        for other in _cited(paper["key"], listing, marks):
            found.append({"from": paper["key"], "to": other})
    found.sort(key=lambda edge: (edge["from"], edge["to"]))
    with _cache_lock:
        _cache.clear()      # one corpus at a time is all the map ever asks for
        _cache[signature] = found
    return found


def _cited(key: str, listing: str, marks: dict[str, list[str]]) -> list[str]:
    """Which papers this reference list names, longest title first.

    One title can sit inside another — "Attention is all you need" inside
    "Attention is all you need for image restoration" — and a reference to the
    longer would otherwise be read as a citation of both. Where two marks land
    on the same stretch of the list, only the longer is a citation; the shorter
    is part of it.
    """
    # Every mark a paper is named by, not the first that hits: an entry can
    # carry the arXiv id and the title both, and it is the title's span that
    # covers a shorter title printed inside it.
    hits: list[tuple[int, str, list[int]]] = []
    named: dict[str, bool] = {}
    for other, found in marks.items():
        if other == key:
            continue
        for at, mark in enumerate(found):
            places = _occurrences(listing, mark)
            if places:
                hits.append((len(mark), other, places))
                # An arXiv id or a DOI, rather than a title: `fingerprints`
                # puts the identifiers first and the title last.
                named[other] = named.get(other, False) or at < len(found) - 1
    hits = [hit for hit in hits if _identified(hit[1], named, marks, listing)]
    # Longest first, so a shorter mark inside one already taken is dropped —
    # but only where every mention of it is inside one, and against every
    # place the longer one was printed: a bibliography can name the same paper
    # in the article's list and again in the supplement's. A list that cites
    # both papers names the shorter one somewhere on its own.
    taken: list[tuple[int, int]] = []
    cited: set[str] = set()
    for width, other, places in sorted(hits, key=lambda hit: -hit[0]):
        # Strictly wider: a mark swallows a shorter one printed inside it, but
        # two papers whose titles are the same word for word — a preprint and
        # its published version — do not swallow each other. Which of those is
        # cited is settled by `_identified`, on the identifiers.
        covered = all(any(width < end - begin and begin <= at and at + width <= end
                          for begin, end in taken)
                      for at in places)
        # A paper already cited keeps contributing its spans — one edge per
        # paper, whichever of its marks the list happens to carry.
        if not covered or other in cited:
            cited.add(other)
            taken.extend((at, at + width) for at in places)
    return sorted(cited)


def _identified(key: str, named: dict[str, bool], marks: dict[str, list[str]],
                listing: str) -> bool:
    """Whether a paper survives a twin with the same title.

    A preprint and its published version carry one title and two identifiers.
    A reference to one of them names the title both share and only that one's
    DOI, so the title alone cannot say which is cited — but the identifier
    can, and a paper named by one takes the citation from a twin named by
    nothing but the title they have in common.
    """
    title = marks[key][-1] if marks.get(key) else ""
    if named.get(key, False) or not title:
        return True
    twins = [other for other, found in marks.items()
             if other != key and found and found[-1] == title and named.get(other, False)]
    if not twins:
        return True
    # One printing of the title, one citation: the identified twin has it.
    # Several printings mean several entries, and a bibliography that lists
    # the preprint and the published version names them both.
    return len(_occurrences(listing, title)) > len(twins)


def _occurrences(listing: str, mark: str, cap: int = 20) -> list[int]:
    """Where a mark falls in a reference list, up to `cap` places."""
    places = []
    at = listing.find(mark)
    while at >= 0 and len(places) < cap:
        places.append(at)
        at = listing.find(mark, at + 1)
    return places


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
