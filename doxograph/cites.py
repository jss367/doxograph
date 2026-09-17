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
# Longest first: "references" has to be tried before "reference", or the
# plural would be read as the singular with an "s" left over. A paper with one
# item in its bibliography does label it "Reference", and the tail check keeps
# "Referenced work…" out either way.
_HEADINGS = ("references", "reference", "bibliography", "workscited", "literaturecited")
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


# Where a reference list numbers its entries. A bibliography that does not is
# one entry as far as this is concerned, which is where this started.
_ENTRY_MARK = re.compile(
    r"(?m)^[ \t]*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])(?=[ \t]|$)")


def entries(listing: str) -> list[str]:
    """A reference list cut into its entries, each squashed.

    An entry names one work, and knowing where one ends is what tells a
    citation of "Attention is all you need" from a citation of a paper whose
    title contains it. Only a numbered list says where its entries are; an
    author-year one comes back whole, and the rules fall back to reading the
    list as one entry, which is where they started.
    """
    if not listing.strip():
        return []
    parts = [part for part in _ENTRY_MARK.split(listing) if part.strip()]
    cut = [squashed for part in parts if (squashed := quotes.squash(part))]
    return cut if len(cut) > 1 else [quotes.squash(listing)]


# What marks a letter or numeral as a section label rather than the first word
# of a sentence: the dot or bracket after it. "A. REFERENCES" is a heading and
# "A reference" is the start of a line of prose.
_LABEL = re.compile(r"^[ \t]*[0-9A-Za-z]{1,7}[.)\]]+[ \t]+")


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
        if _is_heading(squashed, labelled=bool(_LABEL.match(line))):
            return text[at:]
    return ""


def _is_heading(squashed: str, labelled: bool = False) -> bool:
    """Whether a line's letters say a reference list starts here.

    A section number comes off first, in either notation: IEEE numbers its
    sections in Roman, so a bibliography can open with "VI. REFERENCES" and
    squash to "vireferences". The Roman run is only taken off when what is
    left is a heading, since "literaturecited" starts with one of its letters.
    """
    plain = squashed.lstrip("0123456789")
    # A section label comes off: Arabic above, a single letter — "A.
    # REFERENCES" — or a Roman numeral. Only those, and only as far as a
    # heading: any alphabetic prefix would take the "see" off "see references"
    # and read the rest of the paper as a bibliography.
    candidates = [plain]
    # A label only comes off where the line wrote one: a letter or a numeral
    # followed by a dot or a bracket. Without that, the first letters are the
    # first letters of a word — "A reference" is not "A. REFERENCES".
    if labelled:
        if len(plain) > 1 and plain[0].isalpha():
            candidates.append(plain[1:])
        candidates += [plain[at:] for at in range(2, 8)
                       if at < len(plain) and set(plain[:at]) <= set("ivxlcdm")]
    for candidate in candidates:
        # Every heading it could begin with, not the first: "references" and
        # "reference" both fit the plural, and only one of them leaves a tail
        # the check accepts.
        if any(candidate.startswith(head) and candidate[len(head):] in _HEADING_TAIL
               for head in _HEADINGS):
            return True
    return False


def fingerprints(paper: dict) -> list[str]:
    """What to look for in a reference list to find this paper named.

    An arXiv id and a DOI are near enough unique on their own. A title is not,
    strictly, but a twenty-character run of one paper's title inside another's
    bibliography is a citation and not a coincidence.
    """
    marks: list[str] = []
    source = paper.get("source") or {}
    if source.get("kind") == "arxiv" and source.get("id"):
        marks.append(quotes.squash(re.sub(r"v\d+$", "", str(source["id"]), flags=re.I)))
    for value in (paper.get("doi"), source.get("id") if source.get("kind") == "doi" else ""):
        if value:
            marks.append(quotes.squash(str(value)))
    title = quotes.squash(paper.get("title") or "")
    if len(title) >= _TITLE_FLOOR:
        marks.append(title)
    # Crossref fills in `doi` and `source["id"]` alike, and one identifier
    # recorded twice is still one identifier.
    return list(dict.fromkeys(mark for mark in marks if mark))


