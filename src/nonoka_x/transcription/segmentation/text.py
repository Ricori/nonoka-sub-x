"""The few text helpers the sentence DP and the timeline contract use.

Copied from FineSub `finesub/text.py` (v0.7.0 @ 7123055) unchanged: `normalized_compact`, `SENTENCE_END_PUNCT`,
`CLAUSE_PUNCT`, `punct_class`, `coerce_optional_float`, `min_word_confidence`, `words_to_text`. Nonoka maintains
this copy (`docs/transcription-engines-plan.md` 4.1 item 5); FineSub's original is left as it is.
"""

from __future__ import annotations

import unicodedata
from typing import Dict, List, Optional


def normalized_compact(text: str) -> str:
    """NFKC + casefold, punctuation and whitespace stripped.

    The shared comparison key for hallucination-phrase matching, ghost
    duplicate detection and second-model evidence."""

    return "".join(
        char.casefold()
        for char in unicodedata.normalize("NFKC", str(text))
        if not unicodedata.category(char).startswith("P") and not char.isspace()
    )


# --------- Punctuation classification (split-boundary semantics) ---------
# Shared, direction-aware classification for subtitle split decisions: a cut
# between two words inspects the previous word's trailing character and the
# next word's leading character. Classes:
#   "sentence" — sentence-final (。．｡.！!？?…‥‼⁇⁈⁉): strongest break after.
#   "clause"   — clause-level pause (、，,､；;：:): weak break after.
#   "opening"  — opening bracket/quote (Ps/Pi: 「『（〔【《〈 " ( [ …):
#                glues RIGHTWARD — never break right after one.
#   "closing"  — closing bracket/quote (Pe/Pf: 」』）〕】》〉 " ) ] …):
#                glues LEFTWARD — never break right before one.
#   "other"    — remaining punctuation (・‐—/～ " ' etc.): mid-strength break.
#   "none"     — not punctuation.
SENTENCE_END_PUNCT = frozenset("。．｡.！!？?…‥‼⁇⁈⁉")
CLAUSE_PUNCT = frozenset("、，,､；;：:")


def punct_class(ch: str) -> str:
    """Classify one character for split-boundary decisions (see table above)."""

    if not ch:
        return "none"
    if ch in SENTENCE_END_PUNCT:
        return "sentence"
    if ch in CLAUSE_PUNCT:
        return "clause"
    category = unicodedata.category(ch)
    if category in ("Ps", "Pi"):
        return "opening"
    if category in ("Pe", "Pf"):
        return "closing"
    if category.startswith("P"):
        return "other"
    return "none"


def coerce_optional_float(value: object) -> Optional[float]:
    """Return ``value`` as float, or None when missing or not coercible."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def min_word_confidence(words: List[Dict[str, object]]) -> Optional[float]:
    """A synthetic word is only as reliable as its least-confident source."""

    confidences = [
        confidence
        for confidence in (
            coerce_optional_float(word.get("confidence")) for word in words
        )
        if confidence is not None
    ]
    return min(confidences) if confidences else None


def words_to_text(words: List[Dict[str, object]]) -> str:
    result_tokens: List[str] = []
    for word in words:
        token = word.get("word")
        if token is None:
            continue
        token = str(token)
        if not token:
            continue
        if result_tokens and bool(word.get("space_before", False)):
            result_tokens.append(" ")
        result_tokens.append(token)
    return "".join(result_tokens).strip()
