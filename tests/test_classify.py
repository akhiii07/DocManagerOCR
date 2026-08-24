"""Tests for document classification.

The centrepiece is the cross-reference problem: a Sale Deed recites the Agreement of Sale
that preceded it, so naive keyword matching misclassifies it. Several tests pin the
position-weighting behaviour that prevents this.

The other theme is that UNKNOWN is a correct outcome. A wrong classification produces a
full set of confidently wrong extracted fields, so the classifier is tuned to reach for
human review rather than guess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmocr.classify import (
    ClassifierConfig,
    RuleClassifier,
    UnknownReason,
    all_signals,
    apply_to_document,
    known_types,
)
from dmocr.ingest import IngestionService, InMemoryContentStore, pdfinfo
from dmocr.model import (
    Case,
    ConfidenceTier,
    Document,
    DocumentQuality,
    DocumentType,
    LenderType,
    Product,
)

# --------------------------------------------------------------------------------------
# Synthetic document text. Representative phrasing, entirely invented - no real data.
# --------------------------------------------------------------------------------------

SALE_DEED_P1 = """
DEED OF SALE

This DEED OF SALE is made at Mumbai on the 14th day of March 2024 BETWEEN
Shri Ramesh Patil, hereinafter referred to as the VENDOR, of the One Part
AND Smt. Anita Desai, hereinafter referred to as the PURCHASER, of the Other Part.

WHEREAS the Vendor is absolutely seized and possessed of the flat more particularly
described in the Schedule hereunder written.
"""

SALE_DEED_P3_RECITAL = """
AND WHEREAS by an agreement for sale dated 2nd January 2024 the Vendor agreed to
sell the said flat to the Purchaser, and the Purchaser has paid the entire
consideration to the Promoter, and the Allottee has taken possession.

NOW THIS DEED WITNESSETH that the Vendor doth hereby sell, transfer and convey unto
the Purchaser ALL THAT the said flat.
"""

AGREEMENT_P1 = """
AGREEMENT FOR SALE

This AGREEMENT FOR SALE is executed at Mumbai on 2nd January 2024 BETWEEN
Sunrise Developers LLP, hereinafter called the PROMOTER
AND Smt. Anita Desai, hereinafter called the ALLOTTEE.

The said project is registered with MahaRERA under registration number P51900012345.
"""

PROPERTY_TAX_P1 = """
MUNICIPAL CORPORATION OF GREATER MUMBAI
PROPERTY TAX BILL

Property Account Number: A-1234567890
Assessment Number: 0123456789
Rateable Value: 45,000
Bill Period: 2024-2025
"""

POSSESSION_P1 = """
POSSESSION LETTER

We hereby confirm that the quiet and peaceful possession of Flat No. 402 has been
handed over to the Allottee on this 20th day of June 2024.
"""

MODT_P1 = """
MEMORANDUM OF DEPOSIT OF TITLE DEEDS

The Borrower has this day deposited with the Bank the documents of title relating to
the said property with intent to create a security thereon by way of deposit of
title deeds.
"""

AMBIGUOUS_P1 = """
DEED OF SALE and AGREEMENT FOR SALE

