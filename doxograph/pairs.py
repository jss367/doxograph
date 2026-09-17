"""Which claims resemble which, by their words alone.

A claim is the set of content words in its text and evidence, each weighted by
how rare it is across the corpus, and two claims resemble each other when those
sets overlap. No model, no network: this runs over the claims already on disk.

It is a reading aid, not a filter. Claims that resemble each other are worth
looking at side by side, and that is all this is used for — what the tensions
and agreements passes are shown is decided by the topic, not by this. Two
claims can contradict each other in words that have nothing in common, and
that is exactly the pair you would not want a word count to hide.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Iterable

from .store import STOPWORDS

# How much overlap a pair needs before it is worth showing. Low on purpose:
# claims phrase the same measurement in very different ways, and a suggestion
# the reader can dismiss at a glance costs less than one that never appears.
try:
    FLOOR = float(os.environ.get("DOXOGRAPH_PAIR_FLOOR", "0.08"))
except ValueError:
    FLOOR = 0.08

_WORD = re.compile(r"\w+", re.UNICODE)


def stem(word: str) -> str:
    """A word with its commonest English endings off.

    Enough to bring `scales` and `scale`, or `steering` and `steer`, together.
    Not a stemmer: it leaves irregular words alone and does not care, since
    being wrong the same way for both claims still matches them to each other.
    A plural first, then a verb ending, so `scaled` and `scales` meet.
    """
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ied"):
        # `studied` against `studies`: the plural rule above has already turned
        # the one into `study`, and the past tense has to arrive at the same y.
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith("ed"):
        word = word[:-2]
    # A silent e goes last, from every word alike, and so does a doubled final
    # consonant. Only stripping them after a suffix would leave `scaled` as
    # `scal` beside `scale` and `controlled` as `controll` beside `control`;
    # taking them off both is wrong about the word and right about the pair,
    # which is all this measure is for.
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    if len(word) > 3 and word[-1] == word[-2] and word[-1].isalpha():
        word = word[:-1]
    return word


def words(row: dict) -> frozenset[str]:
    """The content words of a claim: its text and its evidence, stemmed.

    Evidence is in because it names the models, datasets and metrics, which is
    most of what decides whether two claims are about the same measurement.
    """
    found = set()
    for field in ("text", "evidence"):
        for word in _WORD.findall((row.get(field) or "").casefold()):
            if len(word) < 2 or word in STOPWORDS:
                continue
            found.add(stem(word))
    return frozenset(found)


def weights(rows: Iterable[dict]) -> dict[str, float]:
    """How much each word counts: rarer across the corpus, worth more.

    Over the whole corpus rather than one topic, so a word that is everywhere
    — the topic's own name, most of all — weighs next to nothing wherever it
    turns up.
    """
    counts: dict[str, int] = {}
    total = 0
    for row in rows:
        total += 1
        for word in words(row):
            counts[word] = counts.get(word, 0) + 1
    return {word: math.log(1 + total / count) for word, count in counts.items()}


def _norm(bag: frozenset[str], weight: dict[str, float]) -> float:
    return math.sqrt(sum(weight.get(word, 1.0) ** 2 for word in bag)) or 1.0


def similarity(a: frozenset[str], b: frozenset[str], weight: dict[str, float]) -> float:
    """How much two claims have in common, from 0 to 1.

    The cosine of their word sets under the weights: the shared words' weight
    against the size of both claims, so a long claim is not comparable to
    everything just for having many words.
    """
    shared = a & b
    if not shared:
        return 0.0
    return sum(weight.get(word, 1.0) ** 2 for word in shared) / (_norm(a, weight) * _norm(b, weight))


def similar_to(claim_id: str, rows: list[dict], weight: dict[str, float] | None = None,
               limit: int = 5, floor: float = FLOOR) -> list[dict]:
    """The claims from other papers that most resemble one claim.

    This is a suggestion and nothing more. It does not decide what the tensions
    and agreements passes are shown: two claims can disagree in words that have
    nothing in common — "recovers in 46% of rollouts" against "almost never
    returns to the task" — and a filter that kept only the pairs this finds
    would throw those away without anyone knowing.
    """
    weight = weights(rows) if weight is None else weight
    mine = next((row for row in rows if row["id"] == claim_id), None)
    if mine is None:
        raise KeyError(claim_id)
    bag = words(mine)
    scored = []
    for row in rows:
        if row["id"] == claim_id or row.get("paper") == mine.get("paper"):
            continue
        score = similarity(bag, words(row), weight)
        if score >= floor:
            scored.append({"claim": row["id"], "score": round(score, 3)})
    scored.sort(key=lambda row: (-row["score"], row["claim"]))
    return scored[:limit]
