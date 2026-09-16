"""Search the papers' own text, with no model and no network.

The claims are searched in the browser, where they already are. This searches
what the claims were drawn from: the text extracted for the quote checks, one
file per paper under `text/`. It answers the question the claim search cannot
— "does anything in my pile mention this at all" — for a word nobody wrote a
claim about, or a paper nothing has been extracted from yet.

A paper has to hold every term to be a hit. The ones that do are ranked by
BM25, which is the standard answer to "this term appears twice in a long paper
and twice in a short one; which is more about it". A term matches at the start
of a word, so `steer` finds steering and steered.
"""

from __future__ import annotations

import math
import re
import threading
from itertools import islice
from pathlib import Path

from . import config, quotes, store

# BM25's usual constants: `K1` is how quickly repeated terms stop adding to a
# score, `B` how much a long paper is discounted for its length.
K1 = 1.5
B = 0.75

# How many places in a paper to show, and how much of the paper around each.
PASSAGES = 2
PASSAGE_SPAN = 140

_WORD = re.compile(r"\w+", re.UNICODE)

# Paper text keyed by file, with the identity it was read at. These are the
# same files the quote checks write; without this every keystroke would read
# the whole corpus off the disk again.
_texts: dict[Path, tuple[tuple[int, int], str]] = {}
_texts_lock = threading.Lock()
_TEXT_LIMIT = 400


def terms(query: str) -> list[str]:
    """The words of a query, lowercased, in order and without repeats."""
    seen: dict[str, None] = {}
    for word in _WORD.findall((query or "").casefold()):
        seen.setdefault(word, None)
    return list(seen)


def paper_text(key: str) -> str | None:
    """A paper's text, or None if there is none to search.

    Read from the cache the quote checks write. A paper whose text has never
    been extracted — one added before this existed, or one nothing has been
    read from yet — is extracted here, once.
    """
    path = store.text_path(key)
    try:
        st = path.stat()
    except OSError:
        if quotes.paper_text(store.pdf_path(key), path) is None:
            return None
        try:
            st = path.stat()
        except OSError:
            return None
    identity = (st.st_size, st.st_mtime_ns)
    with _texts_lock:
        hit = _texts.get(path)
        if hit and hit[0] == identity:
            return hit[1]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    with _texts_lock:
        if len(_texts) >= _TEXT_LIMIT:
            _texts.pop(next(iter(_texts)))
        _texts[path] = (identity, text)
    return text


def _pattern(term: str) -> re.Pattern:
    """A term matches from the start of a word, whatever its case."""
    return re.compile(rf"\b{re.escape(term)}\w*", re.IGNORECASE | re.UNICODE)


def search_papers(query: str, limit: int = 20) -> list[dict]:
    """The papers whose text holds every term, best first.

    Each hit carries where the terms are: a couple of passages with the page
    they are on, as the paper writes them.
    """
    wanted = terms(query)
    if not wanted:
        return []
    patterns = [_pattern(term) for term in wanted]

    # One pass over the corpus for everything a ranking needs. Papers missing a
    # term are still counted, since how rare a term is across the whole pile is
    # what decides how much it is worth.
    texts: dict[str, str] = {}
    counts: dict[str, list[int]] = {}
    lengths: dict[str, int] = {}
    seen = [0] * len(wanted)
    corpus = 0
    for key in store.paper_keys():
        text = paper_text(key)
        if not text:
            continue
        corpus += 1
        found = [len(pattern.findall(text)) for pattern in patterns]
        for i, count in enumerate(found):
            if count:
                seen[i] += 1
        if not all(found):
            continue
        texts[key] = text
        counts[key] = found
        lengths[key] = len(_WORD.findall(text)) or 1
    if not texts:
        return []

    average = sum(lengths.values()) / len(lengths)
    idf = [math.log(1 + (corpus - df + 0.5) / (df + 0.5)) for df in seen]
    ranked = sorted(
        (
            (-sum(weight * (count * (K1 + 1))
                  / (count + K1 * (1 - B + B * lengths[key] / average))
                  for weight, count in zip(idf, counts[key])), key)
            for key in texts
        )
    )
    return [
        {
            "key": key,
            "score": round(-score, 3),
            "occurrences": sum(counts[key]),
            "passages": _passages(texts[key], patterns),
        }
        for score, key in ranked[:limit]
    ]


def _passages(text: str, patterns: list[re.Pattern]) -> list[dict]:
    """A few places in the paper where the terms are, in the paper's own words.

    The earliest occurrences, dropping one that lands inside a passage already
    shown, so two terms in the same sentence are one passage and not two.
    """
    found = [[m.start() for m in islice(pattern.finditer(text), PASSAGES)]
             for pattern in patterns]
    starts: list[int] = []
    # A term at a time, so a query's second word is shown before the first
    # word's second occurrence.
    for nth in range(PASSAGES):
        for places in found:
            if len(starts) >= PASSAGES:
                break
            if nth < len(places) and not any(abs(places[nth] - at) < PASSAGE_SPAN for at in starts):
                starts.append(places[nth])
    passages = []
    for at in sorted(starts)[:PASSAGES]:
        # Kept inside one page: text either side of a page break is the header
        # of the next page or the footer of this one, and a passage running
        # across the break would be on neither page it claims to be on.
        low = text.rfind(quotes.PAGE_BREAK, 0, at) + 1
        high = text.find(quotes.PAGE_BREAK, at)
        high = len(text) if high < 0 else high
        begin = max(low, at - PASSAGE_SPAN)
        end = min(high, at + PASSAGE_SPAN)
        passages.append({
            "text": quotes.tidy(text[begin:end]),
            "page": text.count(quotes.PAGE_BREAK, 0, at) + 1,
        })
    return passages


def cache_text(key: str) -> None:
    """Extract a paper's text now, so the first search does not have to."""
    quotes.paper_text(store.pdf_path(key), store.text_path(key))


def warm() -> int:
    """Extract the text of every paper that has none stored. Returns how many."""
    config.ensure_dirs()
    done = 0
    for key in store.paper_keys():
        if store.text_path(key).exists() or not store.pdf_path(key).exists():
            continue
        cache_text(key)
        done += 1
    return done
