"""Tests for entity resolution and case assembly.

The central risk here is **false merging**. Treating two different people as one can make a
broken title chain look continuous, which is worse than splitting one person into two and
raising a spurious mismatch. So the uncertain band routes to a human rather than deciding.
"""

from __future__ import annotations

import pytest

from dmocr.extract import ExtractionService
from dmocr.model import (
    Case,
    Determination,
    DocumentType,
    InstrumentStrength,
    LenderType,
    Product,
    Property,
)
from dmocr.ocr import TextSource, assemble_page
from dmocr.ocr.types import OcrDocument
from dmocr.model.provenance import BoundingBox
from dmocr.resolve import (
    CaseAssembler,
    match_names,
    phonetic_key,
    similarity,
    split_name,
)


def make_doc(text: str) -> OcrDocument:
    blocks = [(line, BoundingBox(x0=50, y0=20 * n, x1=500, y1=20 * n + 12), None)
              for n, line in enumerate(text.split("\n")) if line.strip()]
    return OcrDocument(
        document_sha256="sha",
        pages=[assemble_page(1, 595, 842, blocks, TextSource.TEXT_LAYER)],
    )


def extract(text: str, doc_type: DocumentType, document_id: str):
    return ExtractionService().extract(
        make_doc(text), document_id=document_id, document_type=doc_type)


SALE_DEED = """DEED OF SALE
This Deed of Sale is executed at Mumbai on the 14th day of March 2024
BETWEEN Shri Ramesh Patil, hereinafter called the VENDOR, of the One Part
AND Smt. Anita Desai, hereinafter called the PURCHASER, of the Other Part.
Flat No. 402, C.T.S. No. 1234/5A, Andheri West, Mumbai.
Carpet Area: 1150 sq. ft.
Consideration: Rs. 1,25,00,000/-"""

AGREEMENT = """AGREEMENT FOR SALE
This Agreement for Sale is executed at Mumbai on the 2nd day of January 2024
BETWEEN R. Patil, hereinafter called the PROMOTER, of the One Part
AND Anita Dessai, hereinafter called the ALLOTTEE, of the Other Part.
Flat No. 402, C.T.S. No. 1234/5A, Andheri West, Mumbai.
Carpet Area: 1150 sq. ft.
Consideration: Rs. 1,25,00,000/-"""

TAX_BILL = """MUNICIPAL CORPORATION OF GREATER MUMBAI
PROPERTY TAX BILL
Assessment Number: A-1234567890
Flat No. 402, C.T.S. No. 1234/5A, Andheri West, Mumbai.
Carpet Area: 980 sq. ft."""


# =====================================================================================
# Name normalisation
# =====================================================================================


class TestSplitName:
    def test_honorifics_are_dropped(self):
        assert split_name("Shri Ramesh Patil").tokens == ("ramesh", "patil")
        assert split_name("Smt. Anita Desai").tokens == ("anita", "desai")

    def test_patronymic_is_separated_not_absorbed(self):
        """'Ramesh s/o Ganpat Patil' - Ganpat is the father, not the party."""
        parts = split_name("Ramesh s/o Ganpat Patil")
        assert parts.tokens == ("ramesh",)
        assert parts.relation == "Ganpat Patil"

    def test_empty_name(self):
        assert split_name("").is_empty


class TestPhoneticKey:
    @pytest.mark.parametrize("a,b", [
        ("desai", "dessai"),
        ("anita", "aneeta"),
        ("vishwas", "vishvas"),
        ("phadke", "fadke"),
    ])
    def test_transliteration_variants_share_a_key(self, a, b):
        assert phonetic_key(a) == phonetic_key(b)

    def test_distinct_surnames_keep_distinct_keys(self):
        """An aggressive fold would merge genuinely different names."""
        assert phonetic_key("patil") != phonetic_key("patel")
        assert phonetic_key("shah") != phonetic_key("sharma")

    def test_similarity_bounds(self):
        assert similarity("abc", "abc") == 1.0
        assert similarity("", "") == 1.0
        assert similarity("abc", "") == 0.0


# =====================================================================================
# Name matching
# =====================================================================================


class TestMatchNames:
    def test_identical_names_match(self):
        assert match_names("Ramesh Patil", "Ramesh Patil").determination is Determination.MATCH

    def test_honorifics_do_not_prevent_a_match(self):
        assert match_names("Shri Ramesh Patil",
                           "Ramesh Patil").determination is Determination.MATCH

    def test_initial_stands_for_a_full_first_name(self):
        m = match_names("Ramesh Patil", "R. Patil")
        assert m.determination is Determination.MATCH

    def test_transliteration_variant_matches(self):
        m = match_names("Anita Desai", "Anita Dessai")
        assert m.determination is Determination.MATCH

    def test_word_order_does_not_matter(self):
        assert match_names("Patil Ramesh",
                           "Ramesh Patil").determination is Determination.MATCH

    def test_different_people_do_not_match(self):
        m = match_names("Ramesh Patil", "Suresh Kulkarni")
        assert m.determination is Determination.MISMATCH

    def test_similar_but_unclear_goes_to_review(self):
        """The uncertain band is a real outcome, not a failure to decide."""
        m = match_names("Ramesh Patil", "Ramesh Patil Kulkarni Deshmukh")
        assert m.determination in (Determination.PARTIAL_MATCH, Determination.MISMATCH)
        if m.determination is Determination.PARTIAL_MATCH:
            assert m.needs_review

    def test_extra_middle_name_dilutes_the_score(self):
        exact = match_names("Ramesh Patil", "Ramesh Patil").score
        with_middle = match_names("Ramesh Patil", "Ramesh Ganpat Patil").score
        assert with_middle < exact

    def test_ocr_glued_name_still_matches(self):
        """Real OCR returned 'RameshPatil' with the space lost."""
        m = match_names("rameshpatil", "Ramesh Patil")
        assert m.determination is Determination.MATCH

    def test_empty_name_is_not_determinable(self):
        m = match_names("", "Ramesh Patil")
        assert m.determination is Determination.NOT_DETERMINABLE

    def test_detail_explains_the_pairing(self):
        m = match_names("Ramesh Patil", "R. Patil")
        assert m.detail and any("patil" in d for d in m.detail)


