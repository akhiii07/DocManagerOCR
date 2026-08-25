"""Tests for the rule engine.

Focused on the safety properties: unapproved rules cannot enforce, NOT_DETERMINABLE never
blocks, applicability carve-outs prevent false positives, and a crashed check is reported
rather than swallowed.
"""

from __future__ import annotations

from datetime import date

import pytest

from dmocr.model import (
    Area,
    AreaUnit,
    AreaValue,
    Case,
    Claim,
    Determinacy,
    Determination,
    Disposition,
    Document,
    DocumentProvenance,
    DocumentType,
    InstrumentStrength,
    LenderType,
    Money,
    MoneyValue,
    Product,
    Property,
    SecurityType,
    Severity,
    TextValue,
    TransactionType,
    derive_disposition,
)
from dmocr.rules import (
    ExecutionMode,
    LegalSignoff,
    RuleEngine,
    RuleSet,
    RuleSpec,
    RuleStatus,
    registered_names,
    summarise,
)

RULES_PATH = "rules/mvp.yaml"


def make_case(**kw) -> Case:
    kw.setdefault("tenant_id", "T1")
    kw.setdefault("lender_type", LenderType.HFC)
    kw.setdefault("product", Product.HOUSING_LOAN)
    return Case(**kw)


def add_doc(case: Case, dtype: DocumentType, **kw) -> Document:
    d = Document(case_id=case.case_id, tenant_id=case.tenant_id,
                 document_type=dtype, sha256="a" * 64, **kw)
    case.add_document(d)
    return d


def add_claim(prop: Property, attribute, value, doc_id="DOC1", strength=None) -> Claim:
    c = Claim(
        subject_id=prop.property_id,
        attribute=attribute,
        value=value,
        provenance=DocumentProvenance(document_id=doc_id, page=1),
        instrument_strength=strength,
    )
    prop.add_claim(c)
    return c


def approved(spec_kwargs: dict) -> RuleSpec:
    """A rule with sign-off, for testing ENFORCE paths."""
    spec_kwargs.setdefault("legal_signoff",
                           LegalSignoff(by="legal@example.com", at=date(2026, 1, 1)))
    spec_kwargs.setdefault("status", RuleStatus.APPROVED)
    return RuleSpec(**spec_kwargs)


# =====================================================================================
# Sign-off gate
# =====================================================================================


