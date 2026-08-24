"""End-to-end pipeline tests.

Two themes:

* **The whole path works on a real bundle** — three documents describing one property,
  with a deliberate area conflict, run from bytes to findings.
* **Every stage degrades rather than aborting.** A blocked, rejected, duplicate or
  unclassifiable document leaves the case processable, and each is reported. A gap in the
  bundle must never be silent.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from dmocr.ingest import InMemoryContentStore, QualityThresholds
from dmocr.model import (
    Case,
    Determination,
    Disposition,
    DocumentType,
    LenderType,
    Money,
    Product,
    SecurityType,
    TransactionType,
)
from dmocr.ocr import UnavailableEngine
from dmocr.pipeline import CasePipeline, render_summary
from dmocr.rules import ExecutionMode, RuleSet

RULES = "rules/mvp.yaml"


def make_case(**kw) -> Case:
    kw.setdefault("tenant_id", "T1")
    kw.setdefault("lender_type", LenderType.HFC)
    kw.setdefault("product", Product.HOUSING_LOAN)
    return Case(**kw)


def make_pipeline(*, mode=ExecutionMode.DRY_RUN, with_rules=True, **kw) -> CasePipeline:
    return CasePipeline(
        InMemoryContentStore(),
        # The bundle carries a text layer, so no OCR engine is needed. This also proves
        # the pipeline runs on a machine with no OCR installed.
        ocr_engine=UnavailableEngine("text layer only"),
        rule_set=RuleSet.from_yaml(RULES) if with_rules else None,
        rule_mode=mode,
        **kw,
    )


def files_from(directory: Path) -> list[tuple[str, bytes]]:
    return [(p.name, p.read_bytes()) for p in sorted(directory.iterdir()) if p.is_file()]


# =====================================================================================
# The happy path
# =====================================================================================


class TestBundle:
    def test_all_three_documents_are_classified(self, bundle_dir: Path):
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        types = {d.document_type for d in result.documents}
        assert types == {
            DocumentType.SALE_DEED,
            DocumentType.AGREEMENT_OF_SALE,
            DocumentType.PROPERTY_TAX,
        }

    def test_no_ocr_was_needed(self, bundle_dir: Path):
        """Never OCR what you can read - the bundle is all text-layer."""
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        assert all(d.ocr_pages == 0 for d in result.documents)
        assert all(d.text_layer_pages >= 1 for d in result.documents)

    def test_fields_are_extracted_from_every_document(self, bundle_dir: Path):
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        assert all(d.fields_extracted > 0 for d in result.documents)

    def test_claims_are_assembled_onto_one_property(self, bundle_dir: Path):
        case = make_case()
        result = make_pipeline().process_directory(case, bundle_dir)
        assert len(case.properties) == 1
        assert result.assembly.claims_added > 0

    def test_parties_are_resolved_across_documents(self, bundle_dir: Path):
        """'Shri Ramesh Patil' in the deed and 'R. Patil' in the agreement are one person."""
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        assert any(d.action == "merged" for d in result.assembly.decisions)
        assert len(result.assembly.parties_for("party.seller")) == 1

    def test_area_conflict_is_surfaced_as_a_blocker(self, bundle_dir: Path):
        """The tax bill says 980 sq ft; the deed and agreement say 1150."""
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        area = next(f for f in result.findings if f.rule_id == "XDOC_AREA_001")
        assert area.determination is Determination.MISMATCH
        assert area.disposition is Disposition.BLOCKER

    def test_parcel_identifier_agrees(self, bundle_dir: Path):
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        parcel = next(f for f in result.findings if f.rule_id == "XDOC_PARCEL_001")
        assert parcel.determination is Determination.MATCH

    def test_processing_context_is_pinned(self, bundle_dir: Path):
        """A finding must remain explainable under the versions then in force."""
        case = make_case()
        make_pipeline().process_directory(case, bundle_dir,
                                          regulatory_as_of=date(2026, 8, 24))
        ctx = case.processing_context
        assert ctx is not None
        assert ctx.pipeline_version
        assert ctx.rule_set_version
        assert ctx.regulatory_as_of == date(2026, 8, 24)
        assert "ocr" in ctx.model_versions


# =====================================================================================
# Rules over an assembled case
# =====================================================================================


class TestRulesOverBundle:
    def _findings(self, bundle_dir: Path, case: Case):
        return {f.rule_id: f
                for f in make_pipeline().process_directory(case, bundle_dir).findings}

    def test_mumbai_equitable_mortgage_is_not_a_registration_defect(self, bundle_dir: Path):
        """TPA s.59 carve-out, end to end on a real bundle."""
        case = make_case(security_type=SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS)
        f = self._findings(bundle_dir, case)["MORTGAGE_REG_001"]
        assert f.determination is Determination.NOT_APPLICABLE

    def test_ltv_clears_when_within_the_cap(self, bundle_dir: Path):
        case = make_case()
        case.loan.total_outstanding = Money.from_rupees(9_000_000)
        case.loan.property_value_for_ltv = Money.from_rupees(12_500_000)   # 72%
        f = self._findings(bundle_dir, case)["LTV_CAP_001"]
        assert f.determination is Determination.MATCH

    def test_annex_xiv_cap_uses_the_extracted_consideration(self, bundle_dir: Path):
        """The documented consideration comes from extraction, not configuration."""
        case = make_case()
        case.loan.transaction_type = TransactionType.INITIAL_PURCHASE
        case.loan.total_outstanding = Money.from_rupees(9_000_000)
        case.loan.property_value_for_ltv = Money.from_rupees(20_000_000)  # above 1.25 cr
        f = self._findings(bundle_dir, case)["LTV_CONSIDERATION_001"]
        assert f.determination is Determination.MISMATCH

    def test_missing_expected_document_is_reported(self, bundle_dir: Path):
        case = make_case(expected_documents=[
            DocumentType.SALE_DEED, DocumentType.POSSESSION_DOCUMENT])
        f = self._findings(bundle_dir, case)["DOC_COMPLETENESS_001"]
        assert f.determination is Determination.MISSING
        assert "possession_document" in f.message

    def test_enforce_mode_yields_nothing_because_no_rule_is_approved(self, bundle_dir: Path):
        pipeline = make_pipeline(mode=ExecutionMode.ENFORCE)
        result = pipeline.process_directory(make_case(), bundle_dir)
        assert result.findings == []
        assert any("No rules are APPROVED" in n for n in result.notes)

    def test_dry_run_findings_are_advisory(self, bundle_dir: Path):
        result = make_pipeline().process_directory(make_case(), bundle_dir)
        assert all(f.advisory_only for f in result.findings)

    def test_no_rule_set_is_reported(self, bundle_dir: Path):
        result = make_pipeline(with_rules=False).process_directory(
            make_case(), bundle_dir)
        assert result.findings == []
        assert any("No rule set configured" in n for n in result.notes)


# =====================================================================================
# Degrading honestly
# =====================================================================================


class TestDegradation:
    def test_blocked_upload_is_skipped_and_the_case_still_processes(self, bundle_dir: Path):
        files = files_from(bundle_dir)
        files.append(("evil.pdf", b"%PDF-1.4\n<< /JavaScript (x) >>\n%%EOF\n"))
        result = make_pipeline().process(make_case(), files)

        blocked = [d for d in result.documents if d.filename == "evil.pdf"]
        assert blocked and blocked[0].skipped_reason
        assert not blocked[0].ingested
        # The rest of the bundle still went through.
        assert sum(1 for d in result.documents if d.fields_extracted > 0) == 3

    def test_rejected_quality_document_is_visible_not_silent(self, bundle_dir: Path):
        pipeline = make_pipeline(quality_thresholds=QualityThresholds(max_pages=0))
        result = pipeline.process(make_case(), files_from(bundle_dir))
        assert all(d.skipped_reason for d in result.documents)
        assert all(d.needs_human for d in result.documents)

    def test_unclassifiable_document_is_not_parsed_with_a_guessed_schema(self, bundle_dir):
        files = files_from(bundle_dir)
        # A valid, readable PDF whose content matches no document type.
        from tools.make_fixtures import lines_pdf  # type: ignore
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "mystery.pdf"
            lines_pdf(p, ["lorem ipsum dolor sit amet", "consectetur adipiscing elit"])
            files.append((p.name, p.read_bytes()))
            result = make_pipeline().process(make_case(), files)

        mystery = next(d for d in result.documents if d.filename == "mystery.pdf")
        assert mystery.document_type is DocumentType.UNKNOWN
        assert mystery.fields_extracted == 0
        assert "human review" in (mystery.skipped_reason or "")

    def test_duplicate_upload_is_detected(self, bundle_dir: Path):
        files = files_from(bundle_dir)
        files.append(files[0])          # same bytes, same name
        result = make_pipeline().process(make_case(), files)
        dupes = [d for d in result.documents
                 if d.skipped_reason and "Duplicate" in d.skipped_reason]
        assert len(dupes) == 1

    def test_empty_case_does_not_crash(self):
        result = make_pipeline().process(make_case(), [])
        assert result.documents == []
        assert result.assembly is not None
        assert any("cross-document agreement cannot be established" in n
                   for n in result.notes)


# =====================================================================================
# Review package
# =====================================================================================


class TestSummary:
    def test_summary_leads_with_what_needs_attention(self, bundle_dir: Path):
        case = make_case(expected_documents=[
            DocumentType.SALE_DEED, DocumentType.POSSESSION_DOCUMENT])
        text = render_summary(make_pipeline().process_directory(case, bundle_dir))

        assert "COLLATERAL DOCUMENT REVIEW" in text
        assert "BLOCKER" in text
        assert "ENTITY RESOLUTION" in text
        # Cleared checks are not listed - the package answers "what needs attention?"
        assert "CLEARED" not in text

    def test_summary_shows_resolution_decisions(self, bundle_dir: Path):
        text = render_summary(make_pipeline().process_directory(make_case(), bundle_dir))
        assert "merged" in text

    def test_summary_marks_advisory_findings(self, bundle_dir: Path):
        text = render_summary(make_pipeline().process_directory(make_case(), bundle_dir))
        assert "[advisory]" in text