def edges(papers: list[dict] | None = None) -> list[dict]:
    """Every `{"from": key, "to": key}` where one paper's references name another.

    The answer is cached against the corpus and the extracted text, both of
    which it is read from, so the map can ask for it on every opening.
    """
    given = papers is not None
    signature = f"{store.corpus_signature()}:{_text_signature()}"
    with _cache_lock:
        hit = _cache.get(signature)
    if hit is not None:
        return hit
    papers = papers if given else store.all_papers()
    marks = {paper["key"]: fingerprints(paper) for paper in papers}
    found = []
    for paper in papers:
        text = search.paper_text(paper["key"])
        if not text:
            continue
        listing = entries(reference_text(text))
        if not listing:
            continue
        for other in _cited(paper["key"], listing, marks):
            found.append({"from": paper["key"], "to": other})
    found.sort(key=lambda edge: (edge["from"], edge["to"]))
    # Only if the corpus stood still while it was being read. A paper added or
    # removed in the middle leaves this describing neither the corpus before
    # nor the one after, and storing it under the new signature would serve
    # that to every later asking.
    if given or signature == f"{store.corpus_signature()}:{_text_signature()}":
        with _cache_lock:
            _cache.clear()  # one corpus at a time is all the map ever asks for
            _cache[signature] = found
    return found


def _cited(key: str, entries: list[str], marks: dict[str, list[str]]) -> list[str]:
    """Which papers a reference list names, one entry at a time.

    An entry names one work. Within it the longest mark wins, so a title
    printed inside a longer title — "Attention is all you need" inside
    "Attention is all you need for image restoration" — is part of that
    citation rather than another one; and an identifier beats a title, so a
    preprint and its published version are told apart by the DOI or the arXiv
    id the entry carries. Where nothing separates two papers, both are cited:
    the entry does not say which, and neither do we.
    """
    cited: set[str] = set()
    for entry in entries:
        cited |= _cited_in(key, entry, marks)
    return sorted(cited)


def _cited_in(key: str, entry: str, marks: dict[str, list[str]]) -> set[str]:
    """The papers one stretch of a reference list names.

    A paper claims the places its title is printed, or — where the entry names
    it by an identifier and not by name — the places that identifier is. A
    claim sitting strictly inside another paper's is part of that citation
    rather than a second one: "Attention is all you need" inside "Attention is
    all you need for image restoration". Claims on the same place are twins,
    told apart by an identifier if the entry carries one. Everything else is a
    separate citation, which is what keeps an unnumbered bibliography from
    coming back as a single work.
    """
    claims: dict[str, tuple[list[int], int, int]] = {}
    fallbacks: dict[str, tuple[list[int], int]] = {}
    for other, found in marks.items():
        if other == key:
            continue
        # `fingerprints` puts a paper's identifiers first and its title last.
        title = found[-1] if found else ""
        by_identifier = next((mark for mark in found[:-1] if mark in entry), "")
        named_by = title if title and title in entry else by_identifier
        if not named_by:
            continue
        claims[other] = (_occurrences(entry, named_by), len(named_by), bool(by_identifier))
        if by_identifier and named_by is not by_identifier:
            # Where every printing of its title turns out to be inside
            # somebody else's, the identifier is what it is cited by: an entry
            # naming this paper by its DOI alone is a citation of it, however
            # its title reads elsewhere in the list.
            fallbacks[other] = (_occurrences(entry, by_identifier), len(by_identifier))

    def swallowed(other: str, at: int, width: int) -> bool:
        """Whether a claim at `at` lies inside a longer claim of somebody else's."""
        return any(w > width and any(begin <= at and at + width <= begin + w for begin in wheres)
                   for name, (wheres, w, _) in claims.items() if name != other)

    # A paper whose every title printing is inside somebody else's title is
    # still cited where the entry names it by an identifier.
    for other, (places, width) in fallbacks.items():
        if all(swallowed(other, at, claims[other][1]) for at in claims[other][0]):
            claims[other] = (places, width, 1)

    cited: set[str] = set()
    twins: dict[tuple[int, int], list[str]] = {}
    for other, (places, width, _) in claims.items():
        for at in places:
            # Inside a longer claim of somebody else's: part of that citation.
            if swallowed(other, at, width):
                continue
            twins.setdefault((at, width), []).append(other)
    for named_by in twins.values():
        best = max(claims[other][2] for other in named_by)
        # An identifier says which of two same-titled papers an entry means —
        # unless this stretch is a whole bibliography that could not be cut
        # into entries, where the identifier sits beside one printing of the
        # title and we cannot see which. As many printings as claimants means
        # each of them has one, and all are cited.
        printings = min(len(claims[other][0]) for other in named_by)
        spoken_for = sum(1 for other in named_by if claims[other][2] == best)
        if printings > spoken_for:
            # More printings of the title than there are papers an identifier
            # accounts for: the rest cite one of the others, and nothing here
            # says which, so none of them is dropped.
            cited |= set(named_by)
        else:
            cited |= {other for other in named_by if claims[other][2] == best}
    return cited


def _occurrences(entry: str, mark: str, cap: int = 20) -> list[int]:
    """Where a mark falls in a stretch of a reference list, up to `cap` places."""
    places = []
    at = entry.find(mark)
    while at >= 0 and len(places) < cap:
        places.append(at)
        at = entry.find(mark, at + 1)
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
