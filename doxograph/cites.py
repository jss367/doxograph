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
import re
import threading
import unicodedata
from dataclasses import dataclass

from . import quotes, search, store

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
_HEADING_ANCHORS = frozenset({"references", "reference", "bibliography"})
# And the words that name one only in company. A section headed "Literature"
# is as often the paper's own reading of the field, and one headed "Works" an
# artist's output; "Literature Cited" and "Works Cited" are the forms that say
# a bibliography follows. Taken alone they would make a heading of the review
# section a paper puts before its references, and everything after it — the
# body included — would be read as the list.
_CITED_ANCHORS = frozenset({"works", "literature"})
# What a heading may carry after the word, as words rather than as phrases: a
# heading is built out of these — "References and Notes", "List of References",
# "Selected Bibliography (Primary Sources)" — and
# listing the phrases meant meeting each new one for the first time. A short
# body line beginning "References to Figure 2 show…" is refused because "to"
# and "figure" are not among them, which is the work this does.
_HEADING_WORDS = frozenset({
    "and", "of", "cited", "notes", "further", "reading", "recommended",
    "selected", "additional", "primary", "secondary", "sources", "consulted",
    "list", "works", "literature", "bibliography", "references", "reference",
    "key",
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
#
# A number with nothing else on its line counts as well: a two-column
# extraction puts the marker of a list written `1. Vaswani…` on a line of its
# own, and read as one stretch a list like that lets an entry's identifier
# settle a title printed in somebody else's entry.
_ENTRY_MARK = re.compile(
    r"(?m)(?:^|(?<=\f))[ \t]*"
    # A bracket closes a marker itself, so the entry may start against it.
    r"(?:\[\d{1,3}\]|\(\d{1,3}\)"
    # A dot or a bracket after the number does too, as long as what follows is
    # not another digit: `1.Smith` is an entry and `3.5` is a section number.
    r"|\d{1,3}[.)](?![0-9])"
    # A number with nothing to close it has to have nothing after it either.
    r"|\d{1,3}(?=[ \t]*$))")


def entries(listing: str) -> list[quotes.Text]:
    """A reference list cut into its entries, each squashed beside its own raw
    text: an identifier is read against the raw, where its punctuation is.

    An entry names one work, and knowing where one ends is what tells a
    citation of "Attention is all you need" from a citation of a paper whose
    title contains it. Only a numbered list says where its entries are; an
    author-year one comes back whole, and the rules fall back to reading the
    list as one entry, which is where they started.
    """
    if not listing.strip():
        return []
    parts = []
    at = 0
    for mark in _marks(listing):
        parts.append(listing[at:mark.start()])
        at = mark.end()
    parts.append(listing[at:])
    cut = [built for part in parts if part.strip()
           and (built := quotes.build(part)).squashed]
    return cut if len(cut) > 1 else [quotes.build(listing)]


_BARE = re.compile(r"\d{1,3}")
_DIGITS = re.compile(r"\d+")


def _marks(listing: str) -> list[re.Match]:
    """Where a list numbers its entries, the pages' own numbers left out.

    A bare number at the head or the foot of a page may be the folio: nothing
    but the break stands between them, and read as an entry it would cut a
    reference in two at the page break, leaving a title on one side of the cut
    without the identifier on the other that says which paper it is. In a list
    that numbers itself in bare numbers, what tells the two apart is the
    counting: an entry marker carries on from the one before it, and a page
    number does not.
    """
    found = list(_ENTRY_MARK.finditer(listing))
    # A list numbers its entries one way. Where it writes its markers with a
    # bracket or a dot, a bare number is not one of them — it is the page's,
    # and no counting can tell a folio that happens to fall where the next
    # marker would from the marker itself.
    delimited = any(not _BARE.fullmatch(mark.group().strip()) for mark in found)
    kept: list[re.Match] = []
    for mark in found:
        if _BARE.fullmatch(mark.group().strip()):
            if delimited:
                continue
            if _at_a_page_edge(listing, mark):
                expected = _numbered(kept[-1]) + 1 if kept else 1
                if _numbered(mark) != expected:
                    continue
        kept.append(mark)
    return kept


def _numbered(mark: re.Match) -> int:
    """The number a marker carries."""
    return int(_DIGITS.search(mark.group()).group())


def _at_a_page_edge(listing: str, mark: re.Match) -> bool:
    """Whether a marker has nothing but a page break on one side of it."""
    before = listing.rfind(quotes.PAGE_BREAK, 0, mark.start())
    after = listing.find(quotes.PAGE_BREAK, mark.end())
    return ((before >= 0 and not listing[before + 1:mark.start()].strip())
            or (after >= 0 and not listing[mark.end():after].strip()))


# What marks a letter or numeral as a section label rather than the first word
# of a sentence: the dot or bracket after it. "A. REFERENCES" is a heading and
# "A reference" is the start of a line of prose. A bracket in front of it is
# how some papers write the same label: "(A) References". The space after the
# label is what an extraction is likeliest to lose, so it is not required.
_LABEL = re.compile(r"^[ \t]*[\[(]?[0-9A-Za-z]{1,7}[.)\]]+[ \t]*")


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
    page = 1
    found: list[tuple[int, bool]] = []
    # `splitlines` breaks on the page separator too, so a heading at the top of
    # a page is found like any other.
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        at += len(line)
        here, page = page, page + line.count(quotes.PAGE_BREAK)
        squashed = quotes.squash(line)
        # Measured on the letters, not the line: a small-capitals heading can
        # come out of pypdf with a space between every letter, and "R E F E R
        # E N C E S  A N D  F U R T H E R  R E A D I N G" is long as a line
        # and short as a heading.
        if len(squashed) > _HEADING_LETTERS:
            continue
        label = _LABEL.match(line)
        if _is_heading(squashed, labelled=bool(label),
                       label=quotes.squash(label.group()) if label else ""):
            found.append((at, _points_at_a_page(lines, i + 1, here)))
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
_PAGE_NUMBER = re.compile(r"^[ 	.·•…‐-―]*(\d{1,4})[ 	]*$")


def _points_at_a_page(lines: list[str], start: int, page: int) -> bool:
    """Whether a bare number under a heading is a page for it to point at.

    Larger than the page the heading is on, because a contents entry points
    forward and a bibliography numbers its first entry 1 on a page well past
    the first. The shape alone does not tell them apart — `References` over
    `12` reads the same either way — and a paper with a supplement has a
    second heading for the loop below to fall back to, so having another
    heading says nothing about which of the two this is.
    """
    for line in lines[start:]:
        if quotes.PAGE_BREAK in line:
            return False        # over the page, and no longer under the heading
        if not line.strip():
            continue
        number = _PAGE_NUMBER.match(line.rstrip("\n\r\f"))
        return bool(number) and int(number.group(1)) > page
    return False


def _is_heading(squashed: str, labelled: bool = False, label: str = "") -> bool:
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
    if (label and any(char.isdigit() for char in label) and squashed.startswith(label)):
        # The label as the line wrote it, where it counts: a supplement numbers
        # its bibliography "S1. References", and neither a letter nor a roman
        # numeral accounts for that. A number in it is what says it is a label
        # and not a word — "Cf. references" is prose, and taking "Cf" off it
        # would make a heading of it.
        candidates.append(squashed[len(label):])
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
    if not said:
        return False
    return (any(word in _HEADING_ANCHORS for word in said)
            or ("cited" in said and any(word in _CITED_ANCHORS for word in said)))


def title_mark(paper: dict) -> str:
    """The squashed title, where it is long enough to name a paper on its own.

    Empty for a short title, and a paper left with nothing but its identifiers
    is named by those alone — which is why the callers ask for this rather
    than reading `fingerprints` from the end.
    """
    title = quotes.squash(paper.get("title") or "")
    return title if len(title) >= _TITLE_FLOOR else ""


@dataclass(frozen=True)
class Names:
    """What a paper can be named by in somebody's reference list.

    The arXiv id is kept apart from the rest because it is the one identifier
    printed with a version: `1706.03762v5` is that paper, where `10.1234/foov2`
    is a DOI of its own.
    """

    arxiv: str
    identifiers: tuple[str, ...]
    title: str
    # Each identifier as the paper records it, by the mark it squashes to.
    # Squashing takes the punctuation out, and two DOIs that differ only in
    # where theirs falls — `10.1234/foo.bar` and `10.1234/foob.ar` — come to
    # the same letters. What is printed says which of them an entry means.
    printed: dict[str, str]

    @property
    def all(self) -> list[str]:
        """Every mark, identifiers first and the title last, each once."""
        return list(dict.fromkeys(mark for mark in (*self.identifiers, self.title) if mark))


def names(paper: dict) -> Names:
    """What to look for in a reference list to find this paper named.

    An arXiv id and a DOI are near enough unique on their own. A title is not,
    strictly, but a twenty-character run of one paper's title inside another's
    bibliography is a citation and not a coincidence.
    """
    source = paper.get("source") or {}
    written = []
    if source.get("kind") == "arxiv" and source.get("id"):
        # Without the version: an id is cited at whichever version, and the
        # `v5` is not part of what names the paper.
        written.append(re.sub(r"v\d+$", "", str(source["id"]), flags=re.I))
    arxiv = quotes.squash(written[0]) if written else ""
    # Crossref fills in `doi` and `source["id"]` alike, and one identifier
    # recorded twice is still one identifier.
    written += [str(value) for value in
                (paper.get("doi"), source.get("id") if source.get("kind") == "doi" else "")
                if value]
    printed = {}
    for value in written:
        mark = quotes.squash(value)
        if mark:
            printed.setdefault(mark, value)
    return Names(arxiv=arxiv, title=title_mark(paper),
                 identifiers=tuple(printed), printed=printed)


def fingerprints(paper: dict) -> list[str]:
    """Every mark a paper can be found by, identifiers first."""
    return names(paper).all


def edges(papers: list[dict] | None = None) -> list[dict]:
    """Every `{"from": key, "to": key}` where one paper's references name another.

    The answer is cached against the corpus and the extracted text, both of
    which it is read from, so the map can ask for it on every opening.
    """
    given = papers is not None
    papers = papers if given else store.all_papers()
    named = _named_signature(papers)
    with _cache_lock:
        hit = _cache.get(f"{named}:{_read_signature(
            {paper['key']: _identity(paper['key']) for paper in papers})}")
    if hit is not None:
        return hit
    marks = {paper["key"]: names(paper) for paper in papers}
    read: dict[str, tuple | None] = {}
    found = []
    for paper in papers:
        # The state each paper's text was in when it was read. A PDF arriving
        # for a paper this scan has already passed changes its text and not
        # its name, and the answer would be stored as though it had seen it.
        #
        # Read on both sides of the reading, since a PDF can arrive during it
        # and leave new text behind the old text this returned. Unchanged
        # across the read is the text that was read; where there was none, the
        # reading is what wrote it and it is the text that was read too.
        # Anything else is a paper whose text this scan cannot speak for, and
        # `None` says so.
        was = _identity(paper["key"])
        text = search.paper_text(paper["key"])
        now = _identity(paper["key"])
        # `was[0]` is the stored text: where there was none, this reading is
        # what wrote it, and the text it wrote is the text it read — as long
        # as the PDF it came from is the one that is still there.
        read[paper["key"]] = (now if now == was or (not was[0] and now[1] == was[1])
                              else None)
        if not text:
            continue
        references = reference_text(text)
        listing = entries(references)
        if not listing:
            continue
        # A list that numbers nothing is one long stretch, and a paper named
        # by an identifier there has no entry to tie it to a printing of a
        # title somebody else may have cited.
        uncut = not _marks(references)
        for other in _cited(paper["key"], listing, marks, uncut):
            found.append({"from": paper["key"], "to": other})
    found.sort(key=lambda edge: (edge["from"], edge["to"]))
    # Stored only if what it was read from is still what is there: the papers
    # unchanged, and every paper's text as it was when this scan read it.
    # Reading a paper for the first time is what writes its text down, so the
    # comparison is against what this scan saw rather than what was on disk
    # when it started.
    steady = all(was is not None and _identity(key) == was
                 for key, was in read.items())
    if given or (steady and named == _named_signature(store.all_papers())):
        with _cache_lock:
            _cache.clear()  # one corpus at a time is all the map ever asks for
            # Keyed off what this scan read, not off what the text says now:
            # sampling the pile again here would take in a paper replaced
            # since, and file the old answer under the new text's name.
            _cache[f"{named}:{_read_signature(read)}"] = found
    return found


def _cited(key: str, entries: list[quotes.Text], marks: dict[str, Names],
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


def _cited_in(key: str, entry: quotes.Text, marks: dict[str, Names],
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
    where: dict[tuple, list[int]] = {}

    def places(mark: str, identifier: bool, versioned: bool = False,
               printed: str = "") -> list[int]:
        """Where a mark is printed, and printed whole.

        An identifier only where the entry writes that identifier: squashing
        takes the punctuation out of a DOI, and `10.1234/foo` reads straight
        through the middle of `10.1234/foo.bar`, which is somebody else's
        work. A title only where it starts and ends with a word: `Understanding
        neural network` reads through `Understanding neural networks`, which is
        a paper of its own.
        """
        if (mark, identifier, versioned, printed) not in where:
            at = _occurrences(entry.squashed, mark)
            where[mark, identifier, versioned, printed] = [
                place for place in at
                if (_whole(entry, place, len(mark), versioned, printed)
                    if identifier else _bounded(entry, place, len(mark)))]
        return where[mark, identifier, versioned, printed]

    def beside(mark: str, versioned: bool, title: str, printed: str = "") -> bool:
        """Whether the one printing of a title has this identifier on its line.

        A line of a reference list is one entry naming one work, however the
        list is written, so an entry that gives a shared title and one twin's
        identifier together is naming that twin. One printing only: two
        printings are two entries, and the title in the other one is somebody
        else's to claim.
        """
        printings = places(title, False)
        if len(printings) != 1:
            return False
        begin = entry.offsets[printings[0]]
        end = entry.offsets[printings[0] + len(title) - 1] + 1
        for place in places(mark, True, versioned, printed):
            at = entry.offsets[place]
            stop = entry.offsets[place + len(mark) - 1] + 1
            between = entry.raw[end:at] if at >= end else entry.raw[stop:begin]
            if "\n" not in between and quotes.PAGE_BREAK not in between:
                return True
        return False

    for other, found in marks.items():
        if other == key:
            continue
        title = found.title
        by_identifier = next((mark for mark in found.identifiers
                              if places(mark, True, mark == found.arxiv,
                                        found.printed.get(mark, ""))), "")
        # Where the list is one stretch, an identifier is the only thing that
        # points at this paper and nobody else's: a title printed somewhere in
        # a page of references may be somebody's citation of its twin. Unless
        # the two are printed on one line, which is one entry naming one work.
        by_title = title if title and places(title, False) else ""
        if uncut and by_identifier and not beside(
                by_identifier, by_identifier == found.arxiv, title,
                found.printed.get(by_identifier, "")):
            by_title = ""
        named_by = by_title or by_identifier
        if not named_by:
            continue
        claims[other] = (places(named_by, named_by is not title, named_by == found.arxiv,
                                found.printed.get(named_by, "")),
                         len(named_by), bool(by_identifier))
        # Everywhere this paper is named, whatever it is cited by here. An
        # identifier settles which paper an entry means; it does not stop the
        # paper's title covering a shorter title printed inside it.
        for mark in found.all:
            covers.setdefault(other, []).extend(
                (at, len(mark)) for at in places(mark, mark is not title, mark == found.arxiv,
                                                 found.printed.get(mark, "")))
        if by_identifier and named_by is not by_identifier:
            # Where every printing of its title turns out to be inside
            # somebody else's, the identifier is what it is cited by: an entry
            # naming this paper by its DOI alone is a citation of it, however
            # its title reads elsewhere in the list.
            fallbacks[other] = (places(by_identifier, True, by_identifier == found.arxiv,
                                       found.printed.get(by_identifier, "")),
                                len(by_identifier))

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


# What an arXiv id is printed with and is still that paper: the version it is
# cited at, and the extension of the PDF it is linked to. Neither is the
# identifier running on into somebody else's.
_ARXIV_TAIL = re.compile(r"(?:v\d+)?(?:\.pdf)?(?![0-9A-Za-z])", re.I)

# The dashes an extraction can write a hyphen as. They are the one difference
# between two printings of an identifier that means nothing.
_DASHES = "-\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
# What an identifier is written with, which `ingest.DOI_RE` writes out as
# `[-._;()/:A-Za-z0-9]`, with those dashes: the punctuation here joins one part
# of an identifier to the next, and so says the identifier has not ended where
# a fingerprint of it has.
_JOINS = "./_:;()" + _DASHES
# Where an identifier was split to fit the page: a line or page break, or the
# soft hyphen an extraction leaves where a word may be broken, with whatever
# the next line is indented by.
_WRAP = re.compile(r"[ \t]*[\n\r\f\u00ad][ \t]*")
_WRAP_END = re.compile(r"[ \t]*[\n\r\f\u00ad][ \t]*$")
# What holds a word together across a mark that is not a letter: a hyphen in
# any of its shapes, and the two apostrophes.
_WORD_JOINS = _DASHES + "'\u2019"
_IDENTIFIER_CHAR = re.compile(r"[-._;()/:A-Za-z0-9\u2010-\u2015\u2212]")
_IDENTIFIER_RUN = re.compile(r"[-._;()/:A-Za-z0-9\u2010-\u2015\u2212]*")
# Where a DOI starts. `ingest.DOI_RE` again, without its suffix.
_DOI_HEAD = re.compile(r"10\.\d{4,9}/")



def _bounded(entry: quotes.Text, at: int, width: int) -> bool:
    """Whether a title is printed at `at` as the whole of a title.

    Squashing takes the spaces out, so a title reads straight through a longer
    word at either end: `Understanding neural network` is inside
    `Understanding neural networks`, and the two are different papers.

    And through a longer title: an entry citing `Attention is all you need for
    image restoration` is not citing `Attention is all you need`, whether or
    not the longer paper is in the corpus to say so. A title has to end where
    the entry stops writing it — at the full stop, the comma or the quotation
    mark that every citation style puts after a title — so a word after it
    with nothing but a space between is more of the same title.

    Only at the end. What comes before a title is the authors, and a space is
    what an entry puts between them and it.
    """
    begin = entry.offsets[at]
    stop = entry.offsets[at + width - 1] + 1
    # Past the marks belonging to the letter the title ends on, and back over
    # the ones belonging to the letter before it: an accent written as a mark
    # of its own is part of its letter, not the end of a word, and `café` is
    # inside `caféine` whichever way the extraction writes the accent.
    while stop < len(entry.raw) and unicodedata.combining(entry.raw[stop]):
        stop += 1
    while begin and unicodedata.combining(entry.raw[begin - 1]):
        begin -= 1
    after = entry.raw[stop:stop + 12]
    before = entry.raw[max(begin - 12, 0):begin]
    if after[:1].isalnum() or before[-1:].isalnum():
        return False
    # A space after a title is more of the same title, and so is a colon with
    # a subtitle after it: what a paper is recorded under carries its subtitle
    # too, so an entry that writes one the record has not got is writing a
    # different work's title.
    if after[:1].isspace() or (after[:1] in _SUBTITLE and after[1:].strip()):
        return False
    # A hyphen the entry wrapped at stands before the indentation of the line
    # the word goes on to: `Network-\n  based` is one word, and a title that
    # starts at `based` starts inside it.
    before = _WRAP_END.sub("", before)
    # A hyphen or an apostrophe with more word on the other side is inside a
    # word too: `network-based` and `model's` are not `network` and `model`.
    # Only there is a wrap read through, since a title at the end of a line is
    # an entry ending and not a word going on.
    return not ((after[:1] in _WORD_JOINS and _WRAP.sub("", after[1:])[:1].isalnum())
                or (before[-1:] in _WORD_JOINS and _WRAP.sub("", before[:-1])[-1:].isalnum()))


# What stands between a title and the subtitle it goes on with.
_SUBTITLE = ":\uff1a"
# What an identifier finishes with where it does not finish with a letter.
_FINISH = re.compile(r"[^0-9A-Za-z]*$")
# The marks a sentence never ends with, so an entry printing one straight after
# an identifier is printing more of the identifier.
_NEVER_LAST = "_/" + _DASHES


def _ends_in_a_letter(printed: str) -> str:
    """An identifier with the punctuation at its ends taken off.

    A DOI may finish with a bracket — `10.1234/foo(2)` is one, and
    `ingest.normalize_doi` keeps the pair — and squashing leaves no character
    for it, so the span a match covers stops at the last letter or digit and
    has nothing to compare the bracket with.
    """
    return re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", printed)


def _shape(text: str) -> str:
    """An identifier as its punctuation: which mark and where, with the
    letters and digits taken out and the dashes read as one.

    Decomposed first, as `quotes.squash` decomposes: an extraction can write
    `ffi` as one ligature, and counted as one letter it would not line up with
    the three the stored identifier has. The marks it leaves go the same way
    they go there."""
    return "".join("#" if char.isalnum() else "-" if char in _DASHES else char
                   for char in unicodedata.normalize("NFKD", text)
                   if not unicodedata.combining(char))


def _whole(entry: quotes.Text, at: int, width: int, versioned: bool = False,
           printed: str = "") -> bool:
    """Whether an identifier printed at `at` is the whole of the one there.

    Read off the entry's own text rather than the squashed form, since
    squashing is what took the dots and slashes out: `101234foo` reads through
    the middle of `101234foobar` with nothing to say where one ends, while
    `10.1234/foo` beside `10.1234/foo.bar` is plain enough.

    Both ends, since an identifier can be read into from either: the arXiv id
    `1706.03762` sits inside the DOI `10.1706/03762` and ends where it ends.

    `versioned` for an arXiv id, which is printed with the version it is
    cited at and with the extension of the PDF it is linked to —
    `1706.03762v5` and `1706.03762.pdf` are that paper. A DOI is not:
    `10.1234/foov2` is a DOI of its own, not a printing of `10.1234/foo`.
    """
    stop = entry.offsets[at + width - 1] + 1
    # What the entry has between the characters the identifier is made of: the
    # punctuation that writes one, and the break a wrapped one is split at.
    # Anything else is a run of digits that squashed into the same letters —
    # `Vol. 10, 1234. Foo.` is not a DOI, whatever it comes to with the commas
    # and the spaces taken out.
    span = _WRAP.sub("", entry.raw[entry.offsets[at]:stop])
    if any(not (char.isalnum() or char in _JOINS) for char in span):
        return False
    # And the same punctuation in the same places: `10.1234/foo.bar` and
    # `10.1234/foob.ar` are two DOIs and one fingerprint, and so are
    # `10.1234/foo.bar-baz` and `10.1234/foo-bar.baz`. Only the dashes are
    # read as one, since an extraction can write a hyphen as any of them.
    if printed and _shape(span) != _shape(_ends_in_a_letter(printed)):
        return False
    rest = entry.raw[stop:]
    # An identifier that finishes with punctuation has to be printed with it:
    # `10.1234/foo_` is not `10.1234/foo`, and squashing keeps no character
    # for either ending. Where it finishes with a letter, what follows is the
    # entry's own punctuation — a full stop or a comma — except for the marks
    # no sentence ends with, which say the identifier went on.
    finish = _FINISH.search(printed).group() if printed else ""
    if finish:
        if _shape(rest[:len(finish)]) != _shape(finish):
            return False
        rest = rest[len(finish):]
    elif rest[:1] in _NEVER_LAST:
        return False
    version = _ARXIV_TAIL.match(rest) if versioned else None
    if version:
        rest = rest[version.end():]
    if rest[:1].isalnum():
        return False
    # More identifier after the punctuation that joins its parts, however much
    # punctuation that is: `.bar` of `10.1234/foo.bar`, `/(2)` of
    # `10.1234/foo/(2)`. The run stops at the comma or the space an entry goes
    # on with, so a DOI at the end of what it is cited by reads whole.
    run = _IDENTIFIER_RUN.match(rest)
    if run and any(char.isalnum() for char in run.group()):
        return False
    begin = entry.offsets[at]
    lead = entry.raw[:begin]
    if lead[-1:].isalnum():
        return False
    # A separator with a number before it: the `10.` of `10.1706/03762`, where
    # `doi:` and `doi.org/` in front of an identifier end in a letter.
    if lead[-1:] in _JOINS and lead[-2:-1].isdigit():
        return False
    # Or further back: a DOI can have anything in its suffix, `10.1234/abc/`
    # and an arXiv id after it included, and what is printed there is that
    # DOI rather than the paper the id belongs to. Only as far back as the
    # run of identifier characters the match sits in — `doi.org/` in front of
    # a DOI is part of that run and is not a DOI itself.
    start = len(lead)
    while start and _IDENTIFIER_CHAR.match(lead[start - 1]):
        start -= 1
    return not _DOI_HEAD.search(lead[start:])


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

    Half of what the citations are read from; `_read_signature` is the other
    half. Not the corpus signature: that moves when a claim is edited or a
    topic renamed, neither of which changes a bibliography, and every such
    move would throw the answer away and read the pile again.
    """
    parts = []
    for paper in sorted(papers, key=lambda p: p["key"]):
        found = names(paper)
        # Each identifier as it is written as well as as it squashes: where
        # its punctuation falls decides what an entry is a citation of, so a
        # DOI corrected from `10.1234/foo.bar` to `10.1234/foob.ar` is a
        # different paper to look for. The arXiv id is named because it is
        # the one that may be printed with a version.
        parts.append(" ".join([paper["key"], found.title, found.arxiv]
                              + [f"{mark}={found.printed[mark]}"
                                 for mark in found.identifiers]))
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _identity(key: str) -> tuple:
    """A paper's stored text and the PDF behind it, each as it stands now.

    The PDF as well as the text, because a hit answers without reading a
    paper: a PDF replaced by hand leaves text that `search.paper_text` would
    throw away and extract again, and a key made of the text alone would not
    move, so the map would go on being served the old arrows.
    """
    return (_stat(store.text_path(key)), _stat(store.pdf_path(key)))


def _stat(path) -> tuple:
    """A file as it stands, or nothing where there is no file."""
    try:
        st = path.stat()
    except OSError:
        return ()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _read_signature(read: dict) -> str:
    """The text a scan read, named by what each paper's text was at the time.

    What the citations were read from, and the other half of the cache key.
    Only the papers in the corpus: text left behind by a paper that is gone
    says nothing about the answer, and re-reading the pile would have thrown
    it away for nothing.
    """
    parts = "\n".join(f"{key} {was}" for key, was in sorted(read.items()))
    return hashlib.sha1(parts.encode("utf-8")).hexdigest()


