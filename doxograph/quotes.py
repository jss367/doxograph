"""Check that a claim's quote is actually in the paper, and show where.

The extraction prompt insists that `quote` be verbatim, and nothing else in
the pipeline can tell whether the model obeyed. This can: the PDF's text is
compared against the quote on letters and digits alone, so line breaks,
hyphenation, ligatures and punctuation drift do not count against a quote
that is really there.

Finding the quote is only half of it. `locate` keeps the alignment it found
and maps it back to the paper's own characters, so a claim whose quote does
not check out can be shown next to the sentence it was meant to be, on the
page it is on. The squashed text carries an offset per character for that,
which is what separates this module's `Text` from a plain string.
"""

from __future__ import annotations

import array
import contextlib
import difflib
import os
import re
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# A paper's text is cached per file, since a paper is checked once per claim
# and text extraction is the slow part. Keyed on the file's identity so a
# replaced PDF is re-read.
_cache: dict[Path, tuple[tuple[int, int, int], "Text"]] = {}
_cache_lock = threading.Lock()
_CACHE_LIMIT = 32

# A quote shorter than this, once squashed, has to appear exactly: a handful
# of letters is found by accident in any paper.
_EXACT_BELOW = 24
# Longer quotes are located by any exact run of this many characters, then
# compared in full against that stretch of the paper. The share of the quote's
# characters found there, in order, is its coverage; a quote with a garbled
# word scores just under 1.0, a paraphrase well under this.
_ANCHOR = 12
_COVERAGE = 0.9
# Below this, the best alignment is noise — long quotes share enough letters
# with any paragraph to land somewhere — and there is no passage worth showing.
_NEARBY = 0.5
# How many places one anchor may be tried at. A run this long seldom recurs
# in one paper, so the cap is a guard against a degenerate input, not a
# working limit.
_MAX_SITES = 200

# Pages are kept in one string with the conventional separator between them,
# the same one `pdftotext` uses, so the text cache on disk stays readable and
# a page number is a count of separators.
PAGE_BREAK = "\f"

# How far either side of a match to look for the sentence it sits in, and how
# much of the paper to show around that sentence.
_SENTENCE_SPAN = 600
_CONTEXT_SPAN = 400


def squash(text: str) -> str:
    """Letters and digits only, in any script, lowercase; ligatures decomposed
    and accents dropped, since PDF extraction is unreliable on both."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text.casefold() if ch.isalnum())


@dataclass(frozen=True)
class Text:
    """A paper's text: what was extracted, and the squashed form aligned to it.

    `offsets[i]` is the index in `raw` of the character that produced
    `squashed[i]`, so a match found in the squashed text can be read back in
    the paper's own words. One raw character can produce several squashed ones
    (a ligature, an eszett) and most produce none at all.
    """

    raw: str
    squashed: str
    offsets: array.array

    @property
    def pages(self) -> int:
        return self.raw.count(PAGE_BREAK) + 1

    def page_of(self, at: int) -> int:
        """The 1-based page holding the character at `at` in `raw`."""
        return self.raw.count(PAGE_BREAK, 0, at) + 1

    def page_bounds(self, at: int) -> tuple[int, int]:
        """The span of `raw` holding the page that `at` falls on."""
        begin = self.raw.rfind(PAGE_BREAK, 0, at) + 1
        end = self.raw.find(PAGE_BREAK, at)
        return begin, len(self.raw) if end < 0 else end


def build(raw: str) -> Text:
    """Squash `raw` while recording where each surviving character came from.

    Character by character rather than over the whole string, which is what
    makes the offsets possible. It agrees with `squash` on the result: NFKD
    decomposes each character independently, and the combining marks whose
    order it can change are dropped here anyway.
    """
    squashed: list[str] = []
    offsets = array.array("i")
    for at, char in enumerate(raw):
        for out in unicodedata.normalize("NFKD", char).casefold():
            if out.isalnum():
                squashed.append(out)
                offsets.append(at)
    return Text(raw, "".join(squashed), offsets)


def _extract(path: Path) -> str:
    """Every page of the PDF, separated by `PAGE_BREAK`. Empty if unreadable."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        return PAGE_BREAK.join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


@contextlib.contextmanager
def _unguarded():
    yield


