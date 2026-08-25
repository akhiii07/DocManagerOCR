"""Tests for external verification.

The invariants that matter most:

* **`SOURCE_UNAVAILABLE` is never a compliance failure.** A portal being down says nothing
  about the collateral.
* **Data minimisation.** One lookup key per source — the narrowest that resolves a record.
  An external lookup is an outbound disclosure of customer data.
* **Out-of-scope sources are reported, not dropped.** A reviewer must see that a source
  was considered and why it did not apply.
* **A manual result re-enters the same comparison path** as an automated one.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from dmocr.model import (
    Area,
    AreaUnit,
    AreaValue,
    Case,
    Claim,
    ConfidenceTier,
    LenderType,
    ParcelIdentifier,
    ParcelIdentifierType,
    ParcelValue,
    Product,
    Property,
    TextValue,
)
from dmocr.model.provenance import DocumentProvenance
from dmocr.verify import (
    AccessTier,
    AdapterRegistry,
    Execution,
    ExternalObservation,
    Snapshot,
    SourceUnavailable,
    StaticAdapter,
    TaskQueue,
    TaskStatus,
    VerificationOrchestrator,
    VerificationPlanner,
    VerificationStatus,
    compare_values,
    default_sources,
    load_sources,
    render_verification,
    sources_for_attribute,
)


def claim(prop: Property, attribute: str, value, doc_id="D1") -> Claim:
    c = Claim(
        subject_id=prop.property_id, attribute=attribute, value=value,
        provenance=DocumentProvenance(document_id=doc_id, page=1),
    )
    prop.add_claim(c)
    return c


def make_case(*, with_property: bool = True, **kw) -> Case:
    kw.setdefault("tenant_id", "T1")
    kw.setdefault("lender_type", LenderType.HFC)
    kw.setdefault("product", Product.HOUSING_LOAN)
    case = Case(**kw)
    if with_property:
        prop = Property()
        prop.add_parcel_identifier(
            ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="1234/5A"))
        claim(prop, "property.parcel_identifier", ParcelValue(
            identifier=ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="1234/5A")))
        claim(prop, "property.area", AreaValue(
            area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet"))
        claim(prop, "party.owner", TextValue(raw="Ramesh Patil", normalised="RAMESH PATIL"))
        case.properties.append(prop)
    return case


def observation(source_id="SRC_PROPERTY_CARD_MH", tier=AccessTier.T5_OFFLINE,
                fields=None, *, found=True, as_of=None) -> ExternalObservation:
    return ExternalObservation(
        snapshot=Snapshot(source_id=source_id, authority="Test Authority",
                          retrieved_at=datetime.now(), tier=tier),
        fields=fields or {},
        record_found=found,
        record_as_of=as_of,
    )


# =====================================================================================
# Source registry
# =====================================================================================


class TestSourceRegistry:
    def test_registry_loads_from_the_b0_research_file(self):
        """The registry is not duplicated in code, so tiers cannot drift from research."""
        sources = default_sources()
        assert {"SRC_CERSAI", "SRC_IGR_ESEARCH", "SRC_MAHARERA",
                "SRC_MCGM_PTAX", "SRC_PROPERTY_CARD_MH"} <= set(sources)

    def test_only_cersai_is_automatable(self):
        """B0's conclusion: the others are CAPTCHA-gated or terms-restricted."""
        auto = [s.source_id for s in default_sources().values() if s.is_automatable]
        assert auto == ["SRC_CERSAI"]

    def test_a_tier_range_resolves_to_the_worse_tier(self):
        """'T1_OR_T2' plans as T2. Planning optimistically would promise automation the
        environment cannot deliver."""
        assert default_sources()["SRC_CERSAI"].tier is AccessTier.T2_LICENSED
        assert default_sources()["SRC_PROPERTY_CARD_MH"].tier is AccessTier.T5_OFFLINE

    def test_blocked_sources_are_not_automatable(self):
        igr = default_sources()["SRC_IGR_ESEARCH"]
        assert igr.blocked_on
        assert not igr.is_automatable and igr.needs_human

    def test_missing_registry_degrades_to_empty(self, tmp_path):
        assert load_sources(tmp_path / "nope.yaml") == {}

    def test_sources_for_attribute(self):
        found = sources_for_attribute("property.area")
        assert any(s.source_id == "SRC_PROPERTY_CARD_MH" for s in found)


