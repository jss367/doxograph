"""Check that a claim's quote is actually in the paper.

The extraction prompt insists that `quote` be verbatim, and nothing else in
the pipeline can tell whether the model obeyed. This can: the PDF's text is
compared against the quote on letters and digits alone, so line breaks,
hyphenation, ligatures and punctuation drift do not count against a quote
that is really there.
"""

from __future__ import annotations

import difflib
import threading
import unicodedata
from pathlib import Path

# Squashed page text is cached per file, since a paper is checked once per
# claim and text extraction is the slow part. Keyed on the file's identity so
# a replaced PDF is re-read.
_cache: dict[Path, tuple[tuple[int, int, int], str]] = {}
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
# How many places one anchor may be tried at. A run this long seldom recurs
# in one paper, so the cap is a guard against a degenerate input, not a
# working limit.
_MAX_SITES = 200


def squash(text: str) -> str:
    """Letters and digits only, in any script, lowercase; ligatures decomposed
    and accents dropped, since PDF extraction is unreliable on both."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in text.casefold() if ch.isalnum())


def pdf_text(path: Path) -> str | None:
    """The whole paper's text, squashed. None if it cannot be read, or if it
    has no text: a scanned paper opens fine but every page comes back empty,
    and there is nothing to check a quote against."""
    try:
        st = path.stat()
    except OSError:
        return None
    identity = (st.st_size, st.st_mtime_ns, st.st_ino)
    with _cache_lock:
        hit = _cache.get(path)
        if hit and hit[0] == identity:
            return hit[1] or None
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        text = squash("\n".join((page.extract_text() or "") for page in reader.pages))
    except Exception:
        return None
    with _cache_lock:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.pop(next(iter(_cache)))
        _cache[path] = (identity, text)
    return text or None


def coverage(quote: str, haystack: str) -> float:
    """How much of `quote` appears in `haystack`, both already squashed.

    1.0 for a substring. Otherwise the quote is lined up with the paper at
    every place one of its anchors occurs, and the best alignment's share of
    matching characters is returned.
    """
    if not quote:
        return 0.0
    if quote in haystack:
        return 1.0
    if len(quote) < _EXACT_BELOW:
        return 0.0
    # Full-length anchors first; if none of them lines the quote up well
    # enough, half-length ones, which survive an error at an anchor seam
    # in a quote near the minimum length. Both passes are capped the same.
    best = _align(quote, haystack, _ANCHOR)
    if best < _COVERAGE:
        best = max(best, _align(quote, haystack, _ANCHOR // 2))
    return best


def _align(quote: str, haystack: str, size: int) -> float:
    """The best coverage over every place an anchor of `size` characters
    from `quote` occurs in `haystack`."""
    n = len(quote)
    best = 0.0
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
                found = sum(block.size for block in matcher.get_matching_blocks())
                best = max(best, found / n)
                if best >= 1.0:
                    return best
            at = haystack.find(anchor, at + 1)
    return best


def verify(pdf: Path, quote: str) -> bool | None:
    """Whether `quote` is in the paper at `pdf`.

    None when there is nothing to check: no quote, or no readable PDF.
    """
    needle = squash(quote)
    if not needle:
        return None
    haystack = pdf_text(pdf)
    if haystack is None:
        return None
    return coverage(needle, haystack) >= _COVERAGE