class TestSignoffGate:
    def test_approved_rule_without_signoff_is_rejected_at_load(self):
        with pytest.raises(Exception, match="legal_signoff"):
            RuleSpec(
                rule_id="R1", version="1.0.0", title="t", category="c",
                severity=Severity.HIGH, determinacy=Determinacy.DETERMINISTIC,
                check="documents_present", status=RuleStatus.APPROVED,
            )

    def test_shipped_rule_set_has_nothing_approved(self):
        """Rules ship disabled. If this fails, something was signed off in code."""
        rs = RuleSet.from_yaml(RULES_PATH)
        assert rs.enforceable() == []

    def test_enforce_mode_produces_no_findings_for_unapproved_rules(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        findings = RuleEngine(rs).evaluate(make_case(), mode=ExecutionMode.ENFORCE)
        assert findings == []

    def test_dry_run_evaluates_drafts_and_marks_them_advisory(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        findings = RuleEngine(rs).evaluate(make_case(), mode=ExecutionMode.DRY_RUN)
        assert findings
        assert all(f.advisory_only for f in findings)


# =====================================================================================
# Disposition matrix
# =====================================================================================


class TestDisposition:
    def test_not_applicable_is_never_a_finding(self):
        assert derive_disposition(
            Determination.NOT_APPLICABLE, Severity.CRITICAL, Determinacy.DETERMINISTIC
        ) is Disposition.NOT_APPLICABLE

    def test_not_determinable_never_blocks_even_at_critical(self):
        """We did not establish anything, so we must not stop the case."""
        assert derive_disposition(
            Determination.NOT_DETERMINABLE, Severity.CRITICAL, Determinacy.DETERMINISTIC
        ) is Disposition.REVIEW_REQUIRED

    def test_not_determinable_low_severity_is_informational(self):
        assert derive_disposition(
            Determination.NOT_DETERMINABLE, Severity.LOW, Determinacy.DETERMINISTIC
        ) is Disposition.INFORMATIONAL

    def test_machine_certain_and_serious_blocks(self):
        assert derive_disposition(
            Determination.MISMATCH, Severity.CRITICAL, Determinacy.DETERMINISTIC
        ) is Disposition.BLOCKER

    def test_model_proposed_never_blocks_however_alarming(self):
        """A model's opinion goes to a human, not to a blocker."""
        assert derive_disposition(
            Determination.MISMATCH, Severity.CRITICAL, Determinacy.MODEL_PROPOSED
        ) is Disposition.REVIEW_REQUIRED

    def test_match_clears(self):
        assert derive_disposition(
            Determination.MATCH, Severity.HIGH, Determinacy.DETERMINISTIC
        ) is Disposition.CLEARED


# =====================================================================================
# Applicability
# =====================================================================================


class TestApplicability:
    def test_ltv_rule_not_applicable_to_a_bank(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        case = make_case(lender_type=LenderType.BANK)
        findings = RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
        ltv = next(f for f in findings if f.rule_id == "LTV_CAP_001")
        assert ltv.disposition is Disposition.NOT_APPLICABLE
        assert "lender type" in ltv.message

    def test_custody_rule_only_applies_to_equitable_mortgage(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        engine = RuleEngine(rs)

        simple = make_case(security_type=SecurityType.SIMPLE)
        f = next(x for x in engine.evaluate(simple, mode=ExecutionMode.DRY_RUN)
                 if x.rule_id == "CUSTODY_001")
        assert f.disposition is Disposition.NOT_APPLICABLE

        equitable = make_case(security_type=SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS)
        f = next(x for x in engine.evaluate(equitable, mode=ExecutionMode.DRY_RUN)
                 if x.rule_id == "CUSTODY_001")
        assert f.disposition is not Disposition.NOT_APPLICABLE

    def test_effective_dates_are_honoured(self):
        spec = approved({
            "rule_id": "R_DATED", "version": "1.0.0", "title": "t", "category": "c",
            "severity": Severity.HIGH, "determinacy": Determinacy.DETERMINISTIC,
            "check": "documents_present",
            "applicability": {"effective_from": date(2030, 1, 1)},
        })
        findings = RuleEngine(RuleSet(version="t", rules=[spec])).evaluate(
            make_case(), as_of=date(2026, 8, 24)
        )
        assert findings[0].disposition is Disposition.NOT_APPLICABLE
        assert "not in force" in findings[0].message


# =====================================================================================
# The Mumbai carve-out - the false positive we are most worried about
# =====================================================================================


class TestMortgageRegistrationCarveOut:
    def _run(self, case):
        rs = RuleSet.from_yaml(RULES_PATH)
        return next(f for f in RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
                    if f.rule_id == "MORTGAGE_REG_001")

    def test_equitable_mortgage_is_not_a_registration_defect(self):
        """TPA s.59 excepts deposit of title-deeds. This must NOT be a finding."""
        case = make_case(security_type=SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS)
        f = self._run(case)
        assert f.determination is Determination.NOT_APPLICABLE
        assert f.disposition is Disposition.NOT_APPLICABLE

    def test_simple_mortgage_without_a_deed_is_flagged(self):
        case = make_case(security_type=SecurityType.SIMPLE)
        f = self._run(case)
        assert f.determination is Determination.MISSING
        assert f.disposition is Disposition.BLOCKER

    def test_unknown_security_type_is_not_determinable_not_a_defect(self):
        case = make_case(security_type=SecurityType.UNKNOWN)
        f = self._run(case)
        assert f.determination is Determination.NOT_DETERMINABLE
        assert f.disposition is not Disposition.BLOCKER


# =====================================================================================
# Ownership - TPA s.54
# =====================================================================================


class TestOwnership:
    def _run(self, case):
        rs = RuleSet.from_yaml(RULES_PATH)
        return next(f for f in RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
                    if f.rule_id == "OWNERSHIP_001")

    def test_agreement_of_sale_alone_does_not_establish_ownership(self):
        case = make_case()
        add_doc(case, DocumentType.AGREEMENT_OF_SALE)
        prop = Property()
        add_claim(prop, "party.owner", TextValue(raw="A Kumar"),
                  strength=InstrumentStrength.CONTRACTUAL)
        case.properties.append(prop)

        f = self._run(case)
        assert f.determination is Determination.MISSING
        assert f.disposition is Disposition.BLOCKER
        assert "s.54" in f.message or "contract" in f.message.lower()

    def test_sale_deed_establishes_ownership(self):
        case = make_case()
        add_doc(case, DocumentType.SALE_DEED)
        prop = Property()
        add_claim(prop, "party.owner", TextValue(raw="A Kumar"),
                  strength=InstrumentStrength.TITLE_TRANSFERRING)
        add_claim(prop, "party.owner", TextValue(raw="A Kumar"), doc_id="DOC2",
                  strength=InstrumentStrength.ADMINISTRATIVE)
        case.properties.append(prop)

        f = self._run(case)
        assert f.determination is Determination.MATCH
        assert f.disposition is Disposition.CLEARED

    def test_rule_carries_its_regulatory_citations(self):
        f = self._run(make_case())
        assert f.is_regulatory
        assert "REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST" in f.citations


# =====================================================================================
# LTV
# =====================================================================================


class TestLtv:
    def _case(self, outstanding, value, **kw):
        case = make_case(**kw)
        case.loan.total_outstanding = Money.from_rupees(outstanding)
        case.loan.property_value_for_ltv = Money.from_rupees(value)
        return case

    def _run(self, case, rule_id="LTV_CAP_001"):
        rs = RuleSet.from_yaml(RULES_PATH)
        return next(f for f in RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
                    if f.rule_id == rule_id)

    def test_within_cap_clears(self):
        f = self._run(self._case(2_500_000, 3_000_000))   # 83% on the 90% slab
        assert f.determination is Determination.MATCH

    def test_above_cap_blocks(self):
        f = self._run(self._case(2_900_000, 3_000_000))   # 96.67% on the 90% slab
        assert f.determination is Determination.MISMATCH
        assert f.disposition is Disposition.BLOCKER

    def test_slab_boundary_is_inclusive_at_thirty_lakh(self):
        """"up to Rs.30 lakh" includes exactly Rs.30,00,000, so the cap is 90%, not 80%."""
        # 85% LTV: passes under the 90% slab, would fail under the 80% slab.
        f = self._run(self._case(3_000_000, 3_529_412))
        assert f.determination is Determination.MATCH

    def test_just_above_thirty_lakh_uses_the_eighty_percent_slab(self):
        f = self._run(self._case(3_000_001, 3_529_412))   # ~85%
        assert f.determination is Determination.MISMATCH

    def test_missing_inputs_are_not_determinable_not_a_pass(self):
        case = make_case()
        f = self._run(case)
        assert f.determination is Determination.NOT_DETERMINABLE
        assert f.disposition is not Disposition.CLEARED

    def test_annex_xiv_value_above_documented_consideration_is_flagged(self):
        case = self._case(4_000_000, 6_000_000)
        case.loan.transaction_type = TransactionType.INITIAL_PURCHASE
        prop = Property()
        add_claim(prop, "transaction.consideration",
                  MoneyValue(amount=Money.from_rupees(5_000_000)))
        add_claim(prop, "transaction.consideration",
                  MoneyValue(amount=Money.from_rupees(5_000_000)), doc_id="DOC2")
        case.properties.append(prop)

        f = self._run(case, "LTV_CONSIDERATION_001")
        assert f.determination is Determination.MISMATCH

    def test_annex_xiv_not_applicable_to_loan_against_owned_property(self):
        case = self._case(4_000_000, 6_000_000)
        case.loan.transaction_type = TransactionType.LOAN_AGAINST_OWNED_PROPERTY
        f = self._run(case, "LTV_CONSIDERATION_001")
        assert f.determination is Determination.NOT_APPLICABLE

    def test_conflicting_consideration_makes_the_check_indeterminate(self):
        """A cross-document conflict must not silently resolve into a pass or fail."""
        case = self._case(4_000_000, 6_000_000)
        case.loan.transaction_type = TransactionType.INITIAL_PURCHASE
        prop = Property()
        add_claim(prop, "transaction.consideration",
                  MoneyValue(amount=Money.from_rupees(5_000_000)))
        add_claim(prop, "transaction.consideration",
                  MoneyValue(amount=Money.from_rupees(7_000_000)), doc_id="DOC2")
        case.properties.append(prop)

        f = self._run(case, "LTV_CONSIDERATION_001")
        assert f.determination is Determination.NOT_DETERMINABLE


# =====================================================================================
# Cross-document consistency
# =====================================================================================


class TestCrossDocument:
    def _run(self, case):
        rs = RuleSet.from_yaml(RULES_PATH)
        return next(f for f in RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
                    if f.rule_id == "XDOC_AREA_001")

    def test_area_mismatch_is_flagged_and_is_a_business_rule(self):
        case = make_case()
        prop = Property()
        add_claim(prop, "property.area", AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)))
        add_claim(prop, "property.area", AreaValue(area=Area.of(2210, AreaUnit.SQ_FT)),
                  doc_id="DOC2")
        case.properties.append(prop)

        f = self._run(case)
        assert f.determination is Determination.MISMATCH
        assert f.disposition is Disposition.BLOCKER
        # No citations -> business rule, and the package must not imply regulatory backing.
        assert not f.is_regulatory

    def test_rounding_within_tolerance_clears(self):
        case = make_case()
        prop = Property()
        add_claim(prop, "property.area", AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)))
        add_claim(prop, "property.area", AreaValue(area=Area.of(2390, AreaUnit.SQ_FT)),
                  doc_id="DOC2")
        case.properties.append(prop)
        assert self._run(case).determination is Determination.MATCH