class TestAccessTier:
    @pytest.mark.parametrize("tier,automatable", [
        (AccessTier.T1_OFFICIAL_API, True), (AccessTier.T2_LICENSED, True),
        (AccessTier.T3_PORTAL_PERMITTED, True), (AccessTier.T4_PORTAL_MANUAL, False),
        (AccessTier.T5_OFFLINE, False), (AccessTier.T6_UNAVAILABLE, False),
    ])
    def test_automatability(self, tier, automatable):
        assert tier.is_automatable is automatable

    def test_tier_caps_confidence(self):
        """A statutory API and an operator's screenshot are not worth the same."""
        assert AccessTier.T1_OFFICIAL_API.confidence_ceiling is ConfidenceTier.HIGH
        assert AccessTier.T4_PORTAL_MANUAL.confidence_ceiling is ConfidenceTier.MEDIUM
        assert AccessTier.T6_UNAVAILABLE.confidence_ceiling is ConfidenceTier.INSUFFICIENT


# =====================================================================================
# Status semantics
# =====================================================================================


class TestStatusSemantics:
    def test_source_unavailable_is_never_adverse(self):
        """The single easiest mistake to make in this layer."""
        assert not VerificationStatus.SOURCE_UNAVAILABLE.is_adverse
        assert not VerificationStatus.SOURCE_UNAVAILABLE.is_answered

    def test_not_applicable_and_pending_are_not_adverse(self):
        assert not VerificationStatus.NOT_APPLICABLE.is_adverse
        assert not VerificationStatus.PENDING_MANUAL.is_adverse

    def test_only_mismatch_and_not_found_are_adverse(self):
        assert VerificationStatus.MISMATCH.is_adverse
        assert VerificationStatus.NOT_FOUND_IN_SOURCE.is_adverse
        assert not VerificationStatus.PARTIAL_MATCH.is_adverse

    def test_unavailable_does_not_count_as_a_check(self):
        from dmocr.verify import unavailable_result
        r = unavailable_result("S", "A", "property.area", AccessTier.T4_PORTAL_MANUAL, "down")
        assert not r.counts_as_a_check
        assert not r.review_required

    def test_not_applicable_without_a_value_is_not_an_answer(self):
        """Out-of-scope is different from 'answered but nothing to compare'."""
        from dmocr.model.verification import VerificationResult

        r = VerificationResult(
            source_id="S", authority="A", attribute="property.area",
            tier=AccessTier.T4_PORTAL_MANUAL,
            status=VerificationStatus.NOT_APPLICABLE,
        )
        assert not r.counts_as_a_check


# =====================================================================================
# Planning
# =====================================================================================


class TestPlanner:
    def test_out_of_state_case_plans_nothing(self):
        plan = VerificationPlanner().plan(make_case(state="KA"))
        assert plan.items == []
        assert any("NOT_APPLICABLE, not a failure" in n for n in plan.notes)

    def test_maharera_is_not_applicable_without_a_registration_number(self):
        """Much of Mumbai's older resale stock has no RERA record. Absence must not
        become a finding while REQ_RERA_3_2 remains REQUIRES_LEGAL_REVIEW."""
        plan = VerificationPlanner().plan(make_case())
        item = next(i for i in plan.items if i.source_id == "SRC_MAHARERA")
        assert item.execution is Execution.SKIP
        assert "NOT_APPLICABLE" in item.reason

    def test_maharera_is_in_scope_when_a_registration_number_exists(self):
        case = make_case()
        claim(case.properties[0], "project.rera_registration_number",
              TextValue(raw="P51900012345", normalised="P51900012345"))
        item = next(i for i in VerificationPlanner().plan(case).items
                    if i.source_id == "SRC_MAHARERA")
        assert item.execution is not Execution.SKIP

    def test_property_card_needs_a_cts_number(self):
        case = make_case(with_property=True)
        bare = Case(tenant_id="T1", lender_type=LenderType.HFC,
                    product=Product.HOUSING_LOAN)
        bare.properties.append(Property())
        item = next(i for i in VerificationPlanner().plan(bare).items
                    if i.source_id == "SRC_PROPERTY_CARD_MH")
        assert item.execution is Execution.SKIP
        assert "CTS" in item.reason

    def test_only_one_lookup_key_is_sent(self):
        """Data minimisation: the narrowest identifier that resolves a record."""
        plan = VerificationPlanner().plan(make_case())
        for item in plan.items:
            assert len(item.lookup_keys) <= 1

    def test_lookup_key_is_human_usable(self):
        """An operator cannot type a Python tuple into a portal."""
        plan = VerificationPlanner().plan(make_case())
        item = next(i for i in plan.items if i.source_id == "SRC_PROPERTY_CARD_MH")
        assert item.lookup_keys == {"cts_number": "1234/5A"}

    def test_disputed_key_is_not_used_for_lookup(self):
        """Querying on a disputed identifier retrieves the wrong record."""
        case = make_case()
        prop = case.properties[0]
        claim(prop, "property.parcel_identifier", ParcelValue(
            identifier=ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="9999")),
            doc_id="D2")
        item = next(i for i in VerificationPlanner().plan(case).items
                    if i.source_id == "SRC_PROPERTY_CARD_MH")
        assert "cts_number" in item.ambiguous_keys
        assert "cts_number" not in item.lookup_keys

    def test_blocked_source_is_routed_to_a_human(self):
        plan = VerificationPlanner().plan(make_case())
        igr = next(i for i in plan.items if i.source_id == "SRC_IGR_ESEARCH")
        # No document number extracted here, so it skips; the reason still names the key.
        assert igr.execution in (Execution.MANUAL, Execution.SKIP)

    def test_plan_summary_counts(self):
        plan = VerificationPlanner().plan(make_case())
        s = plan.summary()
        assert s["sources_considered"] == len(plan.items)
        assert s["automated"] + s["manual"] + s["skipped"] == s["sources_considered"]


