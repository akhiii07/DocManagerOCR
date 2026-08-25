"""Evaluation reporting and regression gates.

The report is **safe to circulate by default**: metrics, counts and identifiers, never
extracted or reference values. Ground truth contains transcribed customer data, and a
report nobody can share is a report nobody reads.

Two things the report deliberately refuses to do:

* **It does not produce a single headline accuracy number.** Precision, recall, the
  dangerous-error rate and the deferral rate answer different questions, and collapsing
  them lets a system that guesses look like one that is careful.
* **It does not describe a small corpus as an accuracy figure.** Coverage is stated first,
  and rates over a handful of documents are labelled as indicative.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .runner import EvaluationResult


def as_dict(result: EvaluationResult) -> dict:
    """Machine-readable report. Contains no document values."""
    return {
        "schema_version": 1,
        "started_at": result.started_at.isoformat(timespec="seconds"),
        "coverage": result.coverage,
        "classification": result.classification.as_dict(),
        "ocr": result.ocr_summary(),
        "extraction": {
            "overall": result.extraction_counts().as_dict(),
            "by_field": {k: v.as_dict()
                         for k, v in result.extraction_by_field().items()},
        },
        "findings": result.finding_counts().as_dict(),
        "dangerous_errors": [
            {"case": s.case_id, "document": s.document, "field": s.field_name,
             "outcome": s.outcome.value, "detail": s.detail}
            for s in result.dangerous_errors
        ],
        "errors": result.errors,
        "notes": result.notes,
    }


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1%}" if 0 <= value <= 1 else f"{value:.4f}"
    return str(value)


def render_markdown(result: EvaluationResult) -> str:
    lines: list[str] = []
    add = lines.append

    add("# Evaluation report")
    add("")
    add(f"Run at {result.started_at.isoformat(timespec='seconds')}")
    add("")

    cov = result.coverage
    add("## Coverage")
    add("")
    add(f"- Cases: **{cov.get('cases', 0)}**")
    add(f"- Documents: **{cov.get('documents', 0)}**")
    add(f"- Labelled fields: **{cov.get('labelled_fields', 0)}**")
    add(f"- Documents with OCR reference text: **{cov.get('documents_with_ocr_reference', 0)}**")
    add(f"- Expected findings labelled: **{cov.get('expected_findings', 0)}**")
    if cov.get("document_types"):
        add(f"- Document types: `{cov['document_types']}`")
    add("")
    add("> Read every rate below against these numbers. A metric over a handful of")
    add("> documents is indicative, not an accuracy figure.")
    add("")

    # -- classification ----------------------------------------------------------
    cm = result.classification
    add("## Classification")
    add("")
    add(f"- Documents scored: **{cm.total}**")
    add(f"- Accuracy on decided: **{_fmt(cm.accuracy_on_decided)}**")
    add(f"- Deferral rate (routed to a human): **{_fmt(cm.deferral_rate)}**")
    add(f"- Misclassification rate: **{_fmt(cm.misclassification_rate)}**")
    add("")
    add("> Deferral is reported separately from error on purpose. Counting an `UNKNOWN`")
    add("> as a misclassification would score a guessing classifier above a cautious one,")
    add("> and a wrong document type produces a full set of confidently wrong fields.")
    if cm.confusions():
        add("")
        add("| Expected | Predicted | Count |")
        add("|---|---|---|")
        for expected, predicted, n in cm.confusions():
            add(f"| {expected} | {predicted} | {n} |")
    add("")

    # -- OCR ---------------------------------------------------------------------
    ocr = result.ocr_summary()
    add("## OCR")
    add("")
    if not ocr["documents_scored"]:
        add("Not measured — no reference text in the corpus.")
    else:
        add(f"- Documents scored: **{ocr['documents_scored']}**")
        add(f"- CER mean / p90: **{_fmt(ocr['cer_mean'])}** / {_fmt(ocr['cer_p90'])}")
        add(f"- WER mean / p90: **{_fmt(ocr['wer_mean'])}** / {_fmt(ocr['wer_p90'])}")
        add(f"- CER (whitespace-normalised) mean: {_fmt(ocr['cer_normalised_mean'])}")
        add("")
        add("> A large gap between raw and normalised CER means the characters were read")
        add("> but the layout was not — that points at reading order, not recognition.")
    add("")

    # -- extraction --------------------------------------------------------------
    counts = result.extraction_counts()
    add("## Extraction")
    add("")
    add(f"- Fields evaluated: **{counts.evaluated}**")
    add(f"- Precision: **{_fmt(counts.precision)}**  ·  Recall: **{_fmt(counts.recall)}**"
        f"  ·  F1: {_fmt(counts.f1)}")
    add(f"- **Dangerous error rate (wrong or invented): {_fmt(counts.dangerous_error_rate)}**")
    add(f"- Safe failure rate (missing or near): {_fmt(counts.safe_failure_rate)}")
    add("")
    add("> The dangerous-error rate is the headline safety metric. A system can have")
    add("> mediocre recall and still be trustworthy; it cannot have a high")
    add("> dangerous-error rate and be trustworthy, because those failures reach a")
    add("> reviewer as an answer rather than as a gap.")
    add("")

    by_field = result.extraction_by_field()
    if by_field:
        add("| Field | Correct | Near | **Wrong** | Missing | **Spurious** | Precision | Recall |")
        add("|---|---|---|---|---|---|---|---|")
        for name, c in by_field.items():
            add(f"| {name} | {c.correct} | {c.near} | **{c.wrong}** | {c.missing} | "
                f"**{c.spurious}** | {_fmt(c.precision)} | {_fmt(c.recall)} |")
        add("")

    if result.dangerous_errors:
        add("### Dangerous errors")
        add("")
        add("Values that were produced and were wrong. Investigate these first.")
        add("")
        for s in result.dangerous_errors:
            add(f"- `{s.case_id}` / `{s.document}` / **{s.field_name}** "
                f"— {s.outcome.value}: {s.detail}")
        add("")

    # -- findings ----------------------------------------------------------------
    fc = result.finding_counts()
    add("## Findings")
    add("")
    if not fc.evaluated:
        add("Not measured — no expected findings labelled.")
        add("")
        add("> False positives are invisible without `expected_clear` entries. A rule set")
        add("> that fires on everything scores perfectly on recall alone.")
    else:
        add(f"- Findings evaluated: **{fc.evaluated}**")
        add(f"- Precision: **{_fmt(fc.precision)}**  ·  Recall: **{_fmt(fc.recall)}**")
        add(f"- False positives (fired where the case should be clear): **{fc.spurious}**")
        add(f"- False negatives (expected but not produced): **{fc.missing}**")
    add("")

    if result.errors:
        add("## Errors")
        add("")
        for e in result.errors:
            add(f"- {e}")
        add("")

    if result.notes:
        add("## Notes")
        add("")
        for n in result.notes:
            add(f"- {n}")
        add("")
    return "\n".join(lines)


# =====================================================================================
# Regression gates
# =====================================================================================


@dataclass
class Gate:
    """One threshold the evaluation must satisfy."""

    name: str
    #: Dotted path into the report dict, e.g. "extraction.overall.dangerous_error_rate".
    metric: str
    #: "max" - the metric must not exceed the value. "min" - must not fall below it.
    direction: str
    threshold: float
    #: When True, a metric that is None fails. Default is to skip, because "not measured"
    #: is not the same as "failed" - and a gate that fails on an unmeasurable metric
    #: pushes toward labelling data just to make CI pass.
    required: bool = False


@dataclass
class GateResult:
    gate: Gate
    value: float | None
    passed: bool
    reason: str


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.passed]


#: Starting thresholds. **Uncalibrated** - they exist so a regression is visible, not
#: because these numbers have been shown to be the right ones. Tighten them against a real
#: corpus rather than treating them as targets met.
DEFAULT_GATES = [
    Gate("extraction dangerous errors", "extraction.overall.dangerous_error_rate",
         "max", 0.05),
    Gate("extraction recall", "extraction.overall.recall", "min", 0.70),
    Gate("classification misclassification", "classification.misclassification_rate",
         "max", 0.05),
    Gate("finding false positives", "findings.spurious", "max", 0),
]


def _lookup(report: dict, path: str):
    node = report
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def check_gates(report: dict, gates: list[Gate] | None = None) -> GateReport:
    out = GateReport()
    for gate in gates if gates is not None else DEFAULT_GATES:
        value = _lookup(report, gate.metric)
        if value is None:
            out.results.append(GateResult(
                gate, None, not gate.required,
                "not measured" + ("" if gate.required else " (gate skipped)"),
            ))
            continue
        if gate.direction == "max":
            passed = value <= gate.threshold
            reason = f"{value} {'<=' if passed else '>'} {gate.threshold}"
        else:
            passed = value >= gate.threshold
            reason = f"{value} {'>=' if passed else '<'} {gate.threshold}"
        out.results.append(GateResult(gate, value, passed, reason))
    return out


def write_report(result: EvaluationResult, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = as_dict(result)
    json_path = out / "evaluation.json"
    md_path = out / "evaluation.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return json_path, md_path
