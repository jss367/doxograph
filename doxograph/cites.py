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
# The words that name a bibliography. A heading has to say one of them, or
# "Suggested Reading" would be a heading and so would half a paper's sections.
_HEADING_ANCHORS = frozenset({"references", "reference", "bibliography",
                              "works", "literature"})
# What a heading may carry after the word, as words rather than as phrases: a
# heading is built out of these — "References and Notes", "References and
# Recommended Reading", "Selected Bibliography (Primary Sources)" — and
# listing the phrases meant meeting each new one for the first time. A short
# body line beginning "References to Figure 2 show…" is refused because "to"
# and "figure" are not among them, which is the work this does.
_HEADING_WORDS = frozenset({
    "and", "cited", "notes", "further", "reading", "recommended", "selected",
    "additional", "primary", "secondary", "sources", "consulted", "list",
    "works", "literature", "bibliography", "references", "reference", "key",
})
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
    r"(?m)(?:^|(?<=\f))[ \t]*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])(?=[ \t\f]|$)")


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
    found: list[tuple[int, bool]] = []
    # `splitlines` breaks on the page separator too, so a heading at the top of
    # a page is found like any other.
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        at += len(line)
        squashed = quotes.squash(line)
        # Measured on the letters, not the line: a small-capitals heading can
        # come out of pypdf with a space between every letter, and "R E F E R
        # E N C E S  A N D  F U R T H E R  R E A D I N G" is long as a line
        # and short as a heading.
        if len(squashed) > _HEADING_LETTERS:
            continue
        if _is_heading(squashed, labelled=bool(_LABEL.match(line))):
            found.append((at, _points_at_a_page(lines, i + 1)))
    # A contents page writes the word too, and the list it points at is further
    # down. Passed over only when there is another heading to pass to: a
    # bibliography whose first entry is a bare number on a line of its own
    # looks the same from here, and skipping the only heading a paper has
    # would drop every work it cites.
    for start, contents in found:
        if not contents:
            return text[start:]
    return text[found[0][0]:] if found else ""


# A page number a contents entry points at: digits alone, after however many
# dot leaders the extraction put on the line. `[1]` and `1.` are not this —
# they are how a reference list numbers its first entry, and the brackets and
# the dot are what say so.
_PAGE_NUMBER = re.compile(r"^[ 	.·•…‐-―]*\d{1,4}[ 	]*$")


def _points_at_a_page(lines: list[str], start: int) -> bool:
    """Whether the lines after a heading are a page number and nothing else."""
    for line in lines[start:start + 3]:
        if not line.strip():
            continue
        return bool(_PAGE_NUMBER.match(line.rstrip("\n\r\f")))
    return False


def _is_heading(squashed: str, labelled: bool = False) -> bool:
    """Whether a line's letters say a reference list starts here.

    A heading is built out of heading words and says one of the words that
    names a bibliography: "References", "References and Notes", "Selected
    Bibliography", "Works Cited". Anything the words do not account for is
    prose — "References to Figure 2 show…" stops at "to" — which is the work
    this does, since a body line can begin with the word a heading does.

    A section label comes off first where the line wrote one; see `_LABEL`.
    """
    plain = squashed.lstrip("0123456789")
    candidates = [plain]
    if labelled:
        if len(plain) > 1 and plain[0].isalpha():
            candidates.append(plain[1:])
        candidates += [plain[at:] for at in range(2, 8)
                       if at < len(plain) and set(plain[:at]) <= set("ivxlcdm")]
    return any(_reads_as_heading(candidate) for candidate in candidates)