# =====================================================================================
# Comparison
# =====================================================================================


class TestCompareValues:
    def test_matching_areas_within_tolerance(self):
        a = AreaValue(area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")
        b = AreaValue(area=Area.of(1145, AreaUnit.SQ_FT), basis="carpet")
        status, _ = compare_values("property.area", a, b)
        assert status is VerificationStatus.MATCH

    def test_different_measurement_bases_are_partial_not_mismatch(self):
        a = AreaValue(area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")
        b = AreaValue(area=Area.of(1450, AreaUnit.SQ_FT), basis="super_built_up")
        status, detail = compare_values("property.area", a, b)
        assert status is VerificationStatus.PARTIAL_MATCH
        assert "not directly comparable" in detail

    def test_rera_phase_containment_is_partial_match(self):
        """A deed naming 'ABC Residency' against RERA's 'ABC Residency Phase II' is the
        expected shape of a phased project, not a contradiction."""
        status, detail = compare_values(
            "project.name",
            TextValue(raw="ABC Residency"),
            TextValue(raw="ABC Residency Phase II"),
        )
        assert status is VerificationStatus.PARTIAL_MATCH
        assert "phased development" in detail

    def test_names_use_scored_matching(self):
        status, _ = compare_values(
            "party.owner", TextValue(raw="Shri Ramesh Patil"), TextValue(raw="R. Patil"))
        assert status is VerificationStatus.MATCH

    def test_different_names_mismatch(self):
        status, _ = compare_values(
            "party.owner", TextValue(raw="Ramesh Patil"), TextValue(raw="Suresh Kulkarni"))
        assert status is VerificationStatus.MISMATCH

    def test_type_mismatch_is_reported(self):
        status, _ = compare_values(
            "x", TextValue(raw="1150"),
            AreaValue(area=Area.of(1150, AreaUnit.SQ_FT)))
        assert status is VerificationStatus.MISMATCH

    def test_parcel_identifiers_compare_by_typed_key(self):
        cts = ParcelValue(identifier=ParcelIdentifier(
            id_type=ParcelIdentifierType.CTS, value="145"))
        survey = ParcelValue(identifier=ParcelIdentifier(
            id_type=ParcelIdentifierType.SURVEY, value="145"))
        status, _ = compare_values("property.parcel_identifier", cts, survey)
        assert status is VerificationStatus.MISMATCH


class TestCompareObservation:
    def _prop(self) -> Property:
        return make_case().properties[0]

    def test_match_produces_a_result_with_both_values(self):
        prop = self._prop()
        obs = observation(fields={"property.area": AreaValue(
            area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")})
        from dmocr.verify import compare_observation
        r = compare_observation("property.area", prop.resolve("property.area"), obs,
                                tier=AccessTier.T5_OFFLINE)
        assert r.status is VerificationStatus.MATCH
        assert r.internal_value and r.external_value
        assert r.snapshot_id == obs.snapshot.snapshot_id

    def test_record_not_found_is_a_signal_not_an_error(self):
        from dmocr.verify import compare_observation
        prop = self._prop()
        r = compare_observation("property.area", prop.resolve("property.area"),
                                observation(found=False), tier=AccessTier.T4_PORTAL_MANUAL)
        assert r.status is VerificationStatus.NOT_FOUND_IN_SOURCE
        assert r.review_required

    def test_nothing_internal_to_compare_yields_not_applicable(self):
        from dmocr.verify import compare_observation
        prop = Property()
        r = compare_observation("property.area", prop.resolve("property.area"),
                                observation(fields={"property.area": AreaValue(
                                    area=Area.of(1000, AreaUnit.SQ_FT))}),
                                tier=AccessTier.T4_PORTAL_MANUAL)
        assert r.status is VerificationStatus.NOT_APPLICABLE
        # The external value is still recorded, and the authority DID answer. Treating
        # this as "no answer" made a real CERSAI charge invisible - see
        # test_charge_recorded_as_not_applicable_is_still_present.
        assert r.external_value is not None
        assert r.counts_as_a_check

    def test_stale_record_is_flagged(self):
        from dmocr.verify import compare_observation
        prop = self._prop()
        obs = observation(
            fields={"property.area": AreaValue(area=Area.of(1150, AreaUnit.SQ_FT),
                                               basis="carpet")},
            as_of=datetime.now() - timedelta(days=400))
        r = compare_observation("property.area", prop.resolve("property.area"), obs,
                                tier=AccessTier.T5_OFFLINE, freshness=timedelta(days=90))
        assert r.status is VerificationStatus.STALE

    def test_disputed_internal_value_downgrades_a_match(self):
        """Agreeing with the majority reading does not resolve our own disagreement."""
        from dmocr.verify import compare_observation
        prop = self._prop()
        claim(prop, "property.area",
              AreaValue(area=Area.of(980, AreaUnit.SQ_FT), basis="carpet"), doc_id="D2")
        obs = observation(fields={"property.area": AreaValue(
            area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")})
        r = compare_observation("property.area", prop.resolve("property.area"), obs,
                                tier=AccessTier.T5_OFFLINE)
        assert r.status is VerificationStatus.PARTIAL_MATCH
        assert "itself disputed" in r.detail

    def test_tier_caps_the_confidence_of_a_match(self):
        from dmocr.verify import compare_observation
        prop = self._prop()
        fields = {"property.area": AreaValue(area=Area.of(1150, AreaUnit.SQ_FT),
                                             basis="carpet")}
        high = compare_observation("property.area", prop.resolve("property.area"),
                                   observation(fields=fields),
                                   tier=AccessTier.T1_OFFICIAL_API)
        low = compare_observation("property.area", prop.resolve("property.area"),
                                  observation(fields=fields),
                                  tier=AccessTier.T4_PORTAL_MANUAL)
        assert high.confidence is ConfidenceTier.HIGH
        assert low.confidence is ConfidenceTier.MEDIUM


# =====================================================================================
# Orchestration
# =====================================================================================


class TestOrchestrator:
    def test_missing_adapter_yields_source_unavailable_not_a_failure(self):
        run = VerificationOrchestrator().run(make_case())
        unavailable = run.by_status(VerificationStatus.SOURCE_UNAVAILABLE)
        assert unavailable
        assert all(not r.status.is_adverse for r in unavailable)
        assert run.adverse == []

    def test_out_of_scope_source_is_reported_not_dropped(self):
        run = VerificationOrchestrator().run(make_case())
        na = [r for r in run.results if r.status is VerificationStatus.NOT_APPLICABLE]
        assert any(r.source_id == "SRC_MAHARERA" for r in na)

    def test_manual_sources_create_operator_tasks(self):
        queue = TaskQueue()
        run = VerificationOrchestrator(task_queue=queue).run(make_case())
        assert run.tasks
        assert queue.open_tasks()
        assert all(t.lookup_keys for t in run.tasks)

    def test_task_instruction_forbids_widening_the_query(self):
        run = VerificationOrchestrator().run(make_case())
        text = run.tasks[0].render_instruction()
        assert "do not widen" in text

    def test_automated_adapter_is_called_with_only_the_planned_keys(self):
        case = make_case()
        adapter = StaticAdapter("SRC_CERSAI", "CERSAI", AccessTier.T2_LICENSED,
                                {"property.encumbrance": TextValue(raw="No charge")})
        VerificationOrchestrator(AdapterRegistry([adapter])).run(case)
        assert adapter.calls
        assert len(adapter.calls[0]) <= 1

    def test_unavailable_adapter_does_not_stop_the_run(self):
        adapter = StaticAdapter("SRC_CERSAI", "CERSAI", AccessTier.T2_LICENSED,
                                unavailable="portal down")
        run = VerificationOrchestrator(AdapterRegistry([adapter])).run(make_case())
        assert any(r.status is VerificationStatus.SOURCE_UNAVAILABLE for r in run.results)
        assert run.tasks     # manual sources still queued

    def test_a_raising_adapter_is_contained(self):
        class Boom(StaticAdapter):
            def fetch(self, keys):
                raise RuntimeError("kaboom")

        adapter = Boom("SRC_CERSAI", "CERSAI", AccessTier.T2_LICENSED)
        run = VerificationOrchestrator(AdapterRegistry([adapter])).run(make_case())
        cersai = [r for r in run.results if r.source_id == "SRC_CERSAI"]
        assert cersai and all(
            r.status is VerificationStatus.SOURCE_UNAVAILABLE for r in cersai)

    def test_snapshot_records_what_was_sent(self):
        """An external lookup is an outbound disclosure; the audit needs the request."""
        adapter = StaticAdapter("SRC_CERSAI", "CERSAI", AccessTier.T2_LICENSED,
                                {"property.encumbrance": TextValue(raw="No charge")})
        run = VerificationOrchestrator(AdapterRegistry([adapter])).run(make_case())
        assert run.snapshots
        assert run.snapshots[0].request_keys

    def test_case_without_a_property_does_not_crash(self):
        run = VerificationOrchestrator().run(make_case(with_property=False))
        assert run.results == []
        assert any("No property" in n for n in run.notes)

    def test_summary_separates_checks_from_open_items(self):
        run = VerificationOrchestrator().run(make_case())
        s = run.summary()
        assert s["checks_performed"] == 0        # nothing was actually retrieved
        assert s["pending_manual"] > 0
        assert s["open_tasks"] > 0


class TestManualResultsReenter:
    def test_completed_task_runs_the_same_comparison(self):
        case = make_case()
        queue = TaskQueue()
        orch = VerificationOrchestrator(task_queue=queue)
        run = orch.run(case)
        task = next(t for t in run.tasks if t.source_id == "SRC_PROPERTY_CARD_MH")

        results = orch.ingest_manual_observation(
            case, task.task_id,
            observation(fields={"property.area": AreaValue(
                area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")}),
            operator_id="op@example.com",
        )
        areas = [r for r in results if r.attribute == "property.area"]
        assert areas and areas[0].status is VerificationStatus.MATCH
        assert queue.get(task.task_id).status is TaskStatus.COMPLETED
        assert queue.get(task.task_id).operator_id == "op@example.com"

    def test_manual_result_confidence_is_capped_by_tier(self):
        case = make_case()
        orch = VerificationOrchestrator()
        run = orch.run(case)
        task = next(t for t in run.tasks if t.source_id == "SRC_PROPERTY_CARD_MH")
        results = orch.ingest_manual_observation(
            case, task.task_id,
            observation(fields={"property.area": AreaValue(
                area=Area.of(1150, AreaUnit.SQ_FT), basis="carpet")}),
            operator_id="op",
        )
        assert all(r.confidence is not ConfidenceTier.HIGH for r in results)

    def test_unobtainable_is_distinct_from_completed(self):
        """The operator tried and could not retrieve it: not a check, not a failure."""
        queue = TaskQueue()
        run = VerificationOrchestrator(task_queue=queue).run(make_case())
        task = run.tasks[0]
        queue.mark_unobtainable(task.task_id, operator_id="op", reason="portal down")
        assert queue.get(task.task_id).status is TaskStatus.UNOBTAINABLE
        assert not queue.get(task.task_id).is_open

    def test_unknown_task_raises(self):
        with pytest.raises(KeyError):
            VerificationOrchestrator().ingest_manual_observation(
                make_case(), "TASK_nope", observation(), operator_id="op")


class TestRendering:
    def test_render_lists_tasks_and_summary(self):
        text = render_verification(VerificationOrchestrator().run(make_case()))
        assert "EXTERNAL VERIFICATION" in text
        assert "OPERATOR TASKS" in text
        assert "PENDING_MANUAL" in text