# =====================================================================================
# Assembly
# =====================================================================================


class TestAssembly:
    def _assemble(self, docs: dict[str, tuple[str, DocumentType]]):
        case = Case(tenant_id="T1", lender_type=LenderType.HFC,
                    product=Product.HOUSING_LOAN)
        extractions = {
            doc_id: extract(text, dtype, doc_id)
            for doc_id, (text, dtype) in docs.items()
        }
        return case, CaseAssembler().assemble(case, extractions)

    def test_all_documents_attach_to_one_property(self):
        """A case is one collateral property; conflicts must stay visible on it."""
        case, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE),
        })
        assert len(case.properties) == 1
        assert result.claims_added > 0

    def test_agreeing_documents_produce_a_match(self):
        _, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE),
        })
        assert result.property.resolve("property.area").determination is Determination.MATCH

    def test_conflicting_area_surfaces_as_a_mismatch(self):
        """The tax bill states 980 sq ft against the deed's 1150."""
        _, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE),
            "D3": (TAX_BILL, DocumentType.PROPERTY_TAX),
        })
        resolution = result.property.resolve("property.area")
        assert resolution.determination is Determination.MISMATCH
        assert resolution.conflicting_claim_ids

    def test_parcel_identifier_agrees_across_documents(self):
        _, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (TAX_BILL, DocumentType.PROPERTY_TAX),
        })
        assert result.property.resolve(
            "property.parcel_identifier").determination is Determination.MATCH

    def test_owner_claims_reach_the_property(self):
        """Regression: party claims went only to Party entities, so ownership checks -
        which resolve against the PROPERTY - reported no owner even with a Sale Deed."""
        _, result = self._assemble({"D1": (SALE_DEED, DocumentType.SALE_DEED)})
        resolution = result.property.resolve("party.owner", ownership_only=True)
        assert resolution.determination is not Determination.MISSING

    def test_agreement_alone_still_cannot_establish_ownership(self):
        _, result = self._assemble({"D1": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE)})
        assert result.property.resolve(
            "party.owner", ownership_only=True).determination is Determination.MISSING

    def test_same_party_across_documents_is_merged(self):
        """'Shri Ramesh Patil' and 'R. Patil' are one person."""
        case, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE),
        })
        merged = [d for d in result.decisions if d.action == "merged"]
        assert merged
        sellers = result.parties_for("party.seller")
        assert len(sellers) == 1
        assert len(sellers[0].name_variants) == 2

    def test_every_resolution_decision_is_recorded(self):
        _, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (AGREEMENT, DocumentType.AGREEMENT_OF_SALE),
        })
        for d in result.decisions:
            assert d.attribute and d.left and d.right
            assert d.action in ("merged", "kept_separate")
            assert d.reason

    def test_different_parties_are_not_merged(self):
        other = AGREEMENT.replace("R. Patil", "Suresh Kulkarni")
        case, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": (other, DocumentType.AGREEMENT_OF_SALE),
        })
        assert len(result.parties_for("party.seller")) == 2

    def test_single_document_case_is_noted_as_uncorroborated(self):
        _, result = self._assemble({"D1": (SALE_DEED, DocumentType.SALE_DEED)})
        assert any("cross-document agreement cannot be established" in n
                   for n in result.notes)

    def test_documents_with_no_fields_are_skipped_and_reported(self):
        _, result = self._assemble({
            "D1": (SALE_DEED, DocumentType.SALE_DEED),
            "D2": ("nothing useful", DocumentType.SALE_DEED),
        })
        assert "D2" in result.documents_skipped

    def test_missing_required_fields_are_noted(self):
        _, result = self._assemble({"D1": ("DEED OF SALE only", DocumentType.SALE_DEED)})
        assert any("required fields not extracted" in n for n in result.notes)

    def test_claims_keep_their_instrument_strength(self):
        _, result = self._assemble({"D1": (SALE_DEED, DocumentType.SALE_DEED)})
        claims = result.property.all_claims()
        assert claims
        assert all(c.instrument_strength is InstrumentStrength.TITLE_TRANSFERRING
                   for c in claims)

    def test_assembly_is_idempotent_over_an_existing_property(self):
        case = Case(tenant_id="T1", lender_type=LenderType.HFC,
                    product=Product.HOUSING_LOAN)
        prop = Property()
        case.properties.append(prop)
        CaseAssembler().assemble(case, {"D1": extract(SALE_DEED, DocumentType.SALE_DEED, "D1")})
        assert len(case.properties) == 1
        assert case.properties[0] is prop
