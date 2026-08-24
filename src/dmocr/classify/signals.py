"""Classification signals.

Distinctive phrases that indicate a document type, with weights. Deliberately explicit
data rather than a learned model, for three reasons:

* **Auditable.** A classification decision can name the phrases that produced it, on the
  pages they appeared on. A reviewer can disagree with a specific piece of evidence.
* **No training data.** There is no labelled Mumbai corpus yet. A supervised classifier
  trained on synthetic documents would learn the generator, not the domain.
* **Cheap to correct.** A misfire is fixed by adjusting a weight, not by retraining.

This is a baseline, not the end state. Once a real labelled corpus exists, a supervised
classifier should be measured against it — and this rule layer stays useful as a prior and
as a sanity check on the model.

THE CROSS-REFERENCE PROBLEM
---------------------------
A Sale Deed routinely recites the Agreement of Sale that preceded it. A Possession Letter
names the flat and the agreement. So raw keyword presence is actively misleading.

Two mitigations, both implemented in the scorer:

1. **Position weighting.** A phrase in the title region of page 1 is far more indicative
   than the same phrase buried on page 14. Later-page matches are discounted heavily.
2. **Per-signal contribution caps.** A phrase repeated forty times contributes little more
   than a phrase appearing twice, so verbose documents cannot swamp the score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..model.common import DocumentType


@dataclass(frozen=True)
class Signal:
    """One phrase pattern indicating a document type."""

    name: str
    pattern: str
    doc_type: DocumentType
    weight: float
    #: Devanagari signals are marked so the classifier can report script coverage and so
    #: an unvalidated lexicon can be disabled wholesale.
    script: str = "latin"
    #: True for phrases that only mean something in the title region of the first page.
    title_only: bool = False

    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE | re.UNICODE)


# =====================================================================================
# Latin-script signals
#
# Weights are relative, not probabilities. A weight of 10 means "this phrase alone
# substantially identifies the document"; 1-3 means "corroborating, meaningless alone".
# =====================================================================================

_SIGNALS: list[Signal] = [
    # -- Sale Deed ------------------------------------------------------------------
    Signal("sale_deed_title", r"\b(deed\s+of\s+(sale|conveyance)|sale\s+deed|conveyance\s+deed)\b",
           DocumentType.SALE_DEED, 10.0, title_only=True),
    Signal("sale_deed_operative", r"\bhereby\s+(sell|sells|sold|grant|convey|transfer)s?\b.{0,80}\b(convey|transfer|assur)",
           DocumentType.SALE_DEED, 8.0),
    Signal("sale_deed_parties", r"\b(vendor|vendee)\b", DocumentType.SALE_DEED, 3.0),
    Signal("sale_deed_absolute", r"\babsolute(ly)?\s+(owner|seized|possessed)\b",
           DocumentType.SALE_DEED, 2.5),

    # -- Agreement of Sale ----------------------------------------------------------
    Signal("aos_title", r"\bagreement\s+(for|of|to)\s+sale\b",
           DocumentType.AGREEMENT_OF_SALE, 10.0, title_only=True),
    Signal("aos_agreement_to_sell", r"\bagreement\s+to\s+sell\b",
           DocumentType.AGREEMENT_OF_SALE, 8.0, title_only=True),
    # Promoter/allottee is RERA vocabulary and strongly indicates an agreement for sale
    # of an under-construction unit rather than a completed conveyance.
    Signal("aos_promoter_allottee", r"\b(promoter|allottee)\b",
           DocumentType.AGREEMENT_OF_SALE, 4.0),
    Signal("aos_maharera", r"\bmaha\s*rera\b|\bP\d{11,}\b",
           DocumentType.AGREEMENT_OF_SALE, 3.0),

    # -- Property tax ---------------------------------------------------------------
    Signal("tax_title", r"\bproperty\s+tax\b", DocumentType.PROPERTY_TAX, 9.0),
    Signal("tax_mcgm", r"\bmunicipal\s+corporation\s+of\s+greater\s+mumbai\b|\bM\.?C\.?G\.?M\.?\b|\bB\.?M\.?C\.?\b",
           DocumentType.PROPERTY_TAX, 5.0),
    Signal("tax_assessment", r"\b(assessment\s+(no|number)|property\s+account\s+(no|number)|\bP\.?I\.?D\.?\b)",
           DocumentType.PROPERTY_TAX, 5.0),
    Signal("tax_rateable", r"\b(rateable\s+value|capital\s+value|bill\s+period)\b",
           DocumentType.PROPERTY_TAX, 4.0),

    # -- Possession -----------------------------------------------------------------
    Signal("possession_title", r"\bpossession\s+(letter|receipt|certificate)\b",
           DocumentType.POSSESSION_DOCUMENT, 10.0, title_only=True),
    Signal("possession_handover", r"\b(handed\s+over|handing\s+over|taken\s+over)\s+(the\s+)?(quiet\s+and\s+peaceful\s+)?possession\b",
           DocumentType.POSSESSION_DOCUMENT, 7.0),

    # -- Mortgage / MODT ------------------------------------------------------------
    # MODT is the Mumbai equitable-mortgage instrument. Recognising it matters because
    # SecurityType gates the TPA s.59 registration check (REQ_TPA_59).
    Signal("modt", r"\bmemorandum\s+of\s+(deposit|entry)\s+.{0,20}title\s+deeds?\b|\bM\.?O\.?D\.?T\.?\b",
           DocumentType.MORTGAGE_DEED, 10.0, title_only=True),
    Signal("mortgage_deed_title", r"\b(deed\s+of\s+mortgage|mortgage\s+deed|deed\s+of\s+simple\s+mortgage)\b",
           DocumentType.MORTGAGE_DEED, 10.0, title_only=True),
    Signal("mortgage_deposit", r"\bdeposit\s+of\s+title\s+deeds?\b",
           DocumentType.MORTGAGE_DEED, 6.0),

    # -- Registration receipt / Index II --------------------------------------------
    Signal("index_ii", r"\bindex[\s\-]*(ii|2)\b", DocumentType.REGISTRATION_RECEIPT, 10.0),
    Signal("sub_registrar", r"\bsub[\s\-]?registrar\b", DocumentType.REGISTRATION_RECEIPT, 3.0),

    # -- Property Card (Mumbai urban land record) -----------------------------------
    Signal("property_card", r"\bproperty\s+(register\s+)?card\b|\bmalmatta\s+patrak\b",
           DocumentType.PROPERTY_CARD, 10.0, title_only=True),
    Signal("cts_number", r"\bC\.?T\.?S\.?\s*(no|number)?\b", DocumentType.PROPERTY_CARD, 3.0),

    # -- Occupancy / completion -----------------------------------------------------
    Signal("occupancy_certificate", r"\boccupancy\s+certificate\b|\bO\.?C\.?\s+granted\b",
           DocumentType.OCCUPANCY_CERTIFICATE, 10.0, title_only=True),

    # -- Encumbrance ----------------------------------------------------------------
    Signal("encumbrance_certificate", r"\bencumbrance\s+certificate\b",
           DocumentType.ENCUMBRANCE_CERTIFICATE, 10.0, title_only=True),
]


# =====================================================================================
# Devanagari (Marathi) signals
#
# UNVALIDATED. These are common Marathi document terms, but they have NOT been checked
# against real Maharashtra instruments by a Marathi reader. They are given LOW weights so
# they can corroborate but never decide a classification on their own, and the whole set
# can be disabled with ClassifierConfig(use_devanagari=False).
#
# Tracked in docs/OPEN-ITEMS.md - this lexicon needs review before it is weighted up.
# =====================================================================================

_DEVANAGARI_SIGNALS: list[Signal] = [
    Signal("mr_sale_deed", r"खरेदीखत", DocumentType.SALE_DEED, 4.0, script="devanagari"),
    Signal("mr_agreement", r"करारनामा", DocumentType.AGREEMENT_OF_SALE, 4.0, script="devanagari"),
    Signal("mr_property_tax", r"मालमत्ता\s*कर", DocumentType.PROPERTY_TAX, 4.0, script="devanagari"),
    Signal("mr_possession", r"ताबा", DocumentType.POSSESSION_DOCUMENT, 3.0, script="devanagari"),
    Signal("mr_mortgage", r"गहाण", DocumentType.MORTGAGE_DEED, 3.0, script="devanagari"),
]


def all_signals(*, use_devanagari: bool = True) -> list[Signal]:
    return [*_SIGNALS, *(_DEVANAGARI_SIGNALS if use_devanagari else [])]


def signals_for(doc_type: DocumentType, *, use_devanagari: bool = True) -> list[Signal]:
    return [s for s in all_signals(use_devanagari=use_devanagari) if s.doc_type == doc_type]


def known_types(*, use_devanagari: bool = True) -> set[DocumentType]:
    return {s.doc_type for s in all_signals(use_devanagari=use_devanagari)}
