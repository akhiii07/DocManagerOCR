"""Tests for the evaluation harness.

The theme is that the metrics must not reward guessing. A harness scoring every
non-correct answer as an error would rank a system that invents values above one that says
`UNKNOWN` — and everything this platform does to avoid confident wrongness would become a
liability in its own evaluation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmocr.eval import (
    ConfusionMatrix,
    EvaluationRunner,
    Gate,
    Outcome,
    OutcomeCounts,
    as_dict,
    character_error_rate,
    check_gates,
    compare_to_truth,
    levenshtein,
    load_case_truth,
    load_corpus,
    render_markdown,
    word_error_rate,
)
from dmocr.model import (
    Area,
    AreaUnit,
    AreaValue,
    DateValue,
    Money,
    MoneyValue,
    ParcelIdentifier,
    ParcelIdentifierType,
    ParcelValue,
    TextValue,
)

TRUTH_DIR = Path("eval/groundtruth")


# =====================================================================================
# Outcome semantics
# =====================================================================================


class TestOutcomeSemantics:
    def test_wrong_and_spurious_are_dangerous(self):
        """These reach a reviewer as an answer, not as a gap."""
        assert Outcome.WRONG.is_dangerous
        assert Outcome.SPURIOUS.is_dangerous

    def test_missing_is_a_safe_failure_not_a_dangerous_one(self):
        assert Outcome.MISSING.is_safe_failure
        assert not Outcome.MISSING.is_dangerous

    def test_near_is_safe_because_it_routes_to_a_human(self):
        assert Outcome.NEAR.is_safe_failure
        assert not Outcome.NEAR.is_dangerous


class TestOutcomeCounts:
    def test_precision_counts_near_against_it(self):
        """A plausible-but-unacceptable value is not a correct answer."""
        c = OutcomeCounts(correct=8, near=2)
        assert c.precision == pytest.approx(0.8)

    def test_missing_hurts_recall_not_precision(self):
        c = OutcomeCounts(correct=5, missing=5)
        assert c.precision == 1.0
        assert c.recall == pytest.approx(0.5)

    def test_dangerous_error_rate_excludes_safe_failures(self):
        c = OutcomeCounts(correct=5, missing=4, wrong=1)
        assert c.dangerous_error_rate == pytest.approx(0.1)
        assert c.safe_failure_rate == pytest.approx(0.4)

    def test_a_declining_system_beats_a_guessing_one_on_danger(self):
        """The core property of the scoring scheme."""
        cautious = OutcomeCounts(correct=5, missing=5)
        guesser = OutcomeCounts(correct=7, wrong=3)
        assert guesser.recall > cautious.recall              # guesser looks better
        assert cautious.dangerous_error_rate == 0.0          # but is more dangerous
        assert guesser.dangerous_error_rate > 0.0

    def test_empty_counts_yield_none_not_zero(self):
        """No data is not a score of zero."""
        c = OutcomeCounts()
        assert c.precision is None and c.recall is None
        assert c.dangerous_error_rate is None

    def test_counts_add(self):
        total = OutcomeCounts(correct=1) + OutcomeCounts(correct=2, wrong=1)
        assert total.correct == 3 and total.wrong == 1


# =====================================================================================
# Classification metrics
# =====================================================================================


class TestConfusionMatrix:
    def test_deferral_is_not_counted_as_an_error(self):
        """Counting UNKNOWN as a misclassification would reward guessing."""
        cm = ConfusionMatrix()
        cm.add("sale_deed", "sale_deed")
        cm.add("sale_deed", "unknown")
        assert cm.accuracy_on_decided == 1.0
        assert cm.deferral_rate == pytest.approx(0.5)
        assert cm.misclassification_rate == 0.0

    def test_misclassification_is_counted(self):
        cm = ConfusionMatrix()
        cm.add("sale_deed", "property_tax")
        assert cm.misclassification_rate == 1.0
        assert cm.accuracy_on_decided == 0.0

    def test_top_confusions_exclude_deferrals(self):
        cm = ConfusionMatrix()
        cm.add("sale_deed", "unknown")
        cm.add("sale_deed", "agreement_of_sale")
        confusions = cm.confusions()
        assert confusions == [("sale_deed", "agreement_of_sale", 1)]

    def test_empty_matrix(self):
        assert ConfusionMatrix().accuracy_on_decided is None


# =====================================================================================
# OCR metrics
# =====================================================================================


class TestOcrMetrics:
    def test_levenshtein(self):
        assert levenshtein("kitten", "sitting") == 3
        assert levenshtein("same", "same") == 0

    def test_perfect_transcription(self):
        assert character_error_rate("hello world", "hello world") == 0.0
        assert word_error_rate("hello world", "hello world") == 0.0

    def test_empty_reference_is_none_not_perfect(self):
        """Dividing by zero would report a perfect score for an untranscribed page."""
        assert character_error_rate("", "anything") is None
        assert word_error_rate("   ", "anything") is None

    def test_word_boundary_loss_hurts_wer_far_more_than_cer(self):
        """The real OCR failure mode: characters read, words joined."""
        ref = "March 2024 BETWEEN Ramesh Patil"
        hyp = "March2024BETWEENRameshPatil"
        cer = character_error_rate(ref, hyp)
        wer = word_error_rate(ref, hyp)
        assert cer < 0.2
        assert wer >= 0.8

    def test_cer_is_bounded_below_by_zero(self):
        assert character_error_rate("abc", "xyz") > 0


# =====================================================================================
# Value matching
# =====================================================================================


class TestMatching:
    def test_money_exact(self):
        outcome, _ = compare_to_truth(
            "consideration", "12500000",
            MoneyValue(amount=Money.from_rupees(12_500_000)))
        assert outcome is Outcome.CORRECT

    def test_money_wrong(self):
        outcome, detail = compare_to_truth(
            "consideration", "12500000",
            MoneyValue(amount=Money.from_rupees(9_000_000)))
        assert outcome is Outcome.WRONG
        assert "expected" in detail

    def test_iso_date_reference_is_not_read_day_first(self):
        """2024-03-14 must not be parsed as a day-first numeric date."""
        from datetime import date

        outcome, _ = compare_to_truth(
            "execution_date", "2024-03-14", DateValue(value=date(2024, 3, 14)))
        assert outcome is Outcome.CORRECT

    def test_parcel_ignores_spacing(self):
        outcome, _ = compare_to_truth(
            "cts_number", "1234/5A",
            ParcelValue(identifier=ParcelIdentifier(
                id_type=ParcelIdentifierType.CTS, value="1234/5a")))
        assert outcome is Outcome.CORRECT

    def test_area_within_tolerance(self):
        outcome, _ = compare_to_truth(
            "area", {"value": 1150, "unit": "sq_ft"},
            AreaValue(area=Area.of(1148, AreaUnit.SQ_FT)))
        assert outcome is Outcome.CORRECT

    def test_right_magnitude_wrong_basis_is_near_not_correct(self):
        outcome, detail = compare_to_truth(
            "area", {"value": 1150, "unit": "sq_ft", "basis": "carpet"},
            AreaValue(area=Area.of(1150, AreaUnit.SQ_FT), basis="built_up"))
        assert outcome is Outcome.NEAR
        assert "basis" in detail

    def test_name_variant_is_correct(self):
        outcome, _ = compare_to_truth("seller", "Ramesh Patil", TextValue(raw="R. Patil"))
        assert outcome is Outcome.CORRECT

    def test_name_in_the_review_band_is_near(self):
        outcome, _ = compare_to_truth(
            "seller", "Ramesh Patil", TextValue(raw="Ramesh Patil Kulkarni Deshmukh"))
        assert outcome in (Outcome.NEAR, Outcome.WRONG)

    def test_different_name_is_wrong(self):
        outcome, _ = compare_to_truth(
            "seller", "Ramesh Patil", TextValue(raw="Suresh Kulkarni"))
        assert outcome is Outcome.WRONG

    def test_punctuation_only_difference_is_near(self):
        outcome, detail = compare_to_truth(
            "assessment_number", "A-1234567890", TextValue(raw="A 1234567890"))
        assert outcome is Outcome.NEAR
        assert "punctuation" in detail

    def test_unparseable_reference_is_not_evaluated(self):
        outcome, _ = compare_to_truth(
            "consideration", "not a number",
            MoneyValue(amount=Money.from_rupees(1)))
        assert outcome is Outcome.NOT_EVALUATED


# =====================================================================================
# Ground truth
# =====================================================================================


class TestGroundTruth:
    def test_synthetic_corpus_loads(self):
        corpus = load_corpus(TRUTH_DIR)
        assert corpus
        assert all(c.synthetic for c in corpus)

    def test_absent_fields_are_recorded(self):
        """Without these, an invented value is invisible to the harness."""
        truth = next(c for c in load_corpus(TRUTH_DIR) if c.case_id == "SYNTH_BUNDLE")
        deed = truth.document("bundle/bundle_sale_deed.pdf")
        assert "maharera_number" in deed.absent_fields

    def test_expected_clear_captures_false_positives(self):
        truth = next(c for c in load_corpus(TRUTH_DIR) if c.case_id == "SYNTH_BUNDLE")
        assert "MORTGAGE_REG_001" in truth.expected_clear

    def test_missing_directory_yields_empty_corpus(self, tmp_path: Path):
        assert load_corpus(tmp_path / "nope") == []

    def test_bad_file_does_not_stop_the_load(self, tmp_path: Path):
        (tmp_path / "broken.yaml").write_text("{[not yaml", encoding="utf-8")
        (tmp_path / "ok.yaml").write_text(
            "synthetic: true\ncase_id: X\ndocuments: []\n", encoding="utf-8")
        corpus = load_corpus(tmp_path)
        assert [c.case_id for c in corpus] == ["X"]

    def test_real_ground_truth_inside_the_repo_warns(self, tmp_path, caplog):
        """It contains transcribed customer data and belongs outside version control."""
        import logging

        from dmocr.eval import groundtruth as gt

        p = Path(gt.REPO_ROOT) / "eval" / "_tmp_truth.yaml"
        p.write_text("case_id: R\ndocuments: []\n", encoding="utf-8")
        try:
            with caplog.at_level(logging.WARNING):
                load_case_truth(p)
            assert any("outside the repo" in r.message for r in caplog.records)
        finally:
            p.unlink(missing_ok=True)


# =====================================================================================
# Runner
# =====================================================================================


class TestRunner:
    def _run(self, eval_result, **kw):
        return eval_result

    def test_runs_over_the_synthetic_corpus(self, eval_result):
        result = eval_result
        assert result.coverage["cases"] >= 2
        assert result.field_scores
        assert not result.errors

    def test_classification_is_scored(self, eval_result):
        result = eval_result
        assert result.classification.total >= 3

    def test_extraction_has_no_dangerous_errors_on_fixtures(self, eval_result):
        """A regression here means extraction started inventing values."""
        assert eval_result.dangerous_errors == []

    def test_absent_field_correctly_absent_scores_correct(self, eval_result):
        result = eval_result
        scores = [s for s in result.field_scores
                  if s.field_name == "maharera_number"
                  and s.document.endswith("bundle_sale_deed.pdf")]
        assert scores and scores[0].outcome is Outcome.CORRECT

    def test_expected_findings_are_scored(self, eval_result):
        result = eval_result
        assert result.finding_counts().evaluated >= 3

    def test_ocr_is_scored_when_reference_text_exists(self, eval_result):
        result = eval_result
        summary = result.ocr_summary()
        assert summary["documents_scored"] >= 1
        assert summary["cer_mean"] is not None

    def test_values_are_excluded_by_default(self, eval_result):
        """The report must be safe to circulate."""
        assert all(s.expected is None and s.actual is None
                   for s in eval_result.field_scores)

    def test_values_included_only_when_asked(self, eval_result_with_values):
        assert any(s.expected is not None
                   for s in eval_result_with_values.field_scores)

    def test_missing_document_is_reported_not_raised(self, tmp_path: Path):
        result = EvaluationRunner().run(load_corpus(TRUTH_DIR), tmp_path)
        assert result.errors
        assert any("missing document" in e for e in result.errors)

    def test_empty_corpus_is_not_a_pass(self, fixtures_dir: Path):
        result = EvaluationRunner().run([], fixtures_dir)
        assert any("not a pass" in n for n in result.notes)

    def test_small_corpus_is_flagged(self, eval_result):
        result = eval_result
        assert any("Corpus is small" in n for n in result.notes)


# =====================================================================================
# Report and gates
# =====================================================================================


class TestReport:
    def test_report_contains_no_values_by_default(self, eval_result):
        text = render_markdown(eval_result)
        # A value from the fixtures must not appear in a circulatable report.
        assert "Ramesh Patil" not in text
        assert "12500000" not in text

    def test_report_leads_with_coverage(self, eval_result):
        text = render_markdown(eval_result)
        assert text.index("## Coverage") < text.index("## Extraction")

    def test_report_names_the_dangerous_error_rate(self, eval_result):
        text = render_markdown(eval_result)
        assert "Dangerous error rate" in text

    def test_json_report_is_serialisable(self, eval_result):
        import json

        payload = as_dict(eval_result)
        json.dumps(payload)      # must not raise
        assert "coverage" in payload and "extraction" in payload


class TestGates:
    def test_gate_passes_within_threshold(self):
        report = {"extraction": {"overall": {"dangerous_error_rate": 0.01}}}
        gates = [Gate("d", "extraction.overall.dangerous_error_rate", "max", 0.05)]
        assert check_gates(report, gates).passed

    def test_gate_fails_outside_threshold(self):
        report = {"extraction": {"overall": {"dangerous_error_rate": 0.20}}}
        gates = [Gate("d", "extraction.overall.dangerous_error_rate", "max", 0.05)]
        assert not check_gates(report, gates).passed

    def test_min_direction(self):
        report = {"extraction": {"overall": {"recall": 0.5}}}
        gates = [Gate("r", "extraction.overall.recall", "min", 0.70)]
        assert not check_gates(report, gates).passed

    def test_unmeasured_metric_skips_by_default(self):
        """'Not measured' is not 'failed'. A gate that failed here would push toward
        labelling data purely to make CI pass."""
        gates = [Gate("d", "extraction.overall.dangerous_error_rate", "max", 0.05)]
        report = check_gates({}, gates)
        assert report.passed
        assert "gate skipped" in report.results[0].reason

    def test_required_gate_fails_when_unmeasured(self):
        gates = [Gate("d", "a.b", "max", 0.05, required=True)]
        assert not check_gates({}, gates).passed

    def test_default_gates_pass_on_fixtures(self, eval_result):
        gate_report = check_gates(as_dict(eval_result))
        assert gate_report.passed, [r.reason for r in gate_report.failures]
