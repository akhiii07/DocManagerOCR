"""Document classification.

Classification decides which extraction schema applies. Getting it wrong does not produce
an obvious error — it produces a full set of confidently wrong fields, because a Sale Deed
parsed as a Property Tax receipt will still yield *something*. That is why the escape
hatch matters more here than accuracy: **`UNKNOWN` routing to a human is a correct
outcome**, and the classifier is tuned to reach for it.

Three ways to land on UNKNOWN, each reported distinctly so the reviewer knows what
happened:

* `NO_TEXT`   — nothing to classify on. Scanned documents need OCR first.
* `WEAK`      — the best candidate did not clear the minimum score.
* `AMBIGUOUS` — two candidates were too close to separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..model.case import Document, DocumentQuality
from ..model.common import ConfidenceTier, DocumentType
from .signals import Signal, all_signals

#: Weight multiplier by page. A title phrase on page 1 is far more indicative than the
#: same phrase recited on page 14, which is how the cross-reference problem is handled:
#: a Sale Deed reciting "agreement for sale" scores that phrase at a heavy discount.
PAGE_1_FACTOR = 1.0
PAGE_2_FACTOR = 0.5
LATER_PAGE_FACTOR = 0.15

#: A single signal contributes at most this multiple of its weight, however often it
#: repeats, so a verbose document cannot swamp the score by repetition.
MAX_SIGNAL_MULTIPLE = 2.0

#: Fraction of page 1 treated as the "title region" for `title_only` signals.
TITLE_REGION_CHARS = 1200


class UnknownReason(StrEnum):
    NO_TEXT = "NO_TEXT"
    WEAK = "WEAK"
    AMBIGUOUS = "AMBIGUOUS"


class ClassifierConfig(BaseModel):
    """Tunable decision policy.

    Defaults are conservative: it is cheaper to send a document to a human than to parse
    it with the wrong schema.
    """

    model_config = ConfigDict(frozen=True)

    #: Best candidate must reach this score or the result is UNKNOWN/WEAK.
    min_score: float = 8.0
    #: Best must exceed the runner-up by this ratio or the result is UNKNOWN/AMBIGUOUS.
    min_margin_ratio: float = 1.5
    #: Score at or above which confidence is HIGH.
    high_confidence_score: float = 16.0
    use_devanagari: bool = True


@dataclass(frozen=True)
class SignalHit:
    """One matched phrase, with where it was found. This is the evidence."""

    signal_name: str
    doc_type: DocumentType
    page: int
    matched_text: str
    contribution: float


@dataclass
class ClassificationResult:
    document_type: DocumentType
    confidence: ConfidenceTier
    score: float = 0.0
    #: All type scores, highest first. Lets a reviewer see what was considered.
    scores: dict[DocumentType, float] = field(default_factory=dict)
    hits: list[SignalHit] = field(default_factory=list)
    unknown_reason: UnknownReason | None = None
    runner_up: DocumentType | None = None
    note: str = ""

    @property
    def is_unknown(self) -> bool:
        return self.document_type is DocumentType.UNKNOWN

    @property
    def needs_human(self) -> bool:
        return self.is_unknown or self.confidence in (
            ConfidenceTier.LOW,
            ConfidenceTier.INSUFFICIENT,
        )

    def evidence_for(self, doc_type: DocumentType) -> list[SignalHit]:
        return [h for h in self.hits if h.doc_type == doc_type]


def _page_factor(page: int) -> float:
    if page == 1:
        return PAGE_1_FACTOR
    if page == 2:
        return PAGE_2_FACTOR
    return LATER_PAGE_FACTOR


class RuleClassifier:
    """Weighted-signal classifier over page text."""

    def __init__(self, config: ClassifierConfig | None = None):
        self.config = config or ClassifierConfig()
        self._signals: list[tuple[Signal, object]] = [
            (s, s.compiled())
            for s in all_signals(use_devanagari=self.config.use_devanagari)
        ]

    def classify(
        self,
        pages: list[str],
        *,
        quality: DocumentQuality = DocumentQuality.OK,
    ) -> ClassificationResult:
        """Classify from per-page text, 1-indexed by position in the list."""
        if not pages or not any(p.strip() for p in pages):
            return ClassificationResult(
                document_type=DocumentType.UNKNOWN,
                confidence=ConfidenceTier.INSUFFICIENT,
                unknown_reason=UnknownReason.NO_TEXT,
                note=(
                    "No text available to classify. A scanned document must be OCR'd "
                    "before classification."
                ),
            )

        hits: list[SignalHit] = []
        scores: dict[DocumentType, float] = {}

        for signal, rx in self._signals:
            accumulated = 0.0
            cap = signal.weight * MAX_SIGNAL_MULTIPLE

            for idx, text in enumerate(pages):
                page = idx + 1
                if not text:
                    continue
                haystack = (
                    text[:TITLE_REGION_CHARS] if signal.title_only and page == 1 else text
                )
                if signal.title_only and page != 1:
                    # A title phrase away from the first page is a recital, not a title.
                    continue

                for m in rx.finditer(haystack):
                    if accumulated >= cap:
                        break
                    contribution = min(
                        signal.weight * _page_factor(page), cap - accumulated
                    )
                    if contribution <= 0:
                        break
                    accumulated += contribution
                    hits.append(SignalHit(
                        signal_name=signal.name,
                        doc_type=signal.doc_type,
                        page=page,
                        matched_text=m.group(0)[:120],
                        contribution=round(contribution, 3),
                    ))
                if accumulated >= cap:
                    break

            if accumulated:
                scores[signal.doc_type] = round(
                    scores.get(signal.doc_type, 0.0) + accumulated, 3
                )

        return self._decide(scores, hits, quality)

    def _decide(
        self,
        scores: dict[DocumentType, float],
        hits: list[SignalHit],
        quality: DocumentQuality,
    ) -> ClassificationResult:
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)

        if not ordered:
            return ClassificationResult(
                document_type=DocumentType.UNKNOWN,
                confidence=ConfidenceTier.INSUFFICIENT,
                scores=dict(ordered),
                hits=hits,
                unknown_reason=UnknownReason.WEAK,
                note="No classification signal matched.",
            )

        best_type, best_score = ordered[0]
        runner_up, runner_score = (ordered[1] if len(ordered) > 1 else (None, 0.0))

        if best_score < self.config.min_score:
            return ClassificationResult(
                document_type=DocumentType.UNKNOWN,
                confidence=ConfidenceTier.INSUFFICIENT,
                score=best_score,
                scores=dict(ordered),
                hits=hits,
                unknown_reason=UnknownReason.WEAK,
                runner_up=best_type,
                note=(
                    f"Best candidate {best_type.value} scored {best_score:.1f}, below the "
                    f"minimum of {self.config.min_score:.1f}."
                ),
            )

        if runner_score > 0 and best_score < runner_score * self.config.min_margin_ratio:
            return ClassificationResult(
                document_type=DocumentType.UNKNOWN,
                confidence=ConfidenceTier.INSUFFICIENT,
                score=best_score,
                scores=dict(ordered),
                hits=hits,
                unknown_reason=UnknownReason.AMBIGUOUS,
                runner_up=runner_up,
                note=(
                    f"{best_type.value} ({best_score:.1f}) and {runner_up.value} "
                    f"({runner_score:.1f}) are too close to separate. Human review "
                    f"required rather than a guess."
                ),
            )

        if best_score >= self.config.high_confidence_score:
            confidence = ConfidenceTier.HIGH
        elif best_score >= self.config.min_score:
            confidence = ConfidenceTier.MEDIUM
        else:  # pragma: no cover - guarded above
            confidence = ConfidenceTier.LOW

        note = ""
        # A DEGRADED document's text came from a poor scan, so the classification rests on
        # unreliable input however decisive the phrases look.
        if quality is DocumentQuality.DEGRADED and confidence is ConfidenceTier.HIGH:
            confidence = ConfidenceTier.MEDIUM
            note = "Confidence capped: source document quality is DEGRADED."

        return ClassificationResult(
            document_type=best_type,
            confidence=confidence,
            score=best_score,
            scores=dict(ordered),
            hits=hits,
            runner_up=runner_up,
            note=note,
        )


def apply_to_document(
    doc: Document,
    result: ClassificationResult,
    *,
    overwrite: bool = False,
) -> bool:
    """Write a classification onto a Document.

    Refuses to overwrite a type that was set deliberately (e.g. declared at upload or
    corrected by a reviewer) unless asked. A human's classification outranks the
    classifier's.
    """
    if doc.document_type is not DocumentType.UNKNOWN and not overwrite:
        return False
    doc.document_type = result.document_type
    doc.classification_confidence = {
        ConfidenceTier.HIGH: 0.9,
        ConfidenceTier.MEDIUM: 0.6,
        ConfidenceTier.LOW: 0.3,
        ConfidenceTier.INSUFFICIENT: 0.0,
    }[result.confidence]
    if result.note:
        doc.quality_notes.append(f"classification: {result.note}")
    return True
