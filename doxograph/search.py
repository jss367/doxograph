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
import os
import re
import threading
import unicodedata
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

# Folded paper text keyed by file, with the identity it was read at. These are
# the same files the quote checks write; without this every keystroke would
# read and fold the whole corpus again.
#
# Held to a size rather than a count of papers, since what costs is the
# characters. A search reads every paper in turn, so evicting the least
# recently used would throw away exactly what the next query asks for first:
# the oldest entry goes only when the budget is reached, and a corpus whose
# text does not fit pays the folding on every query however the cache is kept.
_texts: dict[Path, tuple[tuple[int, int], str]] = {}
_texts_lock = threading.Lock()
_texts_size = 0
try:
    TEXT_BUDGET = int(float(os.environ.get("DOXOGRAPH_TEXT_CACHE_MB", "64")) * 1_000_000)
except ValueError:
    TEXT_BUDGET = 64_000_000


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
    """`text` with the case and the accents taken out and its line-broken words
    put together.

    This is the form a query is matched against. `str.casefold` over the whole
    string would do the case half, but one character at a time is what
    `fold_with_offsets` needs, and the two must agree.
    """
    return fold_with_offsets(text)[0]


def fold_with_offsets(text: str) -> tuple[str, array.array]:
    """`fold(text)`, with the index in `text` each folded character came from.

    Folding is not one character in, one out: ß folds to ss, İ to an i and a
    combining dot, and a hyphen at a line break folds to nothing at all.
    Matching the folded forms against each other is the only way a search for
    STRASSE finds Straße, or one for transformation finds `transfor-\nmation`,
    and this is what carries a position in the folded text back to the paper's
    own characters.
    """
    # A word broken across a line by a hyphen is one word to a search, as it is
    # one word to `quotes.tidy` when a passage is read. The same pattern, so
    # the two cannot disagree about which words a paper contains.
    dropped = {at for match in quotes.LINE_HYPHEN.finditer(text)
               for at in range(match.start(), match.end())}
    folded: list[str] = []
    offsets = array.array("i")
    for at, char in enumerate(text):
        if at in dropped:
            continue
        for out in unicodedata.normalize("NFKD", char).casefold():
            # A PDF can give an accented letter whole or as a letter and a
            # mark, and a query is typed whichever way the keyboard does it.
            # Decomposing both and dropping the marks makes café and cafe
            # meet, as `quotes.squash` already has them meet for a quote.
            if unicodedata.combining(out):
                continue
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
    if quotes.paper_text(store.pdf_path(key), path, lambda: store.paper_lock(key)) is None:
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
    _remember(path, identity, text)
    return text


# Scripts that do not put spaces between their words. A word boundary means
# nothing inside a run of them — every character is a word character, so
# `\b模型` only ever matches at the start of a run — and a term in one of them
# is looked for wherever it falls.
_UNSEGMENTED = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\uac00-\ud7af\u0e00-\u0eff\u1780-\u17ff\u0f00-\u0fff\u1000-\u109f]"
)


def _length(text: str) -> int:
    """How long a paper is, for the ranking to discount it by.

    In word characters rather than words: Chinese and Japanese put no spaces
    between theirs, so a paper of ten thousand characters and one of ten would
    both be one word long and neither would be discounted at all. BM25 only
    ever compares a length against the average, so the unit is free.
    """
    return sum(len(word) for word in _WORD.findall(text)) or 1


def _remember(path: Path, identity: tuple[int, int], text: str) -> None:
    """Keep a folded paper, dropping the oldest until the budget is met."""
    global _texts_size
    with _texts_lock:
        stale = _texts.pop(path, None)
        if stale is not None:
            _texts_size -= len(stale[1])
        _texts[path] = (identity, text)
        _texts_size += len(text)
        while _texts_size > TEXT_BUDGET and len(_texts) > 1:
            # Insertion order: the oldest reading goes first.
            dropped = _texts.pop(next(iter(_texts)))
            _texts_size -= len(dropped[1])


