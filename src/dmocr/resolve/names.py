"""Name matching.

Indian names in property documents vary in ways that defeat string equality:

* **Transliteration** — Desai / Dessai, Anita / Aneeta, Vishwas / Vishvas
* **Initials** — "R. Patil" and "Ramesh Patil" are usually the same person
* **Ordering** — surname first in some records, last in others
* **Honorifics** — Shri, Smt., M/s, Late
* **Patronymics** — "Ramesh s/o Ganpat Patil"
* **OCR damage** — word boundaries lost entirely ("RameshPatil")

So matching is **scored, with an explicit uncertain band**. Two names that are close but
not clearly the same produce `PARTIAL_MATCH`, which routes to a human. Forcing a binary
answer here would either merge two different people — the worse error, since it can make a
title chain look continuous when it is broken — or split one person into two and
manufacture a mismatch finding.

Nothing here is calibrated. The thresholds are starting points to be tuned against
reviewer outcomes on real documents, and the module reports scores so that tuning is
possible. See OPEN-ITEMS.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..model.common import Determination

# Score at or above which two names are treated as the same person.
MATCH_THRESHOLD = 0.92
# Below this they are treated as different. Between the two is the review band.
MISMATCH_THRESHOLD = 0.75

_HONORIFICS = {
    "shri", "sri", "shree", "smt", "smt.", "mr", "mrs", "ms", "dr", "late",
    "m/s", "messrs", "kum", "kumari", "shrimati",
}

#: Relationship markers. Everything after one of these is a parent's name, not the
#: party's, so it is separated rather than treated as part of the name.
_RELATION = re.compile(
    r"\b(?:s\s*/\s*o|d\s*/\s*o|w\s*/\s*o|c\s*/\s*o|son\s+of|daughter\s+of|wife\s+of)\b",
    re.IGNORECASE,
)

#: Common transliteration equivalences, applied in order. Moderate on purpose: an
#: aggressive phonetic fold merges genuinely different surnames, and a false merge is the
#: more dangerous error here.
_FOLDS: list[tuple[str, str]] = [
    ("aa", "a"), ("ee", "i"), ("ii", "i"), ("oo", "u"), ("uu", "u"),
    ("ph", "f"), ("w", "v"), ("z", "j"),
    ("ksh", "x"), ("cch", "ch"),
]


@dataclass(frozen=True)
class NameParts:
    tokens: tuple[str, ...]
    #: Text following a relationship marker (parent/spouse name), kept separately.
    relation: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.tokens


def split_name(raw: str) -> NameParts:
    """Strip honorifics, separate any patronymic, and tokenise."""
    if not raw:
        return NameParts(())

    relation = None
    m = _RELATION.search(raw)
    if m:
        relation = raw[m.end():].strip(" ,.")
        raw = raw[: m.start()]

    cleaned = re.sub(r"[^\w\s.]", " ", raw)
    tokens = []
    for tok in cleaned.split():
        bare = tok.strip(".").lower()
        if not bare or bare in _HONORIFICS:
            continue
        tokens.append(bare)
    return NameParts(tuple(tokens), relation or None)


def phonetic_key(token: str) -> str:
    """Fold common transliteration variants to a comparable key.

    Heuristic, not a linguistic model. Doubled letters collapse and a small set of
    digraphs are normalised, which covers the variants seen most often in transliterated
    Indian names without merging distinct ones.
    """
    t = token.lower()
    for a, b in _FOLDS:
        t = t.replace(a, b)
    # Collapse any remaining doubled letters: Dessai -> Desai.
    t = re.sub(r"(.)\1+", r"\1", t)
    return t


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similarity(a: str, b: str) -> float:
    """Normalised edit similarity in [0, 1]."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - (_levenshtein(a, b) / longest)


def _is_initial(token: str) -> bool:
    return len(token) == 1


def _token_match(x: str, y: str) -> float:
    """Compare two name tokens, allowing an initial to stand for a full name."""
    if x == y:
        return 1.0
    if _is_initial(x) or _is_initial(y):
        # "R." matches "Ramesh" but is weak evidence on its own.
        return 0.85 if x[0] == y[0] else 0.0
    if phonetic_key(x) == phonetic_key(y):
        return 0.95
    return similarity(x, y)


@dataclass(frozen=True)
class NameMatch:
    score: float
    determination: Determination
    reason: str
    #: Per-token pairings, for showing a reviewer why.
    detail: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return self.determination is Determination.PARTIAL_MATCH


def match_names(left: str, right: str) -> NameMatch:
    """Score two names, order-insensitively.

    Returns MATCH / PARTIAL_MATCH / MISMATCH / NOT_DETERMINABLE. PARTIAL_MATCH is a real
    outcome, not a failure to decide - it routes to a human, which is correct when the
    alternative is guessing about identity.
    """
    lp, rp = split_name(left), split_name(right)
    if lp.is_empty or rp.is_empty:
        return NameMatch(0.0, Determination.NOT_DETERMINABLE,
                         "One or both names are empty after normalisation.")

    # OCR sometimes loses internal spaces entirely. If one side is a single token and the
    # other is several, compare against the concatenation before giving up.
    if len(lp.tokens) == 1 and len(rp.tokens) > 1:
        joined = "".join(rp.tokens)
        score = _token_match(lp.tokens[0], joined)
        if score >= MATCH_THRESHOLD:
            return NameMatch(score, Determination.MATCH,
                             "Single token matches the concatenated name; OCR probably "
                             "lost the word boundaries.")
    if len(rp.tokens) == 1 and len(lp.tokens) > 1:
        return match_names(right, left)

    # Greedy best pairing, order-insensitive.
    remaining = list(rp.tokens)
    scores: list[float] = []
    detail: list[str] = []
    for token in lp.tokens:
        if not remaining:
            break
        best_idx, best_score = 0, -1.0
        for i, candidate in enumerate(remaining):
            s = _token_match(token, candidate)
            if s > best_score:
                best_idx, best_score = i, s
        paired = remaining.pop(best_idx)
        scores.append(best_score)
        detail.append(f"{token}~{paired}={best_score:.2f}")

    if not scores:
        return NameMatch(0.0, Determination.NOT_DETERMINABLE, "No comparable tokens.")

    matched = sum(scores) / len(scores)
    # Unpaired tokens on either side dilute the score: "Ramesh Patil" vs
    # "Ramesh Ganpat Patil" should not score as a perfect match.
    total_tokens = max(len(lp.tokens), len(rp.tokens))
    coverage = len(scores) / total_tokens
    score = round(matched * (0.7 + 0.3 * coverage), 4)

    if score >= MATCH_THRESHOLD:
        det, reason = Determination.MATCH, "Names agree."
    elif score >= MISMATCH_THRESHOLD:
        det, reason = (
            Determination.PARTIAL_MATCH,
            "Names are similar but not clearly the same. Human confirmation required "
            "rather than a guess about identity.",
        )
    else:
        det, reason = Determination.MISMATCH, "Names differ."

    return NameMatch(score, det, reason, detail)


def best_match(target: str, candidates: list[str]) -> tuple[str | None, NameMatch]:
    """The candidate that best matches `target`."""
    if not candidates:
        return None, NameMatch(0.0, Determination.NOT_DETERMINABLE, "No candidates.")
    scored = [(c, match_names(target, c)) for c in candidates]
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    return scored[0]
