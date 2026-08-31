"""Tests for the canonical model.

These target the decisions that would produce WRONG FINDINGS if they regressed, not
coverage for its own sake. Each test names the requirement or ADR it protects.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from dmocr.model import (
    Area,
    AreaUnit,
    AreaValue,
    Case,
    Claim,
    ClaimSet,
    ConfidenceTier,
    CustodyStatus,
    Determination,
    Document,
    DocumentProvenance,
    DocumentType,
    HumanProvenance,
    InstrumentStrength,
    LenderType,
    Money,
    MoneyValue,
    ParcelIdentifier,
    ParcelIdentifierType,
    ParcelValue,
    Product,
    Property,
    SecurityType,
    TextValue,
    instrument_strength_of,
)
from dmocr.model.provenance import BoundingBox, TextSpan


def doc_prov(doc_id: str = "DOC1", page: int = 1) -> DocumentProvenance:
    return DocumentProvenance(document_id=doc_id, page=page, source_text="...")


def claim(attr: str, value, *, doc_id="DOC1", strength=None, subject="PRP1") -> Claim:
    return Claim(
        subject_id=subject,
        attribute=attr,
        value=value,
        provenance=doc_prov(doc_id),
        instrument_strength=strength,
    )


# =====================================================================================
# Money - exactness
# =====================================================================================


class TestMoney:
    def test_rupees_round_trip_exactly(self):
        m = Money.from_rupees("12500000.50")
        assert m.paise == 1250000050
        assert m.rupees == Decimal("12500000.50")

    def test_no_float_drift_on_repeated_addition(self):
        """0.1 is not representable in binary float; paise arithmetic must be exact."""
        total = sum(Money.from_rupees("0.10").paise for _ in range(10))
        assert total == Money.from_rupees("1.00").paise

    def test_negative_rejected(self):
        with pytest.raises(Exception):
            Money(paise=-1)

    @pytest.mark.parametrize("rupees,expected", [
        ("12500000", "1,25,00,000.00"),      # one crore twenty-five lakh
        ("990000", "9,90,000.00"),
        ("100000", "1,00,000.00"),
        ("1000", "1,000.00"),
        ("999", "999.00"),
        ("0", "0.00"),
        ("1234567890", "1,23,45,67,890.00"),
    ])
    def test_indian_digit_grouping_on_display(self, rupees, expected):
        """Two-two-three, not Western thousands. We parse Indian grouping, so showing it
        back in the other convention would be inconsistent as well as jarring."""
        assert str(Money.from_rupees(rupees)) == f"INR {expected}"


# =====================================================================================
# Area - conversion, tolerance, and measurement basis
# =====================================================================================


class TestArea:
    def test_sq_ft_to_sq_m_is_exact(self):
        a = Area.of(2400, AreaUnit.SQ_FT)
        # 2400 * 0.09290304 = 222.9672960
        assert a.sq_m == Decimal("222.967296")

    def test_guntha_is_one_fortieth_of_an_acre(self):
        assert (Area.of(40, AreaUnit.GUNTHA).sq_m
                == Area.of(1, AreaUnit.ACRE).sq_m)

    def test_tolerance_absorbs_rounding_between_documents(self):
        """Deeds round. 2400 vs 2390 sq ft is 0.4% and must not be a mismatch."""
        assert Area.of(2400, AreaUnit.SQ_FT).matches(Area.of(2390, AreaUnit.SQ_FT))

    def test_tolerance_does_not_absorb_a_real_discrepancy(self):
        """2400 vs 2210 sq ft is ~8% - a genuine finding, not rounding."""
        assert not Area.of(2400, AreaUnit.SQ_FT).matches(Area.of(2210, AreaUnit.SQ_FT))

    def test_cross_unit_comparison_works(self):
        assert Area.of(2400, AreaUnit.SQ_FT).matches(Area.of("222.97", AreaUnit.SQ_M))

    def test_different_measurement_bases_never_agree(self):
        """Carpet vs super built-up differ legitimately; comparing them is meaningless."""
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        cs = cs.with_claim(claim(
            "property.area",
            AreaValue(area=Area.of(1000, AreaUnit.SQ_FT), basis="carpet"),
            doc_id="DOC1",
        ))
        cs = cs.with_claim(claim(
            "property.area",
            AreaValue(area=Area.of(1000, AreaUnit.SQ_FT), basis="super_built_up"),
            doc_id="DOC2",
        ))
        # Identical numbers, incompatible bases -> not a MATCH.
        assert cs.resolve().determination is Determination.MISMATCH


# =====================================================================================
# Parcel identifiers - typed, not stringly
# =====================================================================================


class TestParcelIdentifier:
    def test_same_digits_different_type_are_not_the_same_parcel(self):
        """CTS 145 and Survey 145 are unrelated parcels that share digits."""
        cts = ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="145")
        svy = ParcelIdentifier(id_type=ParcelIdentifierType.SURVEY, value="145")
        assert cts.comparable_key() != svy.comparable_key()

    def test_whitespace_and_case_are_normalised(self):
        a = ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value=" 1234 / 5a ")
        b = ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="1234/5A")
        assert a.comparable_key() == b.comparable_key()

    def test_locality_disambiguates(self):
        """Identical CTS numbers in different villages are different parcels."""
        a = ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="145", locality="Andheri")
        b = ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="145", locality="Bandra")
        assert a.comparable_key() != b.comparable_key()

    def test_property_shares_identifier(self):
        p1, p2 = Property(), Property()
        p1.add_parcel_identifier(
            ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="145"))
        p2.add_parcel_identifier(
            ParcelIdentifier(id_type=ParcelIdentifierType.CTS, value="145"))
        p2.add_parcel_identifier(
            ParcelIdentifier(id_type=ParcelIdentifierType.PLOT, value="7"))
        assert p1.shares_identifier_with(p2)


# =====================================================================================
# Instrument strength - REQ_TPA_54_CONTRACT_CREATES_NO_INTEREST
# =====================================================================================


class TestInstrumentStrength:
    def test_sale_deed_transfers_title(self):
        assert (instrument_strength_of(DocumentType.SALE_DEED)
                is InstrumentStrength.TITLE_TRANSFERRING)

    def test_agreement_of_sale_is_only_contractual(self):
        """TPA s.54: a contract for sale creates no interest in the property."""
        assert (instrument_strength_of(DocumentType.AGREEMENT_OF_SALE)
                is InstrumentStrength.CONTRACTUAL)

    def test_unmapped_type_defaults_to_non_probative(self):
        """Absent mapping must not be assumed probative."""
        assert (instrument_strength_of(DocumentType.NOC)
                is InstrumentStrength.NON_PROBATIVE)

    def test_agreement_of_sale_cannot_establish_ownership(self):
        c = claim("party.owner", TextValue(raw="A Kumar"),
                  strength=InstrumentStrength.CONTRACTUAL)
        assert not c.can_establish_ownership()

    def test_sale_deed_can_establish_ownership(self):
        c = claim("party.owner", TextValue(raw="A Kumar"),
                  strength=InstrumentStrength.TITLE_TRANSFERRING)
        assert c.can_establish_ownership()

    def test_ownership_resolution_ignores_contractual_claims(self):
        """A case holding only an Agreement of Sale must not appear to establish title."""
        cs = ClaimSet(subject_id="PRP1", attribute="party.owner")
        cs = cs.with_claim(claim("party.owner", TextValue(raw="A Kumar"),
                                 strength=InstrumentStrength.CONTRACTUAL))
        res = cs.resolve(ownership_only=True)
        assert res.determination is Determination.MISSING
        assert "ownership" in res.rationale.lower()


# =====================================================================================
# Claim resolution - conflicts preserved (ADR-0003)
# =====================================================================================


class TestResolution:
    def test_no_claims_is_missing(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        assert cs.resolve().determination is Determination.MISSING

    def test_single_source_is_not_determinable_not_match(self):
        """A lone assertion is not agreement. This must never read as corroborated."""
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        cs = cs.with_claim(claim("property.area",
                                 AreaValue(area=Area.of(2400, AreaUnit.SQ_FT))))
        res = cs.resolve()
        assert res.determination is Determination.NOT_DETERMINABLE
        assert res.confidence is ConfidenceTier.LOW
        assert res.value is not None  # a value is still offered to the reviewer

    def test_agreeing_sources_match(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        for d in ("DOC1", "DOC2", "DOC3"):
            cs = cs.with_claim(claim("property.area",
                                     AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)),
                                     doc_id=d))
        res = cs.resolve()
        assert res.determination is Determination.MATCH
        assert res.confidence is ConfidenceTier.HIGH
        assert len(res.supporting_claim_ids) == 3

    def test_conflicting_sources_preserve_the_minority(self):
        """The dissenting claim is the finding. It must not be discarded."""
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        cs = cs.with_claim(claim("property.area",
                                 AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)),
                                 doc_id="DOC1"))
        cs = cs.with_claim(claim("property.area",
                                 AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)),
                                 doc_id="DOC2"))
        cs = cs.with_claim(claim("property.area",
                                 AreaValue(area=Area.of(2210, AreaUnit.SQ_FT)),
                                 doc_id="DOC3"))
        res = cs.resolve()
        assert res.determination is Determination.MISMATCH
        assert len(res.supporting_claim_ids) == 2
        assert len(res.conflicting_claim_ids) == 1
        assert res.confidence is ConfidenceTier.INSUFFICIENT

    def test_resolution_does_not_mutate_the_claim_set(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        cs = cs.with_claim(claim("property.area",
                                 AreaValue(area=Area.of(2400, AreaUnit.SQ_FT))))
        before = len(cs.claims)
        cs.resolve()
        assert len(cs.claims) == before

    def test_different_value_kinds_never_agree(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.x")
        cs = cs.with_claim(claim("property.x", TextValue(raw="2400")))
        cs = cs.with_claim(claim("property.x",
                                 MoneyValue(amount=Money.from_rupees(2400)),
                                 doc_id="DOC2"))
        assert cs.resolve().determination is Determination.MISMATCH

    def test_human_correction_supersedes_without_deleting(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        original = claim("property.area", AreaValue(area=Area.of(2210, AreaUnit.SQ_FT)))
        cs = cs.with_claim(original)
        correction = Claim(
            subject_id="PRP1",
            attribute="property.area",
            value=AreaValue(area=Area.of(2400, AreaUnit.SQ_FT)),
            provenance=HumanProvenance(
                actor="reviewer@example.com",
                asserted_at=datetime.now(),
                rationale="OCR misread",
                supersedes=original.claim_id,
            ),
        )
        cs = cs.with_claim(correction)
        assert len(cs.claims) == 2                 # nothing deleted - audit intact
        assert len(cs.active_claims()) == 1
        assert cs.active_claims()[0].claim_id == correction.claim_id

    def test_claim_must_belong_to_the_set(self):
        cs = ClaimSet(subject_id="PRP1", attribute="property.area")
        with pytest.raises(ValueError):
            cs.with_claim(claim("property.address", TextValue(raw="x")))


# =====================================================================================
# Security type - REQ_TPA_59, the Mumbai carve-out
# =====================================================================================


class TestSecurityType:
    def test_equitable_mortgage_is_exempt_from_registration(self):
        """TPA s.59 expressly excepts mortgage by deposit of title-deeds."""
        assert not SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS.requires_registered_instrument

    def test_simple_mortgage_requires_registration(self):
        assert SecurityType.SIMPLE.requires_registered_instrument

    def test_unknown_does_not_assert_a_defect(self):
        assert not SecurityType.UNKNOWN.requires_registered_instrument

    def test_case_returns_none_when_security_type_unknown(self):
        """NOT_DETERMINABLE, not False. We must not imply we checked."""
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        assert c.mortgage_requires_registration() is None

    def test_case_mumbai_equitable_mortgage_needs_no_registration(self):
        c = Case(
            tenant_id="T1",
            lender_type=LenderType.HFC,
            product=Product.HOUSING_LOAN,
            security_type=SecurityType.EQUITABLE_DEPOSIT_OF_TITLE_DEEDS,
        )
        assert c.mortgage_requires_registration() is False


# =====================================================================================
# Case and custody
# =====================================================================================


class TestCase:
    def _doc(self, case: Case, dtype: DocumentType, **kw) -> Document:
        return Document(case_id=case.case_id, tenant_id=case.tenant_id,
                        document_type=dtype, sha256="a" * 64, **kw)

    def test_rejects_document_from_another_case(self):
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        stray = Document(case_id="CASE_other", tenant_id="T1",
                         document_type=DocumentType.SALE_DEED, sha256="b" * 64)
        with pytest.raises(ValueError):
            c.add_document(stray)

    def test_rejects_document_from_another_tenant(self):
        """Tenant scoping is enforced in the model even though MVP has no auth."""
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        stray = Document(case_id=c.case_id, tenant_id="T2",
                         document_type=DocumentType.SALE_DEED, sha256="b" * 64)
        with pytest.raises(ValueError):
            c.add_document(stray)

    def test_missing_document_types(self):
        c = Case(
            tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN,
            expected_documents=[DocumentType.SALE_DEED, DocumentType.PROPERTY_TAX],
        )
        c.add_document(self._doc(c, DocumentType.SALE_DEED))
        assert c.missing_document_types() == [DocumentType.PROPERTY_TAX]

    def test_custody_inventory_groups_by_status(self):
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        c.add_document(self._doc(c, DocumentType.SALE_DEED,
                                 custody=CustodyStatus.ORIGINAL_HELD))
        c.add_document(self._doc(c, DocumentType.PROPERTY_TAX,
                                 custody=CustodyStatus.PHOTOCOPY))
        inv = c.custody_inventory()
        assert len(inv["original_held"]) == 1
        assert len(inv["photocopy"]) == 1
        assert len(c.originals_held()) == 1

    def test_degraded_documents_cap_confidence(self):
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        from dmocr.model import DocumentQuality
        d = self._doc(c, DocumentType.SALE_DEED, quality=DocumentQuality.DEGRADED)
        assert d.confidence_capped

    def test_rejected_documents_excluded_from_usable(self):
        c = Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)
        from dmocr.model import DocumentQuality
        c.add_document(self._doc(c, DocumentType.SALE_DEED))
        c.add_document(self._doc(c, DocumentType.PROPERTY_TAX,
                                 quality=DocumentQuality.REJECTED))
        assert len(c.usable_documents()) == 1


# =====================================================================================
# Provenance
# =====================================================================================


class TestProvenance:
    def test_degenerate_bbox_rejected(self):
        with pytest.raises(Exception):
            BoundingBox(x0=100, y0=100, x1=50, y1=200)

    def test_span_end_before_start_rejected(self):
        with pytest.raises(Exception):
            TextSpan(start=100, end=50)

    def test_text_layer_claims_have_no_ocr_confidence(self):
        p = DocumentProvenance(document_id="DOC1", page=4)
        assert p.from_text_layer

    def test_ocr_claims_record_confidence(self):
        p = DocumentProvenance(document_id="DOC1", page=4, ocr_confidence=0.91)
        assert not p.from_text_layer

    def test_derived_provenance_requires_inputs(self):
        """A derived value that cannot name its inputs is not traceable."""
        from dmocr.model import DerivedProvenance
        with pytest.raises(Exception):
            DerivedProvenance(input_claim_ids=[], method="ltv", method_version="1")


# =====================================================================================
# Determination semantics
# =====================================================================================


class TestDetermination:
    @pytest.mark.parametrize("d,adverse", [
        (Determination.MATCH, False),
        (Determination.PARTIAL_MATCH, False),
        (Determination.MISMATCH, True),
        (Determination.MISSING, True),
        (Determination.NOT_APPLICABLE, False),
        (Determination.NOT_DETERMINABLE, False),
    ])
    def test_only_mismatch_and_missing_count_against_a_case(self, d, adverse):
        """NOT_DETERMINABLE and NOT_APPLICABLE are not failures."""
        assert d.is_adverse is adverse
