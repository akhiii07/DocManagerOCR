"""Tests for structured extraction.

Three themes:

* **Indian conventions.** Digit grouping, amounts in words, day-first dates. These are
  where quiet correctness bugs live.
* **Span grounding (ADR-0004).** A value that cannot be located on the page must not
  become a claim, and there must be no code path that emits one.
* **Schema shape carries law.** The Agreement of Sale schema has no owner field, because
  TPA s.54 says a contract for sale creates no interest in the property.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from dmocr.extract import (
    ExtractionService,
    GroundingError,
    Select,
    cross_check_money,
    detect_grouping,
    ground,
    is_grounded,
    locate,
    normalise_name,
    parse_area,
    parse_date,
    parse_indian_words,
    parse_money_figures,
    schema_for,
)
from dmocr.extract.extractors import (
    find_area,
    find_consideration,
    find_consideration_amount,
    find_cts_number,
    find_execution_date,
    find_parties,
    find_seller,
)
from dmocr.extract.normalize import GroupingStyle
from dmocr.model import AreaUnit, DocumentType, InstrumentStrength
from dmocr.model.provenance import BoundingBox
from dmocr.ocr import TextSource, assemble_page
from dmocr.ocr.types import OcrDocument


def bb(y: float = 10) -> BoundingBox:
    return BoundingBox(x0=50, y0=y, x1=500, y1=y + 12)


def make_doc(pages: list[str], *, source: TextSource = TextSource.TEXT_LAYER,
             confidence: float | None = None) -> OcrDocument:
    """Build an OcrDocument from plain text, one block per line."""
    out = []
    for i, text in enumerate(pages, 1):
        blocks = [(line, bb(20 * n), confidence)
                  for n, line in enumerate(text.split("\n")) if line.strip()]
        out.append(assemble_page(i, 595, 842, blocks, source,
                                 "fake/1" if source is TextSource.OCR else None))
    return OcrDocument(document_sha256="sha", pages=out)


SALE_DEED_TEXT = """DEED OF SALE
This Deed of Sale is executed at Mumbai on the 14th day of March 2024
BETWEEN Ramesh Patil, hereinafter called the VENDOR, of the One Part
AND Anita Desai, hereinafter called the PURCHASER, of the Other Part.
Flat No. 402, C.T.S. No. 1234/5A, Andheri West, Mumbai.
Carpet Area: 1150 sq. ft.
Consideration: Rs. 1,25,00,000/- (Rupees One Crore Twenty Five Lakh only)
Stamp Duty paid: Rs. 7,50,000/-
Registration No. BDR-4/1234/2024"""


# =====================================================================================
# Indian numbering
# =====================================================================================


class TestMoney:
    @pytest.mark.parametrize("digits,style", [
        ("1,25,00,000", GroupingStyle.INDIAN),
        ("12,34,567", GroupingStyle.INDIAN),
        ("12,500,000", GroupingStyle.WESTERN),
        ("1250000", GroupingStyle.UNGROUPED),
        ("1,2500,00", GroupingStyle.IRREGULAR),
    ])
    def test_grouping_detection(self, digits, style):
        assert detect_grouping(digits) == style

    def test_indian_grouping_parses_to_the_right_number(self):
        """1,25,00,000 is one crore twenty-five lakh, not one million two hundred fifty."""
        parsed = parse_money_figures("Rs. 1,25,00,000/-")
        assert parsed.amount.rupees == Decimal("12500000")

    def test_irregular_grouping_is_flagged_as_suspicious(self):
        """Neither convention usually means OCR mangled the separators."""
        assert parse_money_figures("Rs. 1,2500,00").suspicious_grouping

    @pytest.mark.parametrize("prefix", ["Rs.", "Rs", "INR", "₹", "Rupees"])
    def test_currency_prefixes(self, prefix):
        assert parse_money_figures(f"{prefix} 5,00,000").amount.rupees == Decimal("500000")

    def test_no_amount_returns_none(self):
        assert parse_money_figures("no money here") is None


class TestAmountInWords:
    @pytest.mark.parametrize("words,expected", [
        ("One Crore Twenty Five Lakh only", 12_500_000),
        ("Fifty Lakh", 5_000_000),
        ("Two Crore Fifty Lakh Seventy Five Thousand", 25_075_000),
        ("Five Thousand Five Hundred", 5_500),
        ("Ninety Nine", 99),
        ("Twenty Five Lac", 2_500_000),
    ])
    def test_parses_indian_scale_words(self, words, expected):
        assert parse_indian_words(words) == expected

    def test_handles_common_misspelling(self):
        assert parse_indian_words("Fourty Thousand") == 40_000

    def test_no_words_returns_none(self):
        assert parse_indian_words("1,25,00,000") is None


class TestMoneyCrossCheck:
    def test_agreement_is_detected(self):
        check = cross_check_money("Rs. 1,25,00,000/- (Rupees One Crore Twenty Five Lakh only)")
        assert check.agree is True
        assert not check.is_conflict

    def test_conflict_is_detected(self):
        """A figures-vs-words mismatch is an integrity signal, not a formatting quirk."""
        check = cross_check_money("Rs. 1,25,00,000/- (Rupees Twenty Five Lakh only)")
        assert check.is_conflict

    def test_missing_words_cannot_be_compared(self):
        check = cross_check_money("Rs. 1,25,00,000/-")
        assert check.agree is None
        assert not check.is_conflict     # unknown is not a conflict

    def test_conflict_surfaces_as_an_extraction_note(self):
        matches = find_consideration("Consideration Rs. 1,00,000/- (Rupees Five Lakh only)")
        assert any("does not match" in n for n in matches[0].notes)


# =====================================================================================
# Dates
# =====================================================================================


class TestDates:
    def test_day_first_is_the_default(self):
        assert parse_date("03/04/2024").value == date(2024, 4, 3)

    def test_ambiguity_is_recorded_not_hidden(self):
        parsed = parse_date("03/04/2024")
        assert parsed.ambiguous is True

    def test_unambiguous_date_is_not_flagged(self):
        assert parse_date("25/12/2023").ambiguous is False

    def test_impossible_day_first_falls_back_to_month_first(self):
        """03/14/2024 can only be month-first."""
        parsed = parse_date("03/14/2024")
        assert parsed.value == date(2024, 3, 14)
        assert parsed.order_assumed == "month_first"

    def test_textual_dates(self):
        assert parse_date("the 14th day of March 2024").value == date(2024, 3, 14)

    def test_textual_date_across_a_line_break(self):
        """OCR splits lines; the date must still parse."""
        assert parse_date("14th day of\nMarch 2024").value == date(2024, 3, 14)

    @pytest.mark.parametrize("raw,expected", [
        ("01/01/98", date(1998, 1, 1)),
        ("01/01/24", date(2024, 1, 1)),
    ])
    def test_two_digit_years_pivot(self, raw, expected):
        assert parse_date(raw).value == expected

    def test_invalid_date_returns_none(self):
        assert parse_date("45/45/2024") is None

    def test_execution_date_is_anchored_not_just_the_first_date(self):
        text = ("recited agreement dated 02/01/2024. This deed is executed on "
                "14th day of March 2024")
        matches = find_execution_date(text)
        assert matches
        assert matches[0].value.value == date(2024, 3, 14)


# =====================================================================================
# Areas
# =====================================================================================


class TestAreas:
    @pytest.mark.parametrize("text,unit", [
        ("1150 sq. ft.", AreaUnit.SQ_FT),
        ("106.84 sq. mtrs", AreaUnit.SQ_M),
        ("200 sq yards", AreaUnit.SQ_YARD),
        ("2 acres", AreaUnit.ACRE),
        ("40 gunthas", AreaUnit.GUNTHA),
    ])
    def test_units(self, text, unit):
        assert parse_area(text).area.unit is unit

    def test_basis_is_captured(self):
        assert parse_area("Carpet Area: 1150 sq ft").basis == "carpet"
        assert parse_area("Super Built-up Area 1450 sq ft").basis == "super_built_up"

    def test_unstated_basis_is_flagged(self):
        matches = find_area("Area 1150 sq ft")
        assert any("basis not stated" in n for n in matches[0].notes)

    def test_thousands_separator_in_area(self):
        assert parse_area("1,150 sq ft").area.value == Decimal("1150")


# =====================================================================================
# Identifiers and parties
# =====================================================================================


class TestIdentifiers:
    def test_cts_number_with_punctuation(self):
        matches = find_cts_number("Flat 402, C.T.S. No. 1234/5A, Andheri")
        assert matches
        assert matches[0].value.identifier.value == "1234/5A"

    def test_cts_without_dots(self):
        assert find_cts_number("CTS No 998")[0].value.identifier.value == "998"


class TestParties:
    def test_stock_deed_phrasing(self):
        matches = find_parties(
            "Ramesh Patil, hereinafter called the VENDOR, of the One Part")
        assert matches
        assert "Ramesh Patil" in matches[0].value.raw
        assert "role=vendor" in matches[0].notes

    def test_seller_and_buyer_are_separated(self):
        text = ("Ramesh Patil, hereinafter called the VENDOR, AND "
                "Anita Desai, hereinafter called the PURCHASER")
        assert any("Ramesh" in m.value.raw for m in find_seller(text))

    def test_honorifics_are_stripped(self):
        assert normalise_name("Shri Ramesh Patil") == "Ramesh Patil"
        assert normalise_name("Smt. Anita Desai") == "Anita Desai"

    def test_unconventional_drafting_yields_nothing(self):
        """Weak by design. A missed party surfaces as MISSING - the safe direction."""
        assert find_parties("Ramesh Patil sells to Anita Desai.") == []

    def test_recital_boilerplate_is_trimmed_from_the_name(self):
        """A greedy capture absorbs 'of the One Part AND' before the actual name."""
        matches = find_parties(
            "of the One Part AND Anita Desai, hereinafter called the PURCHASER")
        assert matches[0].value.raw == "Anita Desai"

    def test_case_sensitivity_anchors_the_name_to_a_proper_noun(self):
        """Regression: a global IGNORECASE flag defeated the leading [A-Z] anchor, so
        the pattern started matching at lowercase connective text."""
        matches = find_parties(
            "of the one part and Anita Desai, hereinafter called the PURCHASER")
        assert matches
        assert matches[0].value.raw.startswith("Anita")


class TestOcrRobustness:
    """Regressions from real OCR output, which drops word boundaries.

    A recognised line came back as 'March2024BETWEENRameshPatil,hereinaftercalledthe'.
    Patterns requiring whitespace failed on exactly the documents most in need of
    extraction.
    """

    def test_year_glued_to_the_next_word_still_parses(self):
        """`(\\d{4})\\b` fails on 'March2024BETWEEN' - no boundary between 4 and B."""
        parsed = parse_date("14th day of March2024BETWEEN Ramesh")
        assert parsed is not None
        assert parsed.value == date(2024, 3, 14)

    def test_numeric_date_glued_to_the_next_word(self):
        assert parse_date("dated 14/03/2024BETWEEN").value == date(2024, 3, 14)

    def test_missing_spaces_in_the_hereinafter_phrase(self):
        matches = find_parties("Anita Desai,hereinaftercalledthePURCHASER")
        assert matches
        assert "role=purchaser" in matches[0].notes

    def test_glued_connective_is_stripped_from_a_name(self):
        matches = find_parties(
            "BETWEENRameshPatil, hereinafter called the VENDOR")
        assert matches
        assert not matches[0].value.raw.upper().startswith("BETWEEN")

    def test_execution_date_survives_an_ocr_line_break(self):
        """OCR line breaks are not sentence boundaries."""
        text = "This Deed is executed at Mumbai on the 14th day of\nMarch 2024 BETWEEN"
        matches = find_execution_date(text)
        assert matches and matches[0].value.value == date(2024, 3, 14)


# =====================================================================================
# Consideration anchoring
# =====================================================================================


class TestConsiderationAnchoring:
    def test_picks_the_anchored_amount_not_the_largest(self):
        """Deliberately not 'largest amount wins' - stamp duty is not the price."""
        text = "Municipal deposit Rs. 9,99,00,000/-. Consideration: Rs. 1,25,00,000/-"
        matches = find_consideration_amount(text)
        assert len(matches) == 1
        assert matches[0].value.amount.rupees == Decimal("12500000")

    def test_no_anchor_yields_nothing(self):
        assert find_consideration_amount("Stamp Duty paid Rs. 7,50,000/-") == []

    def test_all_amounts_finder_still_sees_everything(self):
        assert len(find_consideration(
            "Rs. 1,00,000 and Rs. 2,00,000 and Rs. 3,00,000")) == 3


# =====================================================================================
# Span grounding - ADR-0004
# =====================================================================================


class TestGrounding:
    def test_exact_value_is_located(self):
        doc = make_doc(["Consideration: Rs. 1,25,00,000"])
        loc = locate(doc, "Rs. 1,25,00,000")
        assert loc is not None and loc.page == 1

    def test_whitespace_differences_are_tolerated(self):
        doc = make_doc(["Consideration:  Rs.   1,25,00,000"])
        assert is_grounded(doc, "Rs. 1,25,00,000")

    def test_value_not_on_the_page_is_rejected(self):
        """The control that stops a hallucinated value becoming a claim."""
        doc = make_doc(["Consideration: Rs. 1,25,00,000"])
        assert not is_grounded(doc, "Rs. 9,99,99,999")

    def test_ground_raises_rather_than_returning_none(self):
        doc = make_doc(["some text"])
        with pytest.raises(GroundingError):
            ground(doc, "DOC1", "value that is not there")

    def test_provenance_carries_page_span_and_bbox(self):
        doc = make_doc(["line one", "page two has Rs. 5,00,000 on it"])
        prov = ground(doc, "DOC1", "Rs. 5,00,000")
        assert prov.page == 2
        assert prov.span is not None
        assert prov.bbox is not None
        assert prov.source_text and "5,00,000" in prov.source_text

    def test_text_layer_provenance_has_no_ocr_confidence(self):
        doc = make_doc(["Rs. 5,00,000"], source=TextSource.TEXT_LAYER)
        assert ground(doc, "DOC1", "Rs. 5,00,000").from_text_layer

    def test_ocr_provenance_carries_confidence(self):
        doc = make_doc(["Rs. 5,00,000"], source=TextSource.OCR, confidence=0.82)
        prov = ground(doc, "DOC1", "Rs. 5,00,000")
        assert prov.ocr_confidence == pytest.approx(0.82)

    def test_span_indexes_the_page_text(self):
        doc = make_doc(["Consideration: Rs. 1,25,00,000 total"])
        prov = ground(doc, "DOC1", "Rs. 1,25,00,000")
        page = doc.page(1)
        assert page.text[prov.span.start:prov.span.end] == "Rs. 1,25,00,000"


# =====================================================================================
# Schemas carry law
# =====================================================================================


class TestSchemas:
    def test_agreement_of_sale_has_no_owner_field(self):
        """TPA s.54: a contract for sale creates no interest in the property.

        If this field ever appears, a case holding only an Agreement of Sale would
        appear to establish ownership.
        """
        schema = schema_for(DocumentType.AGREEMENT_OF_SALE)
        assert schema.field("owner") is None
        assert not any(f.attribute == "party.owner" for f in schema.fields)

    def test_sale_deed_does_have_an_owner_field(self):
        schema = schema_for(DocumentType.SALE_DEED)
        assert any(f.attribute == "party.owner" for f in schema.fields)

    def test_property_papers_has_no_schema(self):
        """Catch-all label, not a recognisable document."""
        assert schema_for(DocumentType.PROPERTY_PAPERS) is None

    def test_unknown_has_no_schema(self):
        assert schema_for(DocumentType.UNKNOWN) is None

    def test_every_schema_field_names_an_attribute(self):
        from dmocr.extract import SCHEMAS
        for schema in SCHEMAS.values():
            for f in schema.fields:
                assert f.attribute and "." in f.attribute


# =====================================================================================
# Extraction service
# =====================================================================================


class TestExtractionService:
    def _extract(self, text: str = SALE_DEED_TEXT,
                 doc_type: DocumentType = DocumentType.SALE_DEED, **kw):
        doc = make_doc([text], **kw)
        return ExtractionService().extract(
            doc, document_id="DOC1", document_type=doc_type), doc

    def test_extracts_core_sale_deed_fields(self):
        result, _ = self._extract()
        names = result.field_names()
        assert {"consideration", "execution_date", "area", "cts_number"} <= names

    def test_consideration_is_the_anchored_amount(self):
        result, _ = self._extract()
        amounts = result.by_attribute("transaction.consideration")
        assert len(amounts) == 1
        assert amounts[0].value.amount.rupees == Decimal("12500000")

    def test_every_extracted_field_is_grounded(self):
        """No code path emits an ungrounded claim."""
        result, doc = self._extract()
        assert result.fields
        for f in result.fields:
            page = doc.page(f.provenance.page)
            assert f.provenance.span is not None
            assert page.text[f.provenance.span.start:f.provenance.span.end] == f.raw

    def test_missing_required_fields_are_reported(self):
        result, _ = self._extract("DEED OF SALE\nNothing else useful here.")
        assert "consideration" in result.missing_required

    def test_no_schema_extracts_nothing_and_says_so(self):
        result, _ = self._extract(SALE_DEED_TEXT, DocumentType.PROPERTY_PAPERS)
        assert result.fields == []
        assert any("No extraction schema" in n for n in result.notes)

    def test_no_text_is_reported_as_needing_ocr(self):
        doc = OcrDocument(document_sha256="s", pages=[])
        result = ExtractionService().extract(
            doc, document_id="D", document_type=DocumentType.SALE_DEED)
        assert any("OCR" in n for n in result.notes)
        assert result.missing_required

    def test_conflicting_values_in_one_document_become_competing_claims(self):
        """An internal contradiction surfaces like a cross-document one."""
        text = "Carpet Area: 1150 sq. ft.\nCarpet Area: 1400 sq. ft."
        result, _ = self._extract(text)
        areas = result.by_attribute("property.area")
        assert len(areas) == 2

    def test_repeated_identical_values_collapse(self):
        text = "Carpet Area: 1150 sq. ft.\nCarpet Area: 1150 sq. ft."
        result, _ = self._extract(text)
        assert len(result.by_attribute("property.area")) == 1

    def test_ocr_confidence_drives_field_confidence(self):
        from dmocr.model import ConfidenceTier
        high, _ = self._extract(source=TextSource.OCR, confidence=0.95)
        low, _ = self._extract(source=TextSource.OCR, confidence=0.4)
        assert any(f.confidence is ConfidenceTier.HIGH for f in high.fields)
        assert all(f.confidence is ConfidenceTier.LOW for f in low.fields)

    def test_figures_words_conflict_caps_confidence(self):
        from dmocr.model import ConfidenceTier
        text = "Consideration: Rs. 1,00,000/- (Rupees Five Lakh only)"
        result, _ = self._extract(text)
        field = result.by_attribute("transaction.consideration")[0]
        assert field.confidence is ConfidenceTier.LOW

    def test_a_broken_finder_does_not_lose_the_document(self):
        from dmocr.extract.schema import DocumentSchema, FieldSpec

        def boom(text):
            raise RuntimeError("bad finder")

        schema = DocumentSchema(DocumentType.SALE_DEED, [
            FieldSpec("bad", "x.y", boom),
            FieldSpec("area", "property.area", find_area),
        ])
        svc = ExtractionService(schemas=lambda t: schema)
        result = svc.extract(make_doc([SALE_DEED_TEXT]),
                             document_id="D", document_type=DocumentType.SALE_DEED)
        assert result.by_attribute("property.area")
        assert any("finder failed" in n for n in result.notes)


# =====================================================================================
# Claims
# =====================================================================================


class TestClaims:
    def test_claims_carry_document_instrument_strength(self):
        doc = make_doc([SALE_DEED_TEXT])
        result = ExtractionService().extract(
            doc, document_id="DOC1", document_type=DocumentType.SALE_DEED)
        claims = ExtractionService.to_claims(result, subject_id="PRP1")
        assert claims
        assert all(c.instrument_strength is InstrumentStrength.TITLE_TRANSFERRING
                   for c in claims)

    def test_agreement_of_sale_claims_cannot_establish_ownership(self):
        text = SALE_DEED_TEXT.replace("DEED OF SALE", "AGREEMENT FOR SALE")
        doc = make_doc([text])
        result = ExtractionService().extract(
            doc, document_id="DOC2", document_type=DocumentType.AGREEMENT_OF_SALE)
        claims = ExtractionService.to_claims(result, subject_id="PRP1")
        assert claims
        assert not any(c.can_establish_ownership() for c in claims)

    def test_claims_can_be_filtered_by_attribute(self):
        doc = make_doc([SALE_DEED_TEXT])
        result = ExtractionService().extract(
            doc, document_id="DOC1", document_type=DocumentType.SALE_DEED)
        claims = ExtractionService.to_claims(
            result, subject_id="PRP1", attributes={"property.area"})
        assert claims and all(c.attribute == "property.area" for c in claims)

    def test_claims_feed_the_canonical_model(self):
        from dmocr.model import Determination, Property

        doc = make_doc([SALE_DEED_TEXT])
        result = ExtractionService().extract(
            doc, document_id="DOC1", document_type=DocumentType.SALE_DEED)
        prop = Property()
        prop.add_claims(ExtractionService.to_claims(result, subject_id=prop.property_id))
        # One source only, so a lone assertion must not read as corroborated.
        assert prop.resolve("property.area").determination is Determination.NOT_DETERMINABLE


# =====================================================================================
# End to end
# =====================================================================================


@pytest.mark.slow
class TestEndToEnd:
    def test_ocr_classify_extract(self, text_scan_pdf: Path):
        """The full path on a scanned document: OCR -> classify -> grounded claims."""
        from dmocr.classify import RuleClassifier
        from dmocr.ingest import sha256_hex
        from dmocr.ocr import InMemoryOcrCache, TextExtractionService, default_engine

        engine = default_engine()
        if not engine.available:
            pytest.skip("no OCR engine installed")

        data = text_scan_pdf.read_bytes()
        ocr_doc, _ = TextExtractionService(engine, InMemoryOcrCache()).extract(
            data, sha256_hex(data))

        classified = RuleClassifier().classify(ocr_doc.page_texts())
        assert classified.document_type is DocumentType.SALE_DEED

        result = ExtractionService().extract(
            ocr_doc, document_id="DOC1", document_type=classified.document_type)

        assert result.by_attribute("transaction.consideration")
        assert result.by_attribute("property.area")

        # Everything extracted from OCR text must still be locatable in it.
        for f in result.fields:
            page = ocr_doc.page(f.provenance.page)
            assert page.text[f.provenance.span.start:f.provenance.span.end] == f.raw
            assert f.provenance.bbox is not None