# =====================================================================================
# Failure handling and summary
# =====================================================================================


class TestVerificationAwareRules:
    """Rules that consume external verification results.

    The highest-stakes predicate in the system: a prior registered charge threatens the
    recoverability of the security, and an unreachable registry must never look like one.
    """

    def _case_with(self, *results, **kw) -> Case:
        from dmocr.model import Property

        case = make_case(**kw)
        case.properties.append(Property())
        case.verification_results = list(results)
        return case

    def _result(self, **kw):
        from dmocr.model.verification import AccessTier, VerificationResult

        kw.setdefault("source_id", "SRC_CERSAI")
        kw.setdefault("authority", "CERSAI")
        kw.setdefault("attribute", "property.encumbrance")
        kw.setdefault("tier", AccessTier.T2_LICENSED)
        return VerificationResult(**kw)

    def _finding(self, case: Case, rule_id: str):
        rs = RuleSet.from_yaml(RULES_PATH)
        return next(f for f in RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
                    if f.rule_id == rule_id)

    # -- prior charge ------------------------------------------------------------

    def test_prior_charge_blocks(self):
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            status=VerificationStatus.NOT_APPLICABLE,
            external_value="Mortgage in favour of XYZ Bank",
            snapshot_id="SNAP_1",
        ))
        f = self._finding(case, "EXT_CERSAI_CHARGE_001")
        assert f.determination is Determination.MISMATCH
        assert f.disposition is Disposition.BLOCKER
        assert f.severity is Severity.CRITICAL
        assert "XYZ Bank" in f.message

    def test_charge_recorded_as_not_applicable_is_still_present(self):
        """Regression. A CERSAI hit arrives as NOT_APPLICABLE because there is nothing in
        the borrower's own documents to compare it against. Filtering that out made a real
        prior charge report NOT_DETERMINABLE - i.e. invisible."""
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            status=VerificationStatus.NOT_APPLICABLE,
            external_value="Charge filed 2023-06-11",
        ))
        assert self._finding(
            case, "EXT_CERSAI_CHARGE_001").determination is Determination.MISMATCH

    def test_no_charge_on_the_register_clears(self):
        """Absence is the GOOD answer here - the one place in the rule set where it is."""
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            status=VerificationStatus.NOT_FOUND_IN_SOURCE))
        f = self._finding(case, "EXT_CERSAI_CHARGE_001")
        assert f.determination is Determination.MATCH
        assert f.disposition is Disposition.CLEARED

    def test_unreachable_registry_never_blocks(self):
        """CRITICAL severity, but nothing was established."""
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            status=VerificationStatus.SOURCE_UNAVAILABLE))
        f = self._finding(case, "EXT_CERSAI_CHARGE_001")
        assert f.determination is Determination.NOT_DETERMINABLE
        assert f.disposition is not Disposition.BLOCKER
        assert "not a failure" in f.message

    def test_pending_operator_task_never_blocks(self):
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            status=VerificationStatus.PENDING_MANUAL))
        f = self._finding(case, "EXT_CERSAI_CHARGE_001")
        assert f.determination is Determination.NOT_DETERMINABLE
        assert f.disposition is not Disposition.BLOCKER

    def test_no_verification_at_all_is_not_determinable(self):
        f = self._finding(self._case_with(), "EXT_CERSAI_CHARGE_001")
        assert f.determination is Determination.NOT_DETERMINABLE

    def test_charge_rule_carries_its_regulatory_citations(self):
        f = self._finding(self._case_with(), "EXT_CERSAI_CHARGE_001")
        assert f.is_regulatory
        assert "REQ_SARFAESI_26C_PUBLIC_NOTICE_AND_PRIORITY" in f.citations

    # -- agreement ---------------------------------------------------------------

    def test_owner_mismatch_against_the_land_record(self):
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(self._result(
            source_id="SRC_PROPERTY_CARD_MH", authority="City Survey Office",
            attribute="party.owner", tier=AccessTier.T5_OFFLINE,
            status=VerificationStatus.MISMATCH,
            internal_value="Ramesh Patil", external_value="Suresh Kulkarni",
        ))
        f = self._finding(case, "EXT_OWNER_MATCH_001")
        assert f.determination is Determination.MISMATCH
        assert "Suresh Kulkarni" in f.message

    def test_owner_agreement_clears(self):
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(self._result(
            source_id="SRC_PROPERTY_CARD_MH", authority="City Survey Office",
            attribute="party.owner", tier=AccessTier.T5_OFFLINE,
            status=VerificationStatus.MATCH,
            internal_value="Ramesh Patil", external_value="R. Patil",
        ))
        assert self._finding(
            case, "EXT_OWNER_MATCH_001").disposition is Disposition.CLEARED

    def test_the_worst_status_wins_across_sources(self):
        """One contradiction must not be hidden by other sources agreeing."""
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(
            self._result(source_id="SRC_A", attribute="party.owner",
                         tier=AccessTier.T2_LICENSED,
                         status=VerificationStatus.MATCH, external_value="X"),
            self._result(source_id="SRC_B", attribute="party.owner",
                         tier=AccessTier.T2_LICENSED,
                         status=VerificationStatus.MISMATCH, external_value="Y"),
        )
        assert self._finding(
            case, "EXT_OWNER_MATCH_001").determination is Determination.MISMATCH

    def test_stale_record_is_not_agreement_nor_contradiction(self):
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(self._result(
            attribute="party.owner", tier=AccessTier.T5_OFFLINE,
            status=VerificationStatus.STALE, external_value="X"))
        assert self._finding(
            case, "EXT_OWNER_MATCH_001").determination is Determination.NOT_DETERMINABLE

    def test_source_out_of_scope_is_not_applicable(self):
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            source_id="SRC_MAHARERA", authority="MahaRERA", attribute="*",
            status=VerificationStatus.NOT_APPLICABLE,
            detail="No MahaRERA registration number extracted.",
        ))
        # The wildcard result answers any attribute question about that source.
        f = self._finding(case, "EXT_OWNER_MATCH_001")
        assert f.determination in (Determination.NOT_APPLICABLE,
                                   Determination.NOT_DETERMINABLE)

    def test_insufficient_tier_cannot_support_a_conclusion(self):
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(self._result(
            attribute="party.owner", tier=AccessTier.T6_UNAVAILABLE,
            status=VerificationStatus.MISMATCH, external_value="X"))
        assert self._finding(
            case, "EXT_OWNER_MATCH_001").determination is Determination.NOT_DETERMINABLE

    # -- coverage ----------------------------------------------------------------

    def test_coverage_reports_partial_completion(self):
        from dmocr.model.verification import AccessTier, VerificationStatus

        case = self._case_with(
            self._result(status=VerificationStatus.NOT_FOUND_IN_SOURCE),
            self._result(source_id="SRC_MCGM_PTAX", attribute="tax.assessment_number",
                         tier=AccessTier.T4_PORTAL_MANUAL,
                         status=VerificationStatus.PENDING_MANUAL),
        )
        f = self._finding(case, "EXT_COVERAGE_001")
        assert f.determination is Determination.PARTIAL_MATCH
        assert "1 of 2" in f.message or "1 of 2" in f.evidence.note

    def test_coverage_is_not_applicable_when_no_source_was_in_scope(self):
        from dmocr.model.verification import VerificationStatus

        case = self._case_with(self._result(
            attribute="*", status=VerificationStatus.NOT_APPLICABLE))
        assert self._finding(
            case, "EXT_COVERAGE_001").disposition is Disposition.NOT_APPLICABLE

    def test_coverage_without_any_verification(self):
        assert self._finding(
            self._case_with(), "EXT_COVERAGE_001").determination is Determination.NOT_DETERMINABLE


