"""Metric primitives.

The important design decision here is what the metrics **reward**.

A naive harness scores every non-correct answer as an error. Under that scoring, a system
that guesses beats a system that says `UNKNOWN` — and everything this platform does to
avoid confident wrongness becomes a liability in its own evaluation.

So the outcome vocabulary separates **being wrong** from **declining to answer**:

* `CORRECT`   — matched the reference
* `NEAR`      — close enough to be plausible, not close enough to accept (a review case)
* `WRONG`     — produced a different answer. **The dangerous one.**
* `MISSING`   — the reference has a value, the system produced none. Safe failure.
* `SPURIOUS`  — the system produced a value the reference says does not exist.
* `NOT_EVALUATED` — no reference to compare against.

`WRONG` and `SPURIOUS` are the errors that reach a Risk Manager as false assurance.
`MISSING` surfaces as a gap. Reporting them in one number would hide the distinction that
matters most.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import StrEnum


class Outcome(StrEnum):
    CORRECT = "CORRECT"
    NEAR = "NEAR"
    WRONG = "WRONG"
    MISSING = "MISSING"
    SPURIOUS = "SPURIOUS"
    NOT_EVALUATED = "NOT_EVALUATED"

    @property
    def is_dangerous(self) -> bool:
        """Errors that present as an answer. These reach a reviewer as false assurance."""
        return self in (Outcome.WRONG, Outcome.SPURIOUS)

    @property
    def is_safe_failure(self) -> bool:
        """Failures that surface as a gap rather than as a wrong answer."""
        return self in (Outcome.MISSING, Outcome.NEAR)


# =====================================================================================
# Edit distance, CER and WER
# =====================================================================================


def levenshtein(a: str, b: str) -> int:
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


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"\s+", text.strip()) if t]


def _token_levenshtein(a: list[str], b: list[str]) -> int:
    if a == b:
        return 0
    prev = list(range(len(b) + 1))
    for i, ta in enumerate(a, 1):
        cur = [i]
        for j, tb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ta != tb)))
        prev = cur
    return prev[-1]


def character_error_rate(reference: str, hypothesis: str) -> float | None:
    """CER = edit distance / reference length. Lower is better; 0.0 is perfect.

    Returns None for an empty reference, because dividing by zero would report a perfect
    or infinite score for a page nobody transcribed.
    """
    ref = reference.strip()
    if not ref:
        return None
    return round(levenshtein(ref, hypothesis.strip()) / len(ref), 4)


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    """WER over whitespace tokens.

    Note this is unforgiving of the word-boundary loss real OCR produces: recognising
    "RameshPatil" as one token counts as one substitution plus one deletion. That is the
    honest reading - a downstream extractor really does see one token.
    """
    ref = _tokens(reference)
    if not ref:
        return None
    return round(_token_levenshtein(ref, _tokens(hypothesis)) / len(ref), 4)


def normalised_for_ocr(text: str) -> str:
    """Collapse whitespace and casefold, for a layout-insensitive CER.

    Reported alongside raw CER: a large gap between the two means the recogniser read the
    characters but not the layout, which points at reading order rather than recognition.
    """
    return re.sub(r"\s+", " ", text).strip().casefold()


# =====================================================================================
# Counting
# =====================================================================================


@dataclass
class OutcomeCounts:
    """Tally of outcomes, with the safe/dangerous split preserved."""

    correct: int = 0
    near: int = 0
    wrong: int = 0
    missing: int = 0
    spurious: int = 0
    not_evaluated: int = 0

    def add(self, outcome: Outcome, n: int = 1) -> None:
        setattr(self, outcome.value.lower(), getattr(self, outcome.value.lower()) + n)

    def __add__(self, other: "OutcomeCounts") -> "OutcomeCounts":
        return OutcomeCounts(
            correct=self.correct + other.correct,
            near=self.near + other.near,
            wrong=self.wrong + other.wrong,
            missing=self.missing + other.missing,
            spurious=self.spurious + other.spurious,
            not_evaluated=self.not_evaluated + other.not_evaluated,
        )

    @property
    def evaluated(self) -> int:
        return self.correct + self.near + self.wrong + self.missing + self.spurious

    @property
    def attempted(self) -> int:
        """Cases where the system produced an answer at all."""
        return self.correct + self.near + self.wrong + self.spurious

    @property
    def precision(self) -> float | None:
        """Of the answers given, how many were right.

        `NEAR` counts against precision: a value close enough to look plausible but not
        close enough to accept is not a correct answer.
        """
        if not self.attempted:
            return None
        return round(self.correct / self.attempted, 4)

    @property
    def recall(self) -> float | None:
        """Of the values that exist, how many were correctly produced."""
        expected = self.correct + self.near + self.wrong + self.missing
        if not expected:
            return None
        return round(self.correct / expected, 4)

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if p is None or r is None or (p + r) == 0:
            return None
        return round(2 * p * r / (p + r), 4)

    @property
    def dangerous_error_rate(self) -> float | None:
        """Share of evaluated cases that produced a WRONG or SPURIOUS answer.

        The headline safety metric. A system may have mediocre recall and still be
        trustworthy; it cannot have a high dangerous-error rate and be trustworthy.
        """
        if not self.evaluated:
            return None
        return round((self.wrong + self.spurious) / self.evaluated, 4)

    @property
    def safe_failure_rate(self) -> float | None:
        if not self.evaluated:
            return None
        return round((self.missing + self.near) / self.evaluated, 4)

    def as_dict(self) -> dict:
        return {
            "correct": self.correct, "near": self.near, "wrong": self.wrong,
            "missing": self.missing, "spurious": self.spurious,
            "not_evaluated": self.not_evaluated,
            "precision": self.precision, "recall": self.recall, "f1": self.f1,
            "dangerous_error_rate": self.dangerous_error_rate,
            "safe_failure_rate": self.safe_failure_rate,
        }


@dataclass
class ConfusionMatrix:
    """Predicted vs expected labels, with deferral counted separately.

    `DEFERRED_LABEL` predictions are excluded from accuracy and reported as a deferral
    rate. Counting a deliberate "route this to a human" as a misclassification would score
    a guessing classifier above a cautious one.
    """

    DEFERRED_LABEL: str = "unknown"
    cells: dict[tuple[str, str], int] = field(default_factory=dict)

    def add(self, expected: str, predicted: str) -> None:
        key = (expected, predicted)
        self.cells[key] = self.cells.get(key, 0) + 1

    @property
    def total(self) -> int:
        return sum(self.cells.values())

    @property
    def deferred(self) -> int:
        return sum(n for (_, p), n in self.cells.items() if p == self.DEFERRED_LABEL)

    @property
    def decided(self) -> int:
        return self.total - self.deferred

    @property
    def correct(self) -> int:
        return sum(n for (e, p), n in self.cells.items()
                   if e == p and p != self.DEFERRED_LABEL)

    @property
    def accuracy_on_decided(self) -> float | None:
        """Accuracy over the cases the system chose to answer."""
        if not self.decided:
            return None
        return round(self.correct / self.decided, 4)

    @property
    def deferral_rate(self) -> float | None:
        if not self.total:
            return None
        return round(self.deferred / self.total, 4)

    @property
    def misclassification_rate(self) -> float | None:
        """The dangerous one: answered, and answered wrongly."""
        if not self.total:
            return None
        return round((self.decided - self.correct) / self.total, 4)

    def confusions(self, limit: int = 10) -> list[tuple[str, str, int]]:
        wrong = [(e, p, n) for (e, p), n in self.cells.items()
                 if e != p and p != self.DEFERRED_LABEL]
        return sorted(wrong, key=lambda t: -t[2])[:limit]

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "decided": self.decided,
            "deferred": self.deferred,
            "correct": self.correct,
            "accuracy_on_decided": self.accuracy_on_decided,
            "deferral_rate": self.deferral_rate,
            "misclassification_rate": self.misclassification_rate,
            "top_confusions": [
                {"expected": e, "predicted": p, "count": n}
                for e, p, n in self.confusions()
            ],
        }


def mean(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    return round(sum(clean) / len(clean), 4) if clean else None


def percentile(values: list[float], p: float) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    k = (len(clean) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return round(clean[int(k)], 4)
    return round(clean[lo] + (clean[hi] - clean[lo]) * (k - lo), 4)