def _reads_as_heading(letters: str) -> bool:
    """Whether a run of letters is heading words and nothing else, one of them
    naming a bibliography. Longest word first, or "references" reads as
    "reference" and leaves an s nothing accounts for."""
    order = sorted(_HEADING_WORDS, key=len, reverse=True)
    said = []
    while letters:
        word = next((w for w in order if letters.startswith(w)), None)
        if word is None:
            return False
        said.append(word)
        letters = letters[len(word):]
    return bool(said) and any(word in _HEADING_ANCHORS for word in said)


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
    papers = papers if given else store.all_papers()
    named = _named_signature(papers)
    with _cache_lock:
        hit = _cache.get(f"{named}:{_text_signature()}")
    if hit is not None:
        return hit
    marks = {paper["key"]: fingerprints(paper) for paper in papers}
    read: dict[str, tuple] = {}
    found = []
    for paper in papers:
        text = search.paper_text(paper["key"])
        # The state each paper's text was in when it was read. A PDF arriving
        # for a paper this scan has already passed changes its text and not
        # its name, and the answer would be stored as though it had seen it.
        read[paper["key"]] = _text_identity(paper["key"])
        if not text:
            continue
        references = reference_text(text)
        listing = entries(references)
        if not listing:
            continue
        # A list that numbers nothing is one long stretch, and a paper named
        # by an identifier there has no entry to tie it to a printing of a
        # title somebody else may have cited.
        uncut = not _ENTRY_MARK.search(references)
        for other in _cited(paper["key"], listing, marks, uncut):
            found.append({"from": paper["key"], "to": other})
    found.sort(key=lambda edge: (edge["from"], edge["to"]))
    # Stored only if what it was read from is still what is there: the papers
    # unchanged, and every paper's text as it was when this scan read it.
    # Reading a paper for the first time is what writes its text down, so the
    # comparison is against what this scan saw rather than what was on disk
    # when it started.
    steady = all(_text_identity(key) == was for key, was in read.items())
    if given or (steady and named == _named_signature(store.all_papers())):
        with _cache_lock:
            _cache.clear()  # one corpus at a time is all the map ever asks for
            _cache[f"{named}:{_text_signature()}"] = found
    return found


def _cited(key: str, entries: list[str], marks: dict[str, list[str]],
           uncut: bool = False) -> list[str]:
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
        cited |= _cited_in(key, entry, marks, uncut)
    return sorted(cited)


def _cited_in(key: str, entry: str, marks: dict[str, list[str]],
              uncut: bool = False) -> set[str]:
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
    covers: dict[str, list[tuple[int, int]]] = {}
    fallbacks: dict[str, tuple[list[int], int]] = {}
    for other, found in marks.items():
        if other == key:
            continue
        # `fingerprints` puts a paper's identifiers first and its title last.
        title = found[-1] if found else ""
        by_identifier = next((mark for mark in found[:-1] if mark in entry), "")
        # Where the list is one stretch, an identifier is the only thing that
        # points at this paper and nobody else's: a title printed somewhere in
        # a page of references may be somebody's citation of its twin.
        named_by = (by_identifier if uncut and by_identifier
                    else (title if title and title in entry else by_identifier))
        if not named_by:
            continue
        claims[other] = (_occurrences(entry, named_by), len(named_by), bool(by_identifier))
        # Everywhere this paper is named, whatever it is cited by here. An
        # identifier settles which paper an entry means; it does not stop the
        # paper's title covering a shorter title printed inside it.
        for mark in found:
            covers.setdefault(other, []).extend(
                (at, len(mark)) for at in _occurrences(entry, mark))
        if by_identifier and named_by is not by_identifier:
            # Where every printing of its title turns out to be inside
            # somebody else's, the identifier is what it is cited by: an entry
            # naming this paper by its DOI alone is a citation of it, however
            # its title reads elsewhere in the list.
            fallbacks[other] = (_occurrences(entry, by_identifier), len(by_identifier))

    def swallowed(other: str, at: int, width: int) -> bool:
        """Whether a claim at `at` lies inside a longer name of somebody else's."""
        return any(w > width and begin <= at and at + width <= begin + w
                   for name, spans in covers.items() if name != other
                   for begin, w in spans)

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


def _named_signature(papers: list[dict]) -> str:
    """The names every paper can be cited by, and nothing else.

    Half of what the citations are read from; `_text_signature` is the other
    half. Not the corpus signature: that moves when a claim is edited or a
    topic renamed, neither of which changes a bibliography, and every such
    move would throw the answer away and read the pile again.
    """
    named = "\n".join(f"{paper['key']} {' '.join(fingerprints(paper))}"
                       for paper in sorted(papers, key=lambda p: p["key"]))
    return hashlib.sha1(named.encode("utf-8")).hexdigest()


def _text_identity(key: str) -> tuple:
    """A paper's stored text as it stands, or nothing if there is none."""
    try:
        st = store.text_path(key).stat()
    except OSError:
        return ()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


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