class TestEngineRobustness:
    def test_a_crashed_check_is_reported_not_swallowed(self):
        from dmocr.rules.registry import predicate

        @predicate("_boom")
        def _boom(case, params):
            raise RuntimeError("kaboom")

        spec = approved({
            "rule_id": "R_BOOM", "version": "1.0.0", "title": "t", "category": "c",
            "severity": Severity.HIGH, "determinacy": Determinacy.DETERMINISTIC,
            "check": "_boom",
        })
        f = RuleEngine(RuleSet(version="t", rules=[spec])).evaluate(make_case())[0]
        assert f.determination is Determination.NOT_DETERMINABLE
        assert "not a pass" in f.message

    def test_unknown_predicate_surfaces_as_not_determinable(self):
        spec = approved({
            "rule_id": "R_MISSING", "version": "1.0.0", "title": "t", "category": "c",
            "severity": Severity.HIGH, "determinacy": Determinacy.DETERMINISTIC,
            "check": "no_such_predicate",
        })
        f = RuleEngine(RuleSet(version="t", rules=[spec])).evaluate(make_case())[0]
        assert f.determination is Determination.NOT_DETERMINABLE

    def test_duplicate_rule_ids_rejected(self):
        spec = approved({
            "rule_id": "DUP", "version": "1.0.0", "title": "t", "category": "c",
            "severity": Severity.HIGH, "determinacy": Determinacy.DETERMINISTIC,
            "check": "documents_present",
        })
        with pytest.raises(Exception, match="duplicate"):
            RuleSet(version="t", rules=[spec, spec])

    def test_all_shipped_rules_reference_a_registered_predicate(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        names = set(registered_names())
        unknown = [r.rule_id for r in rs.rules if r.check not in names]
        assert unknown == []

    def test_summary_separates_not_determinable_from_failures(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        findings = RuleEngine(rs).evaluate(make_case(), mode=ExecutionMode.DRY_RUN)
        s = summarise(findings)
        assert s["total"] == len(rs.rules)
        assert "not_determinable" in s
        assert s["regulatory"] + s["business_rules"] == s["total"]

    def test_findings_sorted_worst_first(self):
        rs = RuleSet.from_yaml(RULES_PATH)
        case = make_case(security_type=SecurityType.SIMPLE)
        findings = RuleEngine(rs).evaluate(case, mode=ExecutionMode.DRY_RUN)
        order = [f.disposition for f in findings]
        assert order == sorted(order, key=lambda d: [
            Disposition.BLOCKER, Disposition.REVIEW_REQUIRED, Disposition.INFORMATIONAL,
            Disposition.CLEARED, Disposition.NOT_APPLICABLE,
        ].index(d))