This document is titled both ways, which should not be resolved by guessing.
"""

GIBBERISH_P1 = "lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod"


def classifier(**kw) -> RuleClassifier:
    return RuleClassifier(ClassifierConfig(**kw)) if kw else RuleClassifier()


# =====================================================================================
# Happy paths
# =====================================================================================


class TestBasicClassification:
    @pytest.mark.parametrize("pages,expected", [
        ([SALE_DEED_P1], DocumentType.SALE_DEED),
        ([AGREEMENT_P1], DocumentType.AGREEMENT_OF_SALE),
        ([PROPERTY_TAX_P1], DocumentType.PROPERTY_TAX),
        ([POSSESSION_P1], DocumentType.POSSESSION_DOCUMENT),
        ([MODT_P1], DocumentType.MORTGAGE_DEED),
    ])
    def test_classifies_each_document_type(self, pages, expected):
        assert classifier().classify(pages).document_type is expected

    def test_modt_is_recognised_as_a_mortgage_instrument(self):
        """MODT matters: SecurityType gates the TPA s.59 registration check."""
        r = classifier().classify([MODT_P1])
        assert r.document_type is DocumentType.MORTGAGE_DEED
        assert r.confidence in (ConfidenceTier.HIGH, ConfidenceTier.MEDIUM)

    def test_result_carries_evidence_with_page_numbers(self):
        r = classifier().classify([SALE_DEED_P1])
        hits = r.evidence_for(DocumentType.SALE_DEED)
        assert hits
        assert all(h.page == 1 for h in hits)
        assert any("DEED OF SALE" in h.matched_text.upper() for h in hits)

    def test_all_candidate_scores_are_reported(self):
        r = classifier().classify([SALE_DEED_P1, "", SALE_DEED_P3_RECITAL])
        assert len(r.scores) > 1        # a reviewer can see what else was considered


# =====================================================================================
# The cross-reference problem
# =====================================================================================


class TestCrossReference:
    def test_sale_deed_reciting_an_agreement_is_still_a_sale_deed(self):
        """The core misclassification risk. A deed recites the agreement it supersedes."""
        r = classifier().classify([SALE_DEED_P1, "", SALE_DEED_P3_RECITAL])
        assert r.document_type is DocumentType.SALE_DEED
        assert r.confidence in (ConfidenceTier.HIGH, ConfidenceTier.MEDIUM)

    def test_title_phrase_on_a_later_page_is_treated_as_a_recital(self):
        """"AGREEMENT FOR SALE" on page 3 is a reference, not a title."""
        r = classifier().classify(["", "", AGREEMENT_P1])
        aos_hits = [h for h in r.hits if h.signal_name == "aos_title"]
        assert aos_hits == []

    def test_title_phrase_beyond_the_title_region_of_page_1_does_not_fire(self):
        padded = ("x " * 900) + AGREEMENT_P1        # pushes the title past 1200 chars
        r = classifier().classify([padded])
        assert [h for h in r.hits if h.signal_name == "aos_title"] == []

    def test_later_page_matches_are_discounted_not_ignored(self):
        on_page_1 = classifier().classify([SALE_DEED_P3_RECITAL])
        on_page_5 = classifier().classify(["", "", "", "", SALE_DEED_P3_RECITAL])
        s1 = on_page_1.scores.get(DocumentType.SALE_DEED, 0)
        s5 = on_page_5.scores.get(DocumentType.SALE_DEED, 0)
        assert 0 < s5 < s1

    def test_repetition_cannot_swamp_the_score(self):
        once = classifier().classify([SALE_DEED_P1])
        many = classifier().classify([SALE_DEED_P1 * 40])
        assert many.score <= once.score * 2.5


# =====================================================================================
# UNKNOWN is a correct outcome
# =====================================================================================


class TestUnknownRouting:
    def test_no_text_is_reported_distinctly(self):
        """A scanned document must be OCR'd first - that is not a weak classification."""
        r = classifier().classify([])
        assert r.is_unknown
        assert r.unknown_reason is UnknownReason.NO_TEXT
        assert "OCR" in r.note

    def test_blank_pages_are_no_text(self):
        assert classifier().classify(["", "   ", "\n"]).unknown_reason is UnknownReason.NO_TEXT

    def test_unrecognised_content_is_weak(self):
        r = classifier().classify([GIBBERISH_P1])
        assert r.is_unknown
        assert r.unknown_reason is UnknownReason.WEAK

    def test_two_close_candidates_are_ambiguous_not_guessed(self):
        r = classifier().classify([AMBIGUOUS_P1])
        assert r.is_unknown
        assert r.unknown_reason is UnknownReason.AMBIGUOUS
        assert r.runner_up is not None
        assert "too close" in r.note

    def test_unknown_always_needs_human(self):
        assert classifier().classify([GIBBERISH_P1]).needs_human

    def test_weak_evidence_below_threshold_is_unknown(self):
        """A lone corroborating phrase must not decide a type."""
        r = classifier().classify(["The vendor and the purchaser met."])
        assert r.is_unknown

    def test_stricter_config_routes_more_to_humans(self):
        lenient = classifier().classify([POSSESSION_P1])
        strict = classifier(min_score=50.0).classify([POSSESSION_P1])
        assert not lenient.is_unknown
        assert strict.is_unknown and strict.unknown_reason is UnknownReason.WEAK


# =====================================================================================
# Quality interaction
# =====================================================================================


class TestQualityInteraction:
    def test_degraded_source_caps_confidence(self):
        """Decisive phrases from a poor scan still rest on unreliable input."""
        ok = classifier().classify([SALE_DEED_P1], quality=DocumentQuality.OK)
        degraded = classifier().classify([SALE_DEED_P1], quality=DocumentQuality.DEGRADED)
        assert ok.confidence is ConfidenceTier.HIGH
        assert degraded.confidence is ConfidenceTier.MEDIUM
        assert "DEGRADED" in degraded.note

    def test_degraded_does_not_change_the_type(self):
        a = classifier().classify([SALE_DEED_P1], quality=DocumentQuality.OK)
        b = classifier().classify([SALE_DEED_P1], quality=DocumentQuality.DEGRADED)
        assert a.document_type is b.document_type


