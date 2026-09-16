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

import array
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
    """The words of a query, in order and without repeats, as they were typed.

    Kept as typed so the passages can quote them back; the matching itself is
    done on the folded form of both sides, by `fold`.
    """
    seen: dict[str, str] = {}
    for word in _WORD.findall(query or ""):
        seen.setdefault(word.casefold(), word)
    return list(seen.values())


def fold(text: str) -> str:
    """`text` with the case taken out of it, character by character.

    `str.casefold` over the whole string would do the same, but one character
    at a time is what `fold_with_offsets` needs and the two must agree.
    """
    return "".join(char.casefold() for char in text)


def fold_with_offsets(text: str) -> tuple[str, array.array]:
    """`fold(text)`, with the index in `text` each folded character came from.

    Folding is not one character in, one out: ß folds to ss, İ to an i and a
    combining dot. Matching the folded forms against each other is the only
    way a search for STRASSE finds Straße, and this is what carries a position
    in the folded text back to the paper's own characters.
    """
    folded: list[str] = []
    offsets = array.array("i")
    for at, char in enumerate(text):
        for out in char.casefold():
            folded.append(out)
            offsets.append(at)
    return "".join(folded), offsets


def _stored(key: str):
    """The file holding a paper's text and its identity, extracting it if this
    is the first time anything has asked. None when there is no text to read."""
    path = store.text_path(key)
    try:
        return path, path.stat()
    except OSError:
        pass
    if quotes.paper_text(store.pdf_path(key), path) is None:
        return None
    try:
        return path, path.stat()
    except OSError:
        return None


def paper_text(key: str) -> str | None:
    """A paper's text as the PDF has it. None when there is none to read.

    Read from the file the quote checks write, without keeping it: what a
    search holds on to is the folded form below, and the only callers of this
    one want a handful of papers at a time.
    """
    stored = _stored(key)
    if stored is None:
        return None
    try:
        return stored[0].read_text(encoding="utf-8")
    except OSError:
        return None


def folded_text(key: str) -> str | None:
    """A paper's text with the case taken out, which is what a query meets.

    Kept between queries: a search reads the whole corpus, and folding it on
    every keystroke is the expensive half.
    """
    stored = _stored(key)
    if stored is None:
        return None
    path, st = stored
    identity = (st.st_size, st.st_mtime_ns)
    with _texts_lock:
        hit = _texts.get(path)
        if hit and hit[0] == identity:
            return hit[1]
    try:
        text = fold(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    with _texts_lock:
        if len(_texts) >= _TEXT_LIMIT:
            _texts.pop(next(iter(_texts)))
        _texts[path] = (identity, text)
    return text


def _pattern(term: str) -> re.Pattern:
    """A term matches from the start of a word. Both the term and the paper are
    folded before they meet, so case is already out of the question: STRASSE
    and Straße fold to the same letters, which `IGNORECASE` alone cannot do."""
    return re.compile(rf"\b{re.escape(fold(term))}\w*", re.UNICODE)


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
    # term are measured too: how rare a term is across the whole pile, and how
    # long a paper is against the whole pile, are both what they are whether or
    # not the paper matched. Averaging the length over the hits alone would let
    # the ranking depend on which papers happened to match.
    matched: list[str] = []
    counts: dict[str, list[int]] = {}
    lengths: dict[str, int] = {}
    seen = [0] * len(wanted)
    total = 0
    corpus = 0
    for key in store.paper_keys():
        text = folded_text(key)
        if not text:
            continue
        corpus += 1
        length = len(_WORD.findall(text)) or 1
        total += length
        found = [len(pattern.findall(text)) for pattern in patterns]
        for i, count in enumerate(found):
            if count:
                seen[i] += 1
        if not all(found):
            continue
        matched.append(key)
        counts[key] = found
        lengths[key] = length
    if not matched:
        return []

    average = total / corpus
    idf = [math.log(1 + (corpus - df + 0.5) / (df + 0.5)) for df in seen]
    ranked = sorted(
        (
            (-sum(weight * (count * (K1 + 1))
                  / (count + K1 * (1 - B + B * lengths[key] / average))
                  for weight, count in zip(idf, counts[key])), key)
            for key in matched
        )
    )
    # The paper's own text is read again for the few that are shown, rather
    # than every folded corpus being kept beside its original all session.
    return [
        {
            "key": key,
            "score": round(-score, 3),
            "occurrences": sum(counts[key]),
            "passages": _passages(paper_text(key) or "", patterns),
        }
        for score, key in ranked[:limit]
    ]


def _passages(raw: str, patterns: list[re.Pattern]) -> list[dict]:
    """A few places in the paper where the terms are, in the paper's own words.

    The earliest occurrences, dropping one that lands inside a passage already
    shown, so two terms in the same sentence are one passage and not two. The
    terms are found in the folded text and read back out of the original.
    """
    text, offsets = fold_with_offsets(raw)
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
    for folded_at in sorted(starts)[:PASSAGES]:
        at = offsets[folded_at] if folded_at < len(offsets) else len(raw)
        # Kept inside one page: text either side of a page break is the header
        # of the next page or the footer of this one, and a passage running
        # across the break would be on neither page it claims to be on.
        low = raw.rfind(quotes.PAGE_BREAK, 0, at) + 1
        high = raw.find(quotes.PAGE_BREAK, at)
        high = len(raw) if high < 0 else high
        begin = max(low, at - PASSAGE_SPAN)
        end = min(high, at + PASSAGE_SPAN)
        passages.append({
            "text": quotes.tidy(raw[begin:end]),
            "page": raw.count(quotes.PAGE_BREAK, 0, at) + 1,
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
