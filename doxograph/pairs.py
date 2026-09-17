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
import unicodedata
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

# Words that end in s without being plural. No rule tells `bias` from `areas`,
# so the ones this corpus actually uses are written down: without them `bias`
# stems to `bia` while `biases` stems to `bias`, and two claims about the same
# thing lose their heaviest shared word.
_SINGULAR_S = frozenset({
    "bias", "status", "virus", "corpus", "focus", "campus", "consensus", "apparatus",
    "bonus", "census", "genus", "lens", "nucleus", "radius", "stimulus", "surplus",
    "syllabus", "thesis", "axis", "basis", "crisis", "ellipsis", "emphasis", "gas",
})


# Plurals no suffix rule reaches. The singulars above keep their s, so their
# plurals have to arrive at the same word by name. `bases` is not here: it is
# the plural of `basis` and of `base` alike — nucleotide bases, a basis for a
# claim — and this corpus uses both.
_IRREGULAR = {
    "axes": "axis", "theses": "thesis", "ellipses": "ellipsis", "emphases": "emphasis",
    "matrices": "matrix", "indices": "index", "vertices": "vertex",
    "appendices": "appendix", "criteria": "criterion", "phenomena": "phenomenon",
    "corpora": "corpus", "foci": "focus", "lemmas": "lemma", "lemmata": "lemma",
    "nuclei": "nucleus", "radii": "radius", "stimuli": "stimulus",
    "syllabi": "syllabus", "genera": "genus", "censuses": "census",
    "apparatuses": "apparatus", "consensuses": "consensus", "bonuses": "bonus",
    "lenses": "lens", "surpluses": "surplus", "gases": "gas",
}


def stem(word: str) -> str:
    """A word with its commonest English endings off.

    Enough to bring `scales` and `scale`, or `steering` and `steer`, together.
    Not a stemmer: it leaves irregular words alone and does not care, since
    being wrong the same way for both claims still matches them to each other.
    A plural first, then a verb ending, so `scaled` and `scales` meet.
    """
    word = _IRREGULAR.get(word, word)
    if len(word) > 5 and word.endswith(("yses", "eses", "oses", "ises", "stases")):
        # analyses/analysis, hypotheses/hypothesis, metastases/metastasis.
        # Narrow on purpose: phases is a plural of phase, and turning it into
        # phasis would part the two, so `ases` is only read after an s.
        word = word[:-3] + "sis"
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        word = word[:-2]
    elif (len(word) > 3 and word.endswith("s") and not word.endswith("ss")
          and word not in _SINGULAR_S):
        word = word[:-1]
    if len(word) > 4 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ied"):
        # `studied` against `studies`: the plural rule above has already turned
        # the one into `study`, and the past tense has to arrive at the same y.
        word = word[:-3] + "y"
    elif len(word) > 3 and word.endswith("ed"):
        # Short stems too: `died` is four letters and `dies` becomes `di`, so
        # holding the line at five parted an ordinary pair of verb forms.
        word = word[:-2]
    # A silent e goes last, from every word alike, and so does a doubled final
    # letter. Only stripping them after a suffix would leave `scaled` as
    # `scal` beside `scale` and `controlled` as `controll` beside `control`;
    # taking them off both is wrong about the word and right about the pair,
    # which is all this measure is for.
    #
    # The double comes off either side of the e, since the two endings uncover
    # each other: `programme` needs the e gone before its m's are a pair, and
    # `agree` needs its e's read as a pair before one e goes, or `agreed` (an
    # `ed` short of `agre`) would end a letter shorter than `agree` does.
    word = _undoubled(word)
    if len(word) > 2 and word.endswith("e"):
        word = word[:-1]
    return _undoubled(word)


def _undoubled(word: str) -> str:
    """A word with a doubled final letter made single."""
    if len(word) > 3 and word[-1] == word[-2] and word[-1].isalpha():
        return word[:-1]
    return word


def words(row: dict) -> frozenset[str]:
    """The content words of a claim: its text and its evidence, stemmed.

    Evidence is in because it names the models, datasets and metrics, which is
    most of what decides whether two claims are about the same measurement.
    """
    found = set()
    for field in ("text", "evidence"):
        # Put together before it is cut up: an accent is one character in a
        # claim typed here and two in one pasted out of a PDF, and `\w+` reads
        # the second as a word ending where the accent starts. The same word
        # either way is the whole point of the measure.
        # A soft hyphen goes out with the same stroke: it says where a word
        # may be broken, not where one ends, and `\w+` would read it as the
        # end of one. The search over the papers reads it the same way.
        text = unicodedata.normalize("NFC", (row.get(field) or "")).casefold()
        text = text.replace("\u00ad", "")
        for word in _WORD.findall(text):
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
