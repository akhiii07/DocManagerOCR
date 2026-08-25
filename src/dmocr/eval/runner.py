"""Evaluation runner.

Runs the pipeline over a labelled corpus and scores it stage by stage:

    OCR            CER / WER against reference text
    Classification confusion matrix, with DEFERRAL counted separately from error
    Extraction     per-field outcomes, with WRONG separated from MISSING
    Findings       per-rule precision and recall against expected findings

The output carries **metrics and identifiers only, never values**, unless
`include_values` is set for local debugging. A ground-truth corpus contains transcribed
customer data, and a report that cannot be circulated is a report nobody reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..ingest import InMemoryContentStore, sha256_hex
from ..model.case import Case, LenderType, Product
from ..ocr import InMemoryOcrCache, TextExtractionService, default_engine
from ..pipeline import CasePipeline, CaseResult
from .groundtruth import CaseTruth, DocumentTruth, coverage_summary
from .matching import best_outcome
from .metrics import (
    ConfusionMatrix,
    Outcome,
    OutcomeCounts,
    character_error_rate,
    mean,
    normalised_for_ocr,
    percentile,
    word_error_rate,
)

log = logging.getLogger(__name__)


@dataclass
class FieldScore:
    case_id: str
    document: str
    field_name: str
    outcome: Outcome
    detail: str = ""
    #: Populated only when include_values is set.
    expected: str | None = None
    actual: str | None = None


@dataclass
class OcrScore:
    case_id: str
    document: str
    cer: float | None
    wer: float | None
    cer_normalised: float | None
    pages: int


@dataclass
class FindingScore:
    case_id: str
    rule_id: str
    outcome: Outcome
    detail: str = ""


@dataclass
class EvaluationResult:
    started_at: datetime
    coverage: dict = field(default_factory=dict)
    classification: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    field_scores: list[FieldScore] = field(default_factory=list)
    ocr_scores: list[OcrScore] = field(default_factory=list)
    finding_scores: list[FindingScore] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- aggregation -------------------------------------------------------------

    def extraction_counts(self) -> OutcomeCounts:
        counts = OutcomeCounts()
        for s in self.field_scores:
            counts.add(s.outcome)
        return counts

    def extraction_by_field(self) -> dict[str, OutcomeCounts]:
        out: dict[str, OutcomeCounts] = {}
        for s in self.field_scores:
            out.setdefault(s.field_name, OutcomeCounts()).add(s.outcome)
        return dict(sorted(out.items()))

    def finding_counts(self) -> OutcomeCounts:
        counts = OutcomeCounts()
        for s in self.finding_scores:
            counts.add(s.outcome)
        return counts

    def ocr_summary(self) -> dict:
        cers = [s.cer for s in self.ocr_scores if s.cer is not None]
        wers = [s.wer for s in self.ocr_scores if s.wer is not None]
        norm = [s.cer_normalised for s in self.ocr_scores if s.cer_normalised is not None]
        return {
            "documents_scored": len(cers),
            "cer_mean": mean(cers),
            "cer_p90": percentile(cers, 0.90),
            "wer_mean": mean(wers),
            "wer_p90": percentile(wers, 0.90),
            "cer_normalised_mean": mean(norm),
        }

    @property
    def dangerous_errors(self) -> list[FieldScore]:
        """The failures that reach a reviewer as an answer."""
        return [s for s in self.field_scores if s.outcome.is_dangerous]


class EvaluationRunner:
    def __init__(
        self,
        pipeline_factory=None,
        *,
        include_values: bool = False,
    ):
        #: Callable returning a fresh CasePipeline. A factory rather than an instance so
        #: each case starts from clean state - a shared content store would make the
        #: second case's duplicate detection fire against the first.
        self.pipeline_factory = pipeline_factory or _default_pipeline
        self.include_values = include_values
        self._text: TextExtractionService | None = None

    def _text_service(self) -> TextExtractionService:
        """Lazily built and reused, so OCR models load once across the whole corpus."""
        if self._text is None:
            self._text = TextExtractionService(default_engine(), InMemoryOcrCache())
        return self._text

    def run(self, corpus: list[CaseTruth], documents_root: str | Path) -> EvaluationResult:
        root = Path(documents_root)
        result = EvaluationResult(started_at=datetime.now(),
                                  coverage=coverage_summary(corpus))

        if not corpus:
            result.notes.append(
                "No ground truth found. Nothing was measured - this is not a pass."
            )
            return result

        for truth in corpus:
            try:
                self._run_case(truth, root, result)
            except Exception as exc:  # one bad case must not lose the run
                log.exception("evaluation failed for case %s", truth.case_id)
                result.errors.append(f"{truth.case_id}: {type(exc).__name__}: {exc}")

        self._add_notes(result)
        return result

    # -- per case ----------------------------------------------------------------

    def _run_case(self, truth: CaseTruth, root: Path, result: EvaluationResult) -> None:
        files: list[tuple[str, bytes]] = []
        for doc in truth.documents:
            path = root / doc.file
            if not path.is_file():
                result.errors.append(f"{truth.case_id}: missing document {doc.file}")
                continue
            files.append((doc.file, path.read_bytes()))

        if not files:
            return

        case = Case(tenant_id="eval", lender_type=LenderType.HFC,
                    product=Product.HOUSING_LOAN)
        pipeline = self.pipeline_factory()
        case_result = pipeline.process(case, files)

        self._score_classification(truth, case_result, result)
        self._score_ocr(truth, root, result)
        self._score_extraction(truth, case_result, case, result)
        self._score_findings(truth, case_result, result)

    def _score_classification(
        self, truth: CaseTruth, case_result: CaseResult, result: EvaluationResult
    ) -> None:
        for outcome in case_result.documents:
            doc_truth = truth.document(outcome.filename or "")
            if doc_truth is None or not doc_truth.document_type:
                continue
            result.classification.add(
                expected=doc_truth.document_type,
                predicted=outcome.document_type.value,
            )

    def _score_ocr(
        self, truth: CaseTruth, root: Path, result: EvaluationResult
    ) -> None:
        for doc in truth.documents:
            if not doc.has_ocr_reference:
                continue
            path = root / doc.file
            if not path.is_file():
                continue
            try:
                data = path.read_bytes()
                # Use the SAME text extraction the pipeline uses, so a scanned page is
                # scored on the recogniser's output. Reading only the embedded text layer
                # would return nothing for a scan and report CER 1.0 for every one.
                ocr_doc, _ = self._text_service().extract(data, sha256_hex(data))
            except Exception as exc:
                result.errors.append(f"{truth.case_id}/{doc.file}: OCR scoring failed: {exc}")
                continue

            hypothesis = "\n".join(ocr_doc.page_texts())
            reference = doc.reference_text or ""
            result.ocr_scores.append(OcrScore(
                case_id=truth.case_id,
                document=doc.file,
                cer=character_error_rate(reference, hypothesis),
                wer=word_error_rate(reference, hypothesis),
                cer_normalised=character_error_rate(
                    normalised_for_ocr(reference), normalised_for_ocr(hypothesis)),
                pages=ocr_doc.page_count,
            ))

    def _score_extraction(
        self,
        truth: CaseTruth,
        case_result: CaseResult,
        case: Case,
        result: EvaluationResult,
    ) -> None:
        extracted = _extracted_by_document(case_result)

        for doc_truth in truth.documents:
            outcome = next(
                (d for d in case_result.documents if d.filename == doc_truth.file), None)
            if outcome is None:
                continue
            by_field = extracted.get(outcome.document_id, {})

            for field_name, expected in doc_truth.fields.items():
                candidates = by_field.get(field_name, [])
                score_outcome, detail = best_outcome(
                    field_name, expected, [c.value for c in candidates])
                result.field_scores.append(self._field_score(
                    truth, doc_truth, field_name, score_outcome, detail,
                    expected, candidates))

            for field_name in doc_truth.absent_fields:
                candidates = by_field.get(field_name, [])
                if candidates:
                    # A value invented where the reference says none exists. Without
                    # `absent_fields` this would be invisible.
                    result.field_scores.append(self._field_score(
                        truth, doc_truth, field_name, Outcome.SPURIOUS,
                        "extracted where the reference asserts absence", None, candidates))
                else:
                    result.field_scores.append(self._field_score(
                        truth, doc_truth, field_name, Outcome.CORRECT,
                        "correctly absent", None, []))

    def _field_score(
        self, truth, doc_truth, field_name, outcome, detail, expected, candidates
    ) -> FieldScore:
        score = FieldScore(
            case_id=truth.case_id, document=doc_truth.file, field_name=field_name,
            outcome=outcome, detail=detail,
        )
        if self.include_values:
            score.expected = None if expected is None else str(expected)
            score.actual = "; ".join(str(c.value.comparable()) for c in candidates) or None
        return score

    def _score_findings(
        self, truth: CaseTruth, case_result: CaseResult, result: EvaluationResult
    ) -> None:
        produced = {f.rule_id: f for f in case_result.findings}

        for expected in truth.expected_findings:
            actual = produced.get(expected.rule_id)
            if actual is None:
                result.finding_scores.append(FindingScore(
                    truth.case_id, expected.rule_id, Outcome.MISSING,
                    "expected finding not produced"))
                continue
            if expected.determination and actual.determination.value != expected.determination:
                result.finding_scores.append(FindingScore(
                    truth.case_id, expected.rule_id, Outcome.WRONG,
                    f"expected {expected.determination}, got {actual.determination.value}"))
                continue
            result.finding_scores.append(FindingScore(
                truth.case_id, expected.rule_id, Outcome.CORRECT))

        for rule_id in truth.expected_clear:
            actual = produced.get(rule_id)
            if actual is not None and actual.determination.is_adverse:
                # A false positive: the rule fired where the reference says it should not.
                result.finding_scores.append(FindingScore(
                    truth.case_id, rule_id, Outcome.SPURIOUS,
                    f"fired {actual.determination.value} where the case should be clear"))
            else:
                result.finding_scores.append(FindingScore(
                    truth.case_id, rule_id, Outcome.CORRECT, "correctly clear"))

    @staticmethod
    def _add_notes(result: EvaluationResult) -> None:
        cov = result.coverage
        if cov.get("documents", 0) < 30:
            result.notes.append(
                f"Corpus is small ({cov.get('documents', 0)} documents). Rates computed "
                f"over this many samples have wide confidence intervals and should not be "
                f"quoted as accuracy figures."
            )
        if not result.ocr_scores:
            result.notes.append(
                "No OCR reference text in the corpus, so CER/WER were not measured."
            )
        if not result.finding_scores:
            result.notes.append(
                "No expected findings labelled, so rule precision and recall were not "
                "measured. False positives in particular are invisible without "
                "`expected_clear` entries."
            )


def _extracted_by_document(case_result: CaseResult) -> dict[str, dict[str, list]]:
    """document_id -> schema field name -> extracted fields.

    Keyed on the schema field NAME, not the canonical attribute: `cts_number`,
    `survey_number` and `plot_number` all feed `property.parcel_identifier`, so scoring by
    attribute would conflate three different extractions.
    """
    out: dict[str, dict[str, list]] = {}
    for document_id, extraction in case_result.extractions.items():
        by_field: dict[str, list] = {}
        for f in extraction.fields:
            by_field.setdefault(f.field_name, []).append(f)
        out[document_id] = by_field
    return out


#: Repo-relative default rule set. Evaluation needs rules loaded or finding metrics are
#: vacuously zero.
DEFAULT_RULES = Path(__file__).resolve().parents[3] / "rules/mvp.yaml"


def _default_pipeline() -> CasePipeline:
    from ..rules import ExecutionMode, RuleSet

    rule_set = None
    if DEFAULT_RULES.is_file():
        try:
            rule_set = RuleSet.from_yaml(DEFAULT_RULES)
        except Exception as exc:  # pragma: no cover
            log.error("could not load rules for evaluation: %s", exc)

    return CasePipeline(
        InMemoryContentStore(),
        ocr_engine=default_engine(),
        rule_set=rule_set,
        # DRY_RUN, because no rule is APPROVED yet. Measuring a draft rule's
        # false-positive rate BEFORE requesting sign-off is exactly what this mode is
        # for - in ENFORCE mode the harness would score an empty finding set.
        rule_mode=ExecutionMode.DRY_RUN,
    )
