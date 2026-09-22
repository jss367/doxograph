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
_texts: dict[Path, tuple[tuple[int, int, int], str]] = {}
_texts_lock = threading.Lock()
_texts_size = 0
try:
    TEXT_BUDGET = int(float(os.environ.get("DOXOGRAPH_TEXT_CACHE_MB", "64")) * 1_000_000)
except ValueError:
    TEXT_BUDGET = 64_000_000


def _query_words(text: str):
    r"""The words of a query, counting a mark as part of the word it sits on.

    `\w` does not match a combining mark, and in a script whose vowels are
    marks that cuts one written word into its consonants — किताब into three
    terms, each of which any paper might hold. Done a character at a time
    rather than by pattern because the categories are what say which is
    which, and a query is short enough not to care.
    """
    word: list[str] = []
    for char in text:
        if char.isalnum() or char == "_" or unicodedata.category(char)[0] == "M":
            word.append(char)
        elif word:
            yield "".join(word)
            word = []
    if word:
        yield "".join(word)


def terms(query: str) -> list[str]:
    """The words of a query, folded, in order and without repeats.

    Folded before it is cut into words, not after: a combining mark is no part
    of a word, so a query typed as `nai` + a diaeresis + `ve` would otherwise
    be two terms where `naïve` is one, and neither of them would find the word
    the reader meant. Folding is what the papers are matched against anyway,
    and one word spelled two ways — `café cafe` — is one term, or a single
    mention would be counted twice and weighed twice in the ranking.
    """
    seen: dict[str, None] = {}
    for word in _query_words(fold(query or "")):
        seen.setdefault(word, None)
    return list(seen)


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
        # A soft hyphen is where a word may be broken, not a letter of it, and
        # a PDF can put one in a word it did not end up breaking. The pattern
        # above only reaches the ones a line break follows.
        if at in dropped or char == "\u00ad":
            continue
        for out in unicodedata.normalize("NFKD", char).casefold():
            # A PDF can give an accented letter whole or as a letter and a
            # mark, and a query is typed whichever way the keyboard does it.
            # Decomposing both and dropping the marks makes café and cafe
            # meet, as `quotes.squash` already has them meet for a quote.
            if unicodedata.combining(out) not in _KEEP_COMBINING:
                continue
            folded.append(out)
            offsets.append(at)
    return "".join(folded), offsets


def _stored(key: str):
    """The file holding a paper's text and its identity, reading the PDF when
    there is no text or the text is older than it. None when there is nothing
    to read.

    The age check is the one `quotes.paper_text` makes, and it is repeated
    here because a text file can outlive the PDF it came from: a paper
    replaced or restored by hand leaves the old text in place, and a search
    would go on answering out of the paper that used to be there.
    """
    path = store.text_path(key)
    stored = None
    try:
        stored = path.stat()
    except OSError:
        pass
    try:
        fresh = stored is not None and stored.st_mtime_ns > store.pdf_path(key).stat().st_mtime_ns
    except OSError:
        fresh = stored is not None      # no PDF for it to be older than
    if not fresh:
        if quotes.paper_text(store.pdf_path(key), path, lambda: store.paper_lock(key)) is None:
            return None
        try:
            stored = path.stat()
        except OSError:
            return None
    return path, stored


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
    identity = (st.st_size, st.st_mtime_ns, st.st_ino)
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
# Combining classes that are not accents: a nukta, a kana voicing mark and a
# virama each change the word rather than decorate it — क्ल is not कल, and が
# is not か — so they stay where an acute or an Arabic fatha comes off. Zero is
# in here because a mark with no class is not reordered and not an accent
# either, which is what keeps a Devanagari vowel sign.
_KEEP_COMBINING = frozenset({0, 7, 8, 9})

_UNSEGMENTED = re.compile(
    # Scripts written without spaces between words. A list rather than a rule,
    # because Unicode does not say which scripts these are; the ones a corpus
    # might hold are here — CJK and kana, Hangul, Thai, Lao, Khmer, Burmese,
    # Tibetan, Javanese, Balinese, Sundanese, Tai Tham, Cham. Hangul twice:
    # as syllables, and as the jamo folding takes each syllable apart into,
    # since a term is only ever looked at folded.
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\uac00-\ud7af\u1100-\u11ff\u0e00-\u0eff\u1780-\u17ff\u0f00-\u0fff\u1000-\u109f"
    r"\ua980-\ua9df\u1b00-\u1b7f\u1b80-\u1bbf\u1a20-\u1aaf\uaa00-\uaa5f]"
)


