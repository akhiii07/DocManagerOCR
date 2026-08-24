"""Quality gate.

Runs before any expensive processing. A 90-dpi phone photo of a 40-page deed should be
rejected in milliseconds, not after minutes of GPU time and a confidently wrong extraction.

The important verdict is **DEGRADED**, not REJECTED. Real collateral bundles are often
poor quality, and refusing them pushes work back to a human with no explanation. DEGRADED
means "process, but cap confidence" — the document flows through the pipeline while every
claim extracted from it carries a ceiling on how much the system may assert.

Thresholds are configurable and default to values that should be **re-tuned against a real
corpus survey**. In particular `min_sharpness` is a corpus-relative figure: Laplacian
variance is only comparable between pages rendered at the same scale, so the default here
is a placeholder until `tools/corpus_survey.py` has been run on real documents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ..model.case import DocumentQuality
from .pdfinfo import DocumentInfo, TextLayer


class QualityCode(StrEnum):
    ENCRYPTED = "ENCRYPTED"
    UNPARSEABLE = "UNPARSEABLE"
    NO_PAGES = "NO_PAGES"
    TOO_MANY_PAGES = "TOO_MANY_PAGES"
    LOW_RESOLUTION = "LOW_RESOLUTION"
    VERY_LOW_RESOLUTION = "VERY_LOW_RESOLUTION"
    BLURRY = "BLURRY"
    NO_TEXT_AND_LOW_RESOLUTION = "NO_TEXT_AND_LOW_RESOLUTION"
    ROTATED_PAGES = "ROTATED_PAGES"
    MIXED_PAGE_SIZES = "MIXED_PAGE_SIZES"
    PARTIAL_TEXT_LAYER = "PARTIAL_TEXT_LAYER"
    UNMEASURED_SHARPNESS = "UNMEASURED_SHARPNESS"


class QualityThresholds(BaseModel):
    """Tunable gate configuration.

    Deliberately data, not constants in code, so thresholds can be adjusted per tenant or
    per document type without a release, and so a change is visible in configuration
    review rather than buried in a diff.
    """

    model_config = ConfigDict(frozen=True)

    #: Below this, OCR accuracy degrades noticeably on Indian legal documents.
    min_dpi: float = 200.0
    #: Below this, OCR output is usually not worth the compute.
    reject_dpi: float = 100.0
    #: Corpus-relative. RE-TUNE against p10 of a real corpus survey.
    min_sharpness: float = 60.0
    max_pages: int = 800
    #: A scanned page below this many characters contributed nothing readable.
    treat_as_scanned_below_chars: int = 20


@dataclass(frozen=True)
class QualityFinding:
    code: QualityCode
    detail: str
    #: True if this finding on its own forces REJECTED.
    rejecting: bool = False


@dataclass
class QualityReport:
    verdict: DocumentQuality
    findings: list[QualityFinding] = field(default_factory=list)
    #: Metrics carried forward for the confidence model and the review package.
    metrics: dict = field(default_factory=dict)

    @property
    def notes(self) -> list[str]:
        return [f"{f.code.value}: {f.detail}" for f in self.findings]

    @property
    def caps_confidence(self) -> bool:
        return self.verdict is DocumentQuality.DEGRADED

    @property
    def is_rejected(self) -> bool:
        return self.verdict is DocumentQuality.REJECTED


def assess(info: DocumentInfo, thresholds: QualityThresholds | None = None) -> QualityReport:
    """Turn a structural analysis into a gate verdict."""
    t = thresholds or QualityThresholds()
    findings: list[QualityFinding] = []

    # -- hard failures -----------------------------------------------------------
    if info.encrypted:
        findings.append(QualityFinding(
            QualityCode.ENCRYPTED,
            "Document is password protected and cannot be read.",
            rejecting=True,
        ))
    elif not info.ok:
        findings.append(QualityFinding(
            QualityCode.UNPARSEABLE,
            f"Document could not be parsed ({info.error}).",
            rejecting=True,
        ))
    elif info.page_count == 0:
        findings.append(QualityFinding(
            QualityCode.NO_PAGES, "Document contains no pages.", rejecting=True,
        ))
    elif info.page_count > t.max_pages:
        findings.append(QualityFinding(
            QualityCode.TOO_MANY_PAGES,
            f"{info.page_count} pages exceeds the limit of {t.max_pages}.",
            rejecting=True,
        ))

    if any(f.rejecting for f in findings):
        return QualityReport(DocumentQuality.REJECTED, findings, _metrics(info))

    # -- resolution --------------------------------------------------------------
    dpi = info.min_embedded_dpi
    if info.needs_ocr and dpi is not None:
        if dpi < t.reject_dpi:
            # Not an automatic rejection. A very poor scan is still evidence a human may
            # want to look at, and refusing it outright removes that option. It becomes
            # DEGRADED with the confidence ceiling that implies.
            findings.append(QualityFinding(
                QualityCode.VERY_LOW_RESOLUTION,
                f"Scan resolution {dpi:.0f} dpi is below the usable floor of "
                f"{t.reject_dpi:.0f} dpi. OCR output should not be relied on.",
            ))
        elif dpi < t.min_dpi:
            findings.append(QualityFinding(
                QualityCode.LOW_RESOLUTION,
                f"Scan resolution {dpi:.0f} dpi is below the recommended "
                f"{t.min_dpi:.0f} dpi.",
            ))

    # -- sharpness ---------------------------------------------------------------
    if info.needs_ocr:
        if info.median_sharpness is None:
            findings.append(QualityFinding(
                QualityCode.UNMEASURED_SHARPNESS,
                "Sharpness could not be measured; numpy may be unavailable.",
            ))
        elif info.median_sharpness < t.min_sharpness:
            findings.append(QualityFinding(
                QualityCode.BLURRY,
                f"Median sharpness {info.median_sharpness:.1f} is below the threshold "
                f"of {t.min_sharpness:.1f}. Pages may be blurred or out of focus.",
            ))

    # -- text layer --------------------------------------------------------------
    if info.text_layer == TextLayer.MIXED:
        findings.append(QualityFinding(
            QualityCode.PARTIAL_TEXT_LAYER,
            "Only some pages carry a text layer; pages must be routed individually "
            "between text extraction and OCR.",
        ))
    elif info.text_layer == TextLayer.SCANNED and dpi is None:
        findings.append(QualityFinding(
            QualityCode.NO_TEXT_AND_LOW_RESOLUTION,
            "No text layer and no embedded resolution information; scan quality is "
            "unknown before OCR.",
        ))

    # -- structural oddities -----------------------------------------------------
    if info.rotated_pages:
        findings.append(QualityFinding(
            QualityCode.ROTATED_PAGES,
            f"{info.rotated_pages} page(s) carry a non-zero rotation; layout analysis "
            f"must respect it.",
        ))
    if info.distinct_page_sizes > 1:
        findings.append(QualityFinding(
            QualityCode.MIXED_PAGE_SIZES,
            f"{info.distinct_page_sizes} distinct page sizes, which usually indicates "
            f"appended annexures or differently scanned sections.",
        ))

    verdict = DocumentQuality.DEGRADED if findings else DocumentQuality.OK
    return QualityReport(verdict, findings, _metrics(info))


def _metrics(info: DocumentInfo) -> dict:
    return {
        "page_count": info.page_count,
        "text_layer": info.text_layer,
        "median_chars_per_page": info.median_chars_per_page,
        "min_embedded_dpi": info.min_embedded_dpi,
        "median_sharpness": info.median_sharpness,
        "rotated_pages": info.rotated_pages,
        "distinct_page_sizes": info.distinct_page_sizes,
        "non_latin_ratio": info.non_latin_ratio,
        "needs_ocr": info.needs_ocr,
        "producer": info.producer,
    }