def paper_text(path: Path, cache: Path | None = None, guard=None) -> Text | None:
    """The paper's text. None if it cannot be read, or if it has no text: a
    scanned paper opens fine but every page comes back empty, and there is
    nothing to check a quote against.

    `cache` names a file to keep the extracted text in between runs. Reading a
    long paper with pypdf takes about a second, which is charged once per
    claim without it, and the cached text is the paper in a form the rest of
    the corpus can read. It is used when it is newer than the PDF.

    `guard` is the paper's lock, for the callers that have one. Reading a PDF
    and storing its text is two steps, and a publish landing between them
    leaves the text of a paper that is no longer there — stored last, so
    stored newest, and believed. Held across both steps that cannot happen.
    The module keeps no `store` of its own, so the lock comes from the caller.
    """
    with (guard or _unguarded)():
        # Everything from here is about one state of the file, and the lock is
        # what holds it still: a publish replaces the PDF and drops its stored
        # text under the same lock, so without it a reading that began before
        # the replacement could be finished, cached and believed after it.
        try:
            st = path.stat()
        except OSError:
            return None
        identity = (st.st_size, st.st_mtime_ns, st.st_ino)
        # The stored text has to be there as well as the PDF unchanged.
        # Deleting `text/<key>.txt` is how a corpus is told to read its papers
        # again — the README says so — and a process that had already cached
        # one in memory would otherwise go on answering from it and never
        # write the file back.
        if cache is None or cache.exists():
            with _cache_lock:
                hit = _cache.get(path)
                if hit and hit[0] == identity:
                    return hit[1] if hit[1].squashed else None
        raw = _read_cached(cache, st.st_mtime_ns)
        if raw is None:
            raw = _extract(path)
            _write_cached(cache, raw)
    text = build(raw)
    with _cache_lock:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.pop(next(iter(_cache)))
        _cache[path] = (identity, text)
    return text if text.squashed else None


def _read_cached(cache: Path | None, pdf_mtime_ns: int) -> str | None:
    """The stored text, if it was written strictly after the PDF it came from.

    Strictly: a PDF replaced by `os.replace` keeps the staged file's mtime,
    which can equal the stored text's, and reading it as fresh would check
    quotes against the paper that used to be there.
    """
    if cache is None:
        return None
    try:
        if cache.stat().st_mtime_ns <= pdf_mtime_ns:
            return None
        return cache.read_text(encoding="utf-8")
    except OSError:
        return None