def _length(text: str) -> int:
    """How long a paper is, for the ranking to discount it by.

    In word characters rather than words: Chinese and Japanese put no spaces
    between theirs, so a paper of ten thousand characters and one of ten would
    both be one word long and neither would be discounted at all. BM25 only
    ever compares a length against the average, so the unit is free.
    """
    # By pattern rather than character by character: this runs over every
    # paper in the corpus, and a mark counted or not counted is a rounding
    # error in a length.
    return sum(len(word) for word in _WORD.findall(text)) or 1


def _remember(path: Path, identity: tuple[int, int, int], text: str) -> None:
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


# What counts as part of a word. `\w` leaves out the marks, and the folding
# keeps the ones that change a word rather than decorate it — a vowel sign, a
# virama — so without them here a word boundary falls inside किताब and a
# search for ताब finds it. Written out because `re` has no `\p{M}`; every
# code point whose category begins with M:
#   python -c "import unicodedata; print([c for c in map(chr, range(0x110000))
#              if unicodedata.category(c)[0] == 'M'])"
_MARKS = "\u0300-\u036f\u0483-\u0489\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed\u0711\u0730-\u074a\u07a6-\u07b0\u07eb-\u07f3\u07fd\u0816-\u0819\u081b-\u0823\u0825-\u0827\u0829-\u082d\u0859-\u085b\u0897-\u089f\u08ca-\u08e1\u08e3-\u0903\u093a-\u093c\u093e-\u094f\u0951-\u0957\u0962-\u0963\u0981-\u0983\u09bc\u09be-\u09c4\u09c7-\u09c8\u09cb-\u09cd\u09d7\u09e2-\u09e3\u09fe\u0a01-\u0a03\u0a3c\u0a3e-\u0a42\u0a47-\u0a48\u0a4b-\u0a4d\u0a51\u0a70-\u0a71\u0a75\u0a81-\u0a83\u0abc\u0abe-\u0ac5\u0ac7-\u0ac9\u0acb-\u0acd\u0ae2-\u0ae3\u0afa-\u0aff\u0b01-\u0b03\u0b3c\u0b3e-\u0b44\u0b47-\u0b48\u0b4b-\u0b4d\u0b55-\u0b57\u0b62-\u0b63\u0b82\u0bbe-\u0bc2\u0bc6-\u0bc8\u0bca-\u0bcd\u0bd7\u0c00-\u0c04\u0c3c\u0c3e-\u0c44\u0c46-\u0c48\u0c4a-\u0c4d\u0c55-\u0c56\u0c62-\u0c63\u0c81-\u0c83\u0cbc\u0cbe-\u0cc4\u0cc6-\u0cc8\u0cca-\u0ccd\u0cd5-\u0cd6\u0ce2-\u0ce3\u0cf3\u0d00-\u0d03\u0d3b-\u0d3c\u0d3e-\u0d44\u0d46-\u0d48\u0d4a-\u0d4d\u0d57\u0d62-\u0d63\u0d81-\u0d83\u0dca\u0dcf-\u0dd4\u0dd6\u0dd8-\u0ddf\u0df2-\u0df3\u0e31\u0e34-\u0e3a\u0e47-\u0e4e\u0eb1\u0eb4-\u0ebc\u0ec8-\u0ece\u0f18-\u0f19\u0f35\u0f37\u0f39\u0f3e-\u0f3f\u0f71-\u0f84\u0f86-\u0f87\u0f8d-\u0f97\u0f99-\u0fbc\u0fc6\u102b-\u103e\u1056-\u1059\u105e-\u1060\u1062-\u1064\u1067-\u106d\u1071-\u1074\u1082-\u108d\u108f\u109a-\u109d\u135d-\u135f\u1712-\u1715\u1732-\u1734\u1752-\u1753\u1772-\u1773\u17b4-\u17d3\u17dd\u180b-\u180d\u180f\u1885-\u1886\u18a9\u1920-\u192b\u1930-\u193b\u1a17-\u1a1b\u1a55-\u1a5e\u1a60-\u1a7c\u1a7f\u1ab0-\u1ace\u1b00-\u1b04\u1b34-\u1b44\u1b6b-\u1b73\u1b80-\u1b82\u1ba1-\u1bad\u1be6-\u1bf3\u1c24-\u1c37\u1cd0-\u1cd2\u1cd4-\u1ce8\u1ced\u1cf4\u1cf7-\u1cf9\u1dc0-\u1dff\u20d0-\u20f0\u2cef-\u2cf1\u2d7f\u2de0-\u2dff\u302a-\u302f\u3099-\u309a\ua66f-\ua672\ua674-\ua67d\ua69e-\ua69f\ua6f0-\ua6f1\ua802\ua806\ua80b\ua823-\ua827\ua82c\ua880-\ua881\ua8b4-\ua8c5\ua8e0-\ua8f1\ua8ff\ua926-\ua92d\ua947-\ua953\ua980-\ua983\ua9b3-\ua9c0\ua9e5\uaa29-\uaa36\uaa43\uaa4c-\uaa4d\uaa7b-\uaa7d\uaab0\uaab2-\uaab4\uaab7-\uaab8\uaabe-\uaabf\uaac1\uaaeb-\uaaef\uaaf5-\uaaf6\uabe3-\uabea\uabec-\uabed\ufb1e\ufe00-\ufe0f\ufe20-\ufe2f\U000101fd\U000102e0\U00010376-\U0001037a\U00010a01-\U00010a03\U00010a05-\U00010a06\U00010a0c-\U00010a0f\U00010a38-\U00010a3a\U00010a3f\U00010ae5-\U00010ae6\U00010d24-\U00010d27\U00010d69-\U00010d6d\U00010eab-\U00010eac\U00010efc-\U00010eff\U00010f46-\U00010f50\U00010f82-\U00010f85\U00011000-\U00011002\U00011038-\U00011046\U00011070\U00011073-\U00011074\U0001107f-\U00011082\U000110b0-\U000110ba\U000110c2\U00011100-\U00011102\U00011127-\U00011134\U00011145-\U00011146\U00011173\U00011180-\U00011182\U000111b3-\U000111c0\U000111c9-\U000111cc\U000111ce-\U000111cf\U0001122c-\U00011237\U0001123e\U00011241\U000112df-\U000112ea\U00011300-\U00011303\U0001133b-\U0001133c\U0001133e-\U00011344\U00011347-\U00011348\U0001134b-\U0001134d\U00011357\U00011362-\U00011363\U00011366-\U0001136c\U00011370-\U00011374\U000113b8-\U000113c0\U000113c2\U000113c5\U000113c7-\U000113ca\U000113cc-\U000113d0\U000113d2\U000113e1-\U000113e2\U00011435-\U00011446\U0001145e\U000114b0-\U000114c3\U000115af-\U000115b5\U000115b8-\U000115c0\U000115dc-\U000115dd\U00011630-\U00011640\U000116ab-\U000116b7\U0001171d-\U0001172b\U0001182c-\U0001183a\U00011930-\U00011935\U00011937-\U00011938\U0001193b-\U0001193e\U00011940\U00011942-\U00011943\U000119d1-\U000119d7\U000119da-\U000119e0\U000119e4\U00011a01-\U00011a0a\U00011a33-\U00011a39\U00011a3b-\U00011a3e\U00011a47\U00011a51-\U00011a5b\U00011a8a-\U00011a99\U00011c2f-\U00011c36\U00011c38-\U00011c3f\U00011c92-\U00011ca7\U00011ca9-\U00011cb6\U00011d31-\U00011d36\U00011d3a\U00011d3c-\U00011d3d\U00011d3f-\U00011d45\U00011d47\U00011d8a-\U00011d8e\U00011d90-\U00011d91\U00011d93-\U00011d97\U00011ef3-\U00011ef6\U00011f00-\U00011f01\U00011f03\U00011f34-\U00011f3a\U00011f3e-\U00011f42\U00011f5a\U00013440\U00013447-\U00013455\U0001611e-\U0001612f\U00016af0-\U00016af4\U00016b30-\U00016b36\U00016f4f\U00016f51-\U00016f87\U00016f8f-\U00016f92\U00016fe4\U00016ff0-\U00016ff1\U0001bc9d-\U0001bc9e\U0001cf00-\U0001cf2d\U0001cf30-\U0001cf46\U0001d165-\U0001d169\U0001d16d-\U0001d172\U0001d17b-\U0001d182\U0001d185-\U0001d18b\U0001d1aa-\U0001d1ad\U0001d242-\U0001d244\U0001da00-\U0001da36\U0001da3b-\U0001da6c\U0001da75\U0001da84\U0001da9b-\U0001da9f\U0001daa1-\U0001daaf\U0001e000-\U0001e006\U0001e008-\U0001e018\U0001e01b-\U0001e021\U0001e023-\U0001e024\U0001e026-\U0001e02a\U0001e08f\U0001e130-\U0001e136\U0001e2ae\U0001e2ec-\U0001e2ef\U0001e4ec-\U0001e4ef\U0001e5ee-\U0001e5ef\U0001e8d0-\U0001e8d6\U0001e944-\U0001e94a\U000e0100-\U000e01ef"
_IN_WORD = rf"[\w{_MARKS}]"
_IN_WORD_CHAR = re.compile(_IN_WORD)