# =====================================================================================
# Devanagari lexicon
# =====================================================================================


class TestDevanagari:
    def test_marathi_terms_corroborate(self):
        r = classifier().classify(["खरेदीखत " + SALE_DEED_P1])
        assert r.document_type is DocumentType.SALE_DEED
        assert any(h.signal_name == "mr_sale_deed" for h in r.hits)

    def test_marathi_alone_cannot_decide(self):
        """Unvalidated lexicon: low weights must not carry a classification alone."""
        r = classifier().classify(["खरेदीखत"])
        assert r.is_unknown

    def test_lexicon_can_be_disabled_wholesale(self):
        r = classifier(use_devanagari=False).classify(["खरेदीखत " + SALE_DEED_P1])
        assert not any(h.signal_name.startswith("mr_") for h in r.hits)


# =====================================================================================
# Applying to a Document
# =====================================================================================


class TestApplyToDocument:
    def _doc(self, **kw) -> Document:
        return Document(case_id="C1", tenant_id="T1", sha256="a" * 64, **kw)

    def test_sets_type_and_confidence(self):
        doc = self._doc()
        r = classifier().classify([SALE_DEED_P1])
        assert apply_to_document(doc, r)
        assert doc.document_type is DocumentType.SALE_DEED
        assert doc.classification_confidence == pytest.approx(0.9)

    def test_does_not_overwrite_a_deliberate_classification(self):
        """A human's classification outranks the classifier's."""
        doc = self._doc(document_type=DocumentType.PROPERTY_TAX)
        r = classifier().classify([SALE_DEED_P1])
        assert not apply_to_document(doc, r)
        assert doc.document_type is DocumentType.PROPERTY_TAX

    def test_overwrite_is_possible_when_asked(self):
        doc = self._doc(document_type=DocumentType.PROPERTY_TAX)
        r = classifier().classify([SALE_DEED_P1])
        assert apply_to_document(doc, r, overwrite=True)
        assert doc.document_type is DocumentType.SALE_DEED

    def test_unknown_records_zero_confidence(self):
        doc = self._doc()
        apply_to_document(doc, classifier().classify([GIBBERISH_P1]))
        assert doc.document_type is DocumentType.UNKNOWN
        assert doc.classification_confidence == 0.0


# =====================================================================================
# Signal hygiene
# =====================================================================================


class TestSignals:
    def test_every_signal_compiles(self):
        for s in all_signals():
            s.compiled()

    def test_signal_names_are_unique(self):
        names = [s.name for s in all_signals()]
        assert len(names) == len(set(names))

    def test_weights_are_positive(self):
        assert all(s.weight > 0 for s in all_signals())

    def test_property_papers_has_no_signals(self):
        """It is a catch-all label, not a recognisable document. Never auto-assigned."""
        assert DocumentType.PROPERTY_PAPERS not in known_types()

    def test_no_signal_targets_unknown(self):
        assert DocumentType.UNKNOWN not in known_types()


# =====================================================================================
# End to end over a real file
# =====================================================================================


class TestEndToEnd:
    def test_ingest_then_classify_a_digital_pdf(self, digital_pdf: Path):
        case = Case(tenant_id="T1", lender_type=LenderType.HFC,
                    product=Product.HOUSING_LOAN)
        svc = IngestionService(InMemoryContentStore())
        result = svc.ingest_path(case, digital_pdf)
        assert result.accepted
        assert result.document.has_text_layer

        data = digital_pdf.read_bytes()
        pages = [pdfinfo.page_text(data, n)
                 for n in range(1, (result.document.page_count or 0) + 1)]

        r = classifier().classify(pages, quality=result.document.quality)
        # The fixture's text says "AGREEMENT OF SALE between party A and party B".
        assert r.document_type is DocumentType.AGREEMENT_OF_SALE
        assert apply_to_document(result.document, r)
        assert result.document.document_type is DocumentType.AGREEMENT_OF_SALE

    def test_scanned_pdf_cannot_be_classified_without_ocr(self, good_scan_pdf: Path):
        """Honest failure: no text layer means no classification, not a guess."""
        data = good_scan_pdf.read_bytes()
        info = pdfinfo.analyse_pdf(data)
        pages = [pdfinfo.page_text(data, n) for n in range(1, info.page_count + 1)]
        r = classifier().classify(pages)
        assert r.is_unknown
        assert r.unknown_reason is UnknownReason.NO_TEXT
