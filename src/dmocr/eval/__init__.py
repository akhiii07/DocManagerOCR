"""Evaluation harness: measure the pipeline against labelled ground truth."""

from .groundtruth import (
    CaseTruth,
    DocumentTruth,
    ExpectedFinding,
    coverage_summary,
    load_case_truth,
    load_corpus,
)
from .matching import best_outcome, compare_to_truth
from .metrics import (
    ConfusionMatrix,
    Outcome,
    OutcomeCounts,
    character_error_rate,
    levenshtein,
    mean,
    normalised_for_ocr,
    percentile,
    word_error_rate,
)
from .report import (
    DEFAULT_GATES,
    Gate,
    GateReport,
    GateResult,
    as_dict,
    check_gates,
    render_markdown,
    write_report,
)
from .runner import (
    EvaluationResult,
    EvaluationRunner,
    FieldScore,
    FindingScore,
    OcrScore,
)

__all__ = [
    "DEFAULT_GATES",
    "CaseTruth",
    "ConfusionMatrix",
    "DocumentTruth",
    "EvaluationResult",
    "EvaluationRunner",
    "ExpectedFinding",
    "FieldScore",
    "FindingScore",
    "Gate",
    "GateReport",
    "GateResult",
    "OcrScore",
    "Outcome",
    "OutcomeCounts",
    "as_dict",
    "best_outcome",
    "character_error_rate",
    "check_gates",
    "compare_to_truth",
    "coverage_summary",
    "levenshtein",
    "load_case_truth",
    "load_corpus",
    "mean",
    "normalised_for_ocr",
    "percentile",
    "render_markdown",
    "word_error_rate",
    "write_report",
]