def _pattern(term: str) -> re.Pattern:
    """A term matches from the start of a word, where words have starts.

    Both the term and the paper are folded before they meet, so case is
    already out of the question: STRASSE and Straße fold to the same letters,
    which `IGNORECASE` alone cannot do.

    The term itself is group 1, which is what every caller measures and marks.
    In a script that writes without spaces the pattern is a lookahead, so a
    term can meet itself: 哈哈 occurs twice in 哈哈哈, and a match that ate the
    first two characters would find one occurrence and rank the paper as
    though it had said the word once.
    """
    folded = re.escape(fold(term))
    if _UNSEGMENTED.search(term):
        return re.compile(rf"(?=({folded}))", re.UNICODE)
    return re.compile(rf"(?<!{_IN_WORD})({folded}{_IN_WORD}*)", re.UNICODE)


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
    # Every match, lazily: the ones near each other collapse into one passage,
    # so a term that occurs twenty times in one paragraph and once on a later
    # page needs the twenty-first to say anything new. Taken one at a time, and
    # only as far as the passages need.
    found = [pattern.finditer(text) for pattern in patterns]
    taken: list[list[tuple[int, int]]] = [[] for _ in patterns]

    def place(which: int, nth: int):
        """The `nth` match of a term, or None once they run out."""
        while len(taken[which]) <= nth:
            match = next(found[which], None)
            if match is None:
                return None
            taken[which].append(match.span(1))
        return taken[which][nth]
    starts: list[tuple[int, int]] = []

    def page_of(folded_at: int) -> int:
        at = offsets[folded_at] if folded_at < len(offsets) else len(raw)
        return raw.count(quotes.PAGE_BREAK, 0, at)

    # A term at a time, so a query's second word is shown before the first
    # word's second occurrence. Near enough to be one passage means near
    # enough and on the same page: a passage is cut to one page, so two terms
    # either side of a break cannot both be shown in one.
    nth = 0
    while len(starts) < PASSAGES:
        exhausted = True
        for which in range(len(patterns)):
            if len(starts) >= PASSAGES:
                break
            span = place(which, nth)
            if span is None:
                continue
            exhausted = False
            where, ends = span
            # Shown already means shown whole: a passage reaches PASSAGE_SPAN
            # past where it starts, and a match beginning inside that but
            # running past the end would be cut in half — `transfor` of
            # `transformation`, with nothing to mark and one of the query's
            # words nowhere in the answer.
            if any(at - PASSAGE_SPAN <= where and ends <= at + PASSAGE_SPAN
                   and page_of(where) == page_of(at) for at, _ in starts):
                continue
            starts.append(span)
        if exhausted:
            break
        nth += 1
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
        # Where the matched word itself runs over a break, the passage ends
        # with the word: searching on from there would find the next page's
        # break and read a page the passage does not claim to be on.
        crosses = raw.find(quotes.PAGE_BREAK, at, stop) >= 0
        high = stop if crosses else raw.find(quotes.PAGE_BREAK, max(stop - 1, at))
        high = len(raw) if high < 0 else high
        begin = max(low, at - PASSAGE_SPAN)
        end = max(min(high, at + PASSAGE_SPAN), stop)
        # And cut between words, not inside one: a passage opening on the
        # `linear` of `nonlinear` reads as a word the paper never wrote, and
        # a search for `linear` marks it. Not in a script without spaces,
        # where every character is a word character and any of them will do.
        while (low < begin < at and _inside(raw, begin)
               and not _UNSEGMENTED.match(raw[begin])):
            begin += 1
        while (stop < end < high and _inside(raw, end)
               and not _UNSEGMENTED.match(raw[end - 1])):
            end -= 1
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


def _inside(raw: str, at: int) -> bool:
    """Whether `at` falls between two characters of one word."""
    return bool(_IN_WORD_CHAR.match(raw[at - 1]) and _IN_WORD_CHAR.match(raw[at]))


def _parts(shown: str, patterns: list[re.Pattern]) -> list[dict]:
    """A passage as alternating pieces: `{"text": ..., "mark": bool}`.

    The marked ones are where the terms are. Overlapping matches are merged,
    and a piece is only emitted when it has something in it.
    """
    folded, offsets = fold_with_offsets(shown)
    spans: list[list[int]] = []
    for pattern in patterns:
        for match in pattern.finditer(folded):
            at, stop = match.span(1)
            begin = offsets[at] if at < len(offsets) else len(shown)
            end = offsets[stop - 1] + 1 if stop - 1 < len(offsets) else len(shown)
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