def _write_cached(cache: Path | None, raw: str) -> None:
    """Store the extracted text, including the empty text of a scanned paper
    so it is not parsed again on every claim.

    Written to a temporary file and moved into place: `doxograph serve` and a
    `doxograph extract` in a shell share one corpus, and a truncating write
    leaves a window where the other process reads a half-written file, sees a
    newer mtime, and trusts it. A corpus that cannot be written to is not
    worth failing a quote check over.
    """
    if cache is None:
        return
    staged = None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        handle, name = tempfile.mkstemp(dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp")
        staged = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.replace(staged, cache)
    except OSError:
        if staged is not None:
            staged.unlink(missing_ok=True)


def pdf_text(path: Path, cache: Path | None = None, guard=None) -> str | None:
    """The whole paper's text, squashed. None when there is none to read."""
    text = paper_text(path, cache, guard)
    return text.squashed if text else None


def coverage(quote: str, haystack: str) -> float:
    """How much of `quote` appears in `haystack`, both already squashed.

    1.0 for a substring. Otherwise the quote is lined up with the paper at
    every place one of its anchors occurs, and the best alignment's share of
    matching characters is returned.
    """
    return best_match(quote, haystack)[0]


def best_match(quote: str, haystack: str) -> tuple[float, int, int]:
    """`coverage`, with the stretch of `haystack` that earned it.

    The span is trimmed to the matching characters themselves, so it holds the
    paper's version of the quote and not the margin searched around it. It is
    `(0.0, -1, -1)` when nothing lines up at all.
    """
    if not quote:
        return (0.0, -1, -1)
    at = haystack.find(quote)
    if at >= 0:
        return (1.0, at, at + len(quote))
    if len(quote) < _EXACT_BELOW:
        return (0.0, -1, -1)
    # Full-length anchors first; if none of them lines the quote up well
    # enough, half-length ones, which survive an error at an anchor seam
    # in a quote near the minimum length. Both passes are capped the same.
    found = _align(quote, haystack, _ANCHOR)
    if found[0] < _COVERAGE:
        second = _align(quote, haystack, _ANCHOR // 2)
        if second[0] > found[0]:
            found = second
    return found


def _align(quote: str, haystack: str, size: int) -> tuple[float, int, int]:
    """The best coverage over every place an anchor of `size` characters
    from `quote` occurs in `haystack`, and where that alignment sits."""
    n = len(quote)
    best = (0.0, -1, -1)
    tried: set[int] = set()
    for k in range(0, n - size + 1, size):
        anchor = quote[k:k + size]
        at = haystack.find(anchor)
        while at >= 0 and len(tried) < _MAX_SITES:
            # Where the quote would start if this anchor sits where it does in
            # the quote, with a margin either side for an inserted word.
            begin = max(0, at - k - _ANCHOR)
            if begin not in tried:
                tried.add(begin)
                window = haystack[begin:begin + n + 2 * _ANCHOR]
                matcher = difflib.SequenceMatcher(None, quote, window, autojunk=False)
                blocks = [b for b in matcher.get_matching_blocks() if b.size]
                found = sum(block.size for block in blocks) / n
                if blocks and found > best[0]:
                    best = (found, begin + blocks[0].b,
                            begin + blocks[-1].b + blocks[-1].size)
                if best[0] >= 1.0:
                    return best
            at = haystack.find(anchor, at + 1)
    return best


def verify(pdf: Path, quote: str, cache: Path | None = None, guard=None) -> bool | None:
    """Whether `quote` is in the paper at `pdf`.

    None when there is nothing to check: no quote, or no readable PDF.
    """
    found = locate(pdf, quote, cache, guard)
    return None if found is None else found["found"]


def locate(pdf: Path, quote: str, cache: Path | None = None, guard=None) -> dict | None:
    """Where `quote` sits in the paper at `pdf`, in the paper's own words.

    None when there is nothing to check: no quote, or no readable PDF. A quote
    that is nowhere near the paper still returns a result, with `found` false
    and nothing to show for it.
    """
    needle = squash(quote)
    if not needle:
        return None
    text = paper_text(pdf, cache, guard)
    if text is None:
        return None
    score, lo, hi = best_match(needle, text.squashed)
    result = {
        "found": score >= _COVERAGE,
        "coverage": round(score, 3),
        "page": None,
        "pages": text.pages,
        "before": "",
        "suggestion": "",
        "after": "",
        # True when the quote occurs in the paper more than once. Then the
        # passage shown is the first of them and may not be the one the claim
        # was drawn from, so nothing is concluded from which page it is on.
        "repeated": False,
    }
    if lo < 0 or score < _NEARBY:
        return result
    # The stretch that was matched, not the quote: a match that is not exact
    # still sits on a passage the paper may print twice, and the page it is on
    # says as little then as it does for a repeated quote.
    span = text.squashed[lo:hi]
    result["repeated"] = bool(span) and (text.squashed.find(span, lo + 1) >= 0
                                         or text.squashed.rfind(span, 0, lo) >= 0)
    start, end = text.offsets[lo], text.offsets[hi - 1] + 1
    # The sentence is not bounded by the page. A quote can run over a page
    # break, and so can the sentence around one that does not; cut at the
    # break, "use the paper's wording" would save half a sentence as the
    # quote. What bounds the search is the span either side, as always.
    begin, finish = _sentence_bounds(text.raw, start, end, 0, len(text.raw))
    result["page"] = text.page_of(start)
    result["suggestion"] = tidy(text.raw[begin:finish])
    # The text around it stays on the sentence's own pages: a page further out
    # is a different part of the paper, and reading it here would mislead.
    low = text.page_bounds(begin)[0]
    high = text.page_bounds(max(finish - 1, begin))[1]
    result["before"] = tidy(text.raw[_back(text.raw, begin, low):begin])
    result["after"] = tidy(text.raw[finish:_forward(text.raw, finish, high)])
    return result


# A sentence ends at one of these, closing quotes and brackets included, when
# whitespace follows. The abbreviations are the ones a paper ends a word with
# often enough to matter; an initial is caught by the single capital letter.
_SENTENCE_END = re.compile(r"[.!?][\"'’”)\]]*\s")
_ABBREV = re.compile(
    r"(?:\b[A-Z]|\b(?:al|e\.g|i\.e|cf|approx|est|vs|etc|Fig|Figs|Tab|Tabs|Sec|Secs"
    r"|Eq|Eqs|Ref|Refs|App|Ch|No|Nos|Vol|pp|p|St|Dr|Prof))\.$"
)


def _is_boundary(before: str) -> bool:
    """Whether text ending in a sentence terminator really ends a sentence."""
    return not _ABBREV.search(before.rstrip("\"'’”)]"))


def _sentence_bounds(raw: str, start: int, end: int, low: int, high: int) -> tuple[int, int]:
    """Expand a match to the sentence holding it, within one page.

    A quote that runs over a sentence boundary keeps both sentences: the span
    only ever grows. When no boundary turns up within the search window the
    span stops at a word break, which is better than cutting mid-word.
    """
    left = max(low, start - _SENTENCE_SPAN)
    begin = left
    for match in _SENTENCE_END.finditer(raw, left, start):
        if _is_boundary(raw[left:match.start() + 1]):
            begin = match.end()
    if begin == left and left > low:
        begin = _word_start(raw, left)

    right = min(high, end + _SENTENCE_SPAN)
    finish = right
    for match in _SENTENCE_END.finditer(raw, max(end - 1, begin), right):
        if match.end() > end and _is_boundary(raw[begin:match.start() + 1]):
            finish = match.end()
            break
    if finish == right and right < high:
        finish = _word_end(raw, right)
    return begin, min(max(finish, end), high)


def _word_start(raw: str, at: int) -> int:
    """Forward from `at` to the start of the next whole word."""
    while at < len(raw) and not raw[at].isspace():
        at += 1
    while at < len(raw) and raw[at].isspace():
        at += 1
    return at


def _word_end(raw: str, at: int) -> int:
    """Back from `at` to the end of the last whole word."""
    while at > 0 and not raw[at - 1].isspace():
        at -= 1
    return at


def _back(raw: str, at: int, low: int) -> int:
    begin = max(low, at - _CONTEXT_SPAN)
    return begin if begin == low else _word_start(raw, begin)


def _forward(raw: str, at: int, high: int) -> int:
    end = min(high, at + _CONTEXT_SPAN)
    return end if end == high else _word_end(raw, end)


# A word broken across a line by a hyphen is put back together: the hyphen and
# the break between its halves are what this matches, and they come out. Only
# at a line break — "pre- and post-training" is a hyphen followed by a space,
# and joining that would invent a word. A page break is a line break too: a
# word split at the foot of a page has a form feed between its halves.
#
# Public because the search folds its own copy of the papers and has to do the
# same thing to them; two spellings of this rule would find different words.
LINE_HYPHEN = re.compile(rf"(?<=\w)[-‐­][ \t]*[\n{PAGE_BREAK}][ \t\n{PAGE_BREAK}]*(?=\w)")


def tidy(text: str) -> str:
    """The paper's wording as a line of prose: line breaks closed up, words
    broken across lines rejoined. What a reviewer would paste into a quote."""
    return re.sub(r"\s+", " ", LINE_HYPHEN.sub("", text)).strip()


_WORD = re.compile(r"\S+\s*")


def word_diff(quote: str, paper: str) -> list[dict]:
    """How a claim's quote differs from the paper's wording, word by word.

    Compared on letters and digits, so case, punctuation and hyphenation drift
    do not show up as differences — those are not what a reviewer is looking
    for, and the quote check already forgives them.
    """
    mine = _WORD.findall(quote)
    theirs = _WORD.findall(paper)
    matcher = difflib.SequenceMatcher(
        None, [squash(w) for w in mine], [squash(w) for w in theirs], autojunk=False
    )
    parts: list[dict] = []

    def add(op: str, text: str) -> None:
        if not text:
            return
        if parts and parts[-1]["op"] == op:
            parts[-1]["text"] += text
        else:
            parts.append({"op": op, "text": text})

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            add("equal", "".join(theirs[j1:j2]))
        else:
            add("quote", "".join(mine[i1:i2]))
            add("paper", "".join(theirs[j1:j2]))
    # The word separators belong between the parts, not inside them: a trailing
    # space struck through or underlined is a mark on nothing.
    for part in parts:
        part["text"] = part["text"].strip()
    return [part for part in parts if part["text"]]


_LOCATOR_PAGE = re.compile(r"\b(?:p{1,2}\.?|pages?)\s*(\d{1,4})\b", re.IGNORECASE)


def locator_page(locator: str) -> int | None:
    """The page a locator names, if it names one. `Table 2` names none, and
    `pp. 4-5` names the first."""
    match = _LOCATOR_PAGE.search(locator or "")
    return int(match.group(1)) if match else None