def _pattern(term: str) -> re.Pattern:
    """A term matches from the start of a word, where words have starts.

    Both the term and the paper are folded before they meet, so case is
    already out of the question: STRASSE and Straße fold to the same letters,
    which `IGNORECASE` alone cannot do.
    """
    folded = re.escape(fold(term))
    if _UNSEGMENTED.search(term):
        return re.compile(folded, re.UNICODE)
    return re.compile(rf"\b{folded}\w*", re.UNICODE)


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
        length = _length(text)
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
    found = [[(m.start(), m.end()) for m in islice(pattern.finditer(text), PASSAGES)]
             for pattern in patterns]
    starts: list[tuple[int, int]] = []
    # A term at a time, so a query's second word is shown before the first
    # word's second occurrence.
    for nth in range(PASSAGES):
        for places in found:
            if len(starts) >= PASSAGES:
                break
            if nth < len(places) and not any(abs(places[nth][0] - at) < PASSAGE_SPAN
                                             for at, _ in starts):
                starts.append(places[nth])
    passages = []
    for folded_at, folded_end in sorted(starts)[:PASSAGES]:
        at = offsets[folded_at] if folded_at < len(offsets) else len(raw)
        stop = offsets[folded_end - 1] + 1 if folded_end - 1 < len(offsets) else len(raw)
        # Kept inside one page: text either side of a page break is the header
        # of the next page or the footer of this one, and a passage running
        # across the break would be on neither page it claims to be on. The
        # word that matched is the exception — split at the foot of a page, it
        # is one word, and a passage showing half of it shows nothing.
        low = raw.rfind(quotes.PAGE_BREAK, 0, at) + 1
        high = raw.find(quotes.PAGE_BREAK, max(stop - 1, at))
        high = len(raw) if high < 0 else high
        begin = max(low, at - PASSAGE_SPAN)
        end = max(min(high, at + PASSAGE_SPAN), stop)
        shown = quotes.tidy(raw[begin:end])
        passages.append({
            "text": shown,
            "page": raw.count(quotes.PAGE_BREAK, 0, at) + 1,
            # The passage already cut into pieces, the matched ones flagged.
            # Cut here because here is where the folding is known: a browser's
            # own case-insensitive matching cannot expand ß to ss, so it would
            # find nothing to mark in the passage that was found for it. Cut
            # rather than numbered because an offset into a Python string is
            # not an offset into a JavaScript one — anything outside the basic
            # plane counts once here and twice there.
            "parts": _parts(shown, patterns),
        })
    return passages


def _parts(shown: str, patterns: list[re.Pattern]) -> list[dict]:
    """A passage as alternating pieces: `{"text": ..., "mark": bool}`.

    The marked ones are where the terms are. Overlapping matches are merged,
    and a piece is only emitted when it has something in it.
    """
    folded, offsets = fold_with_offsets(shown)
    spans: list[list[int]] = []
    for pattern in patterns:
        for match in pattern.finditer(folded):
            begin = offsets[match.start()] if match.start() < len(offsets) else len(shown)
            end = offsets[match.end() - 1] + 1 if match.end() - 1 < len(offsets) else len(shown)
            # A mark the folding dropped belongs to the letter before it: the
            # accent on the last letter of a word is part of the word.
            while end < len(shown) and unicodedata.combining(shown[end]):
                end += 1
            spans.append([begin, end])
    spans.sort()
    merged: list[list[int]] = []
    for span in spans:
        if merged and span[0] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span[1])
        else:
            merged.append(span)
    parts: list[dict] = []
    last = 0
    for begin, end in merged:
        if begin > last:
            parts.append({"text": shown[last:begin], "mark": False})
        parts.append({"text": shown[begin:end], "mark": True})
        last = end
    if last < len(shown):
        parts.append({"text": shown[last:], "mark": False})
    return parts


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
