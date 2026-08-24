"""Tests for ingestion and the quality gate.

Focus: active content is refused before parsing, poor quality degrades rather than
disappears, deduplication is by content, and a rejected document is still visible to the
reviewer rather than becoming a silent gap in the bundle.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmocr.ingest import (
    DocumentInfo,
    IngestionService,
    InMemoryContentStore,
    LocalContentStore,
    QualityCode,
    QualityThresholds,
    SafetyVerdict,
    TextLayer,
    analyse,
    analyse_pdf,
    assess,
    scan,
    sha256_hex,
    sniff_type,
    summarise_ingest,
)
from dmocr.model import Case, DocumentQuality, DocumentType, LenderType, Product

CLEAN_PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"


def make_case() -> Case:
    return Case(tenant_id="T1", lender_type=LenderType.HFC, product=Product.HOUSING_LOAN)


def service(**kw) -> IngestionService:
    return IngestionService(InMemoryContentStore(), **kw)


# =====================================================================================
# Format sniffing
# =====================================================================================


class TestSniff:
    def test_detects_pdf(self):
        assert sniff_type(CLEAN_PDF) == "pdf"

    def test_detects_png(self):
        assert sniff_type(b"\x89PNG\r\n\x1a\nrest") == "png"

    def test_detects_jpeg(self):
        assert sniff_type(b"\xff\xd8\xff\xe0rest") == "jpeg"

    def test_unknown_returns_none(self):
        assert sniff_type(b"not a document at all") is None

    def test_real_fixtures_are_recognised(self, digital_pdf: Path, photo_jpg: Path):
        assert sniff_type(digital_pdf.read_bytes()) == "pdf"
        assert sniff_type(photo_jpg.read_bytes()) == "jpeg"


# =====================================================================================
# Safety scan
# =====================================================================================


class TestSafetyScan:
    def test_clean_pdf_is_safe(self, digital_pdf: Path):
        assert scan(digital_pdf.read_bytes()).verdict is SafetyVerdict.SAFE

    def test_javascript_is_blocked(self):
        data = b"%PDF-1.4\n<< /OpenAction << /JavaScript (app.alert) >> >>\n%%EOF\n"
        r = scan(data)
        assert r.verdict is SafetyVerdict.BLOCKED
        assert any(f.code == "PDF_JAVASCRIPT" for f in r.findings)

    @pytest.mark.parametrize("token,code", [
        (b"/Launch", "PDF_LAUNCH"),
        (b"/EmbeddedFile", "PDF_EMBEDDED_FILE"),
        (b"/SubmitForm", "PDF_SUBMIT_FORM"),
        (b"/GoToR", "PDF_REMOTE_GOTO"),
    ])
    def test_active_content_is_blocked(self, token: bytes, code: str):
        r = scan(b"%PDF-1.4\n<< " + token + b" >>\n%%EOF\n")
        assert r.verdict is SafetyVerdict.BLOCKED
        assert any(f.code == code for f in r.findings)

    def test_open_action_alone_is_suspicious_not_blocked(self):
        """Not executable on its own; process with a note rather than refusing."""
        r = scan(b"%PDF-1.4\n<< /OpenAction 3 0 R >>\n%%EOF\n")
        assert r.verdict is SafetyVerdict.SUSPICIOUS

    def test_unrecognised_format_is_blocked(self):
        r = scan(b"this is just text")
        assert r.verdict is SafetyVerdict.BLOCKED
        assert any(f.code == "UNRECOGNISED_FORMAT" for f in r.findings)

    def test_extension_mismatch_is_flagged(self):
        """The filename is an assertion; the bytes are the fact."""
        r = scan(CLEAN_PDF, declared_name="sale_deed.jpg")
        assert any(f.code == "EXTENSION_MISMATCH" for f in r.findings)
        assert r.verdict is SafetyVerdict.SUSPICIOUS

    def test_truncated_pdf_flagged(self):
        r = scan(b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n")
        assert any(f.code == "PDF_TRUNCATED" for f in r.findings)


# =====================================================================================
# Structural analysis
# =====================================================================================


class TestAnalysis:
    def test_digital_pdf_has_text_layer(self, digital_pdf: Path):
        info = analyse(digital_pdf)
        assert info.ok
        assert info.text_layer == TextLayer.DIGITAL
        assert not info.needs_ocr          # never OCR what you can read
        assert info.page_count == 12

    def test_scanned_pdf_needs_ocr(self, good_scan_pdf: Path):
        info = analyse(good_scan_pdf)
        assert info.text_layer == TextLayer.SCANNED
        assert info.needs_ocr

    def test_embedded_dpi_is_detected(self, good_scan_pdf: Path, poor_scan_pdf: Path):
        assert analyse(good_scan_pdf).min_embedded_dpi == pytest.approx(300, abs=1)
        assert analyse(poor_scan_pdf).min_embedded_dpi == pytest.approx(120, abs=1)

    def test_sharpness_separates_good_from_blurred(
        self, good_scan_pdf: Path, poor_scan_pdf: Path
    ):
        good = analyse(good_scan_pdf).median_sharpness
        poor = analyse(poor_scan_pdf).median_sharpness
        assert good is not None and poor is not None
        assert good > poor * 10

    def test_analysis_from_bytes_matches_analysis_from_path(self, good_scan_pdf: Path):
        assert (analyse_pdf(good_scan_pdf.read_bytes()).page_count
                == analyse(good_scan_pdf).page_count)

    def test_image_is_always_scanned(self, photo_jpg: Path):
        info = analyse(photo_jpg)
        assert info.text_layer == TextLayer.SCANNED
        assert info.page_count == 1

    def test_unparseable_is_reported_not_raised(self):
        info = analyse_pdf(b"%PDF-1.4\nbut not really a pdf")
        assert not info.ok
        assert info.error


# =====================================================================================
# Quality gate
# =====================================================================================


class TestQualityGate:
    def test_clean_digital_pdf_passes(self, digital_pdf: Path):
        assert assess(analyse(digital_pdf)).verdict is DocumentQuality.OK

    def test_good_scan_passes(self, good_scan_pdf: Path):
        assert assess(analyse(good_scan_pdf)).verdict is DocumentQuality.OK

    def test_poor_scan_degrades_rather_than_rejects(self, poor_scan_pdf: Path):
        """A poor scan is still evidence a human may want to see."""
        report = assess(analyse(poor_scan_pdf))
        assert report.verdict is DocumentQuality.DEGRADED
        assert report.caps_confidence
        codes = {f.code for f in report.findings}
        assert QualityCode.LOW_RESOLUTION in codes
        assert QualityCode.BLURRY in codes

    def test_photo_degrades_on_resolution(self, photo_jpg: Path):
        report = assess(analyse(photo_jpg))
        assert report.verdict is DocumentQuality.DEGRADED
        assert QualityCode.LOW_RESOLUTION in {f.code for f in report.findings}

    def test_encrypted_is_rejected(self):
        info = DocumentInfo(ok=False, error="encrypted", encrypted=True)
        report = assess(info)
        assert report.verdict is DocumentQuality.REJECTED
        assert QualityCode.ENCRYPTED in {f.code for f in report.findings}

    def test_unparseable_is_rejected(self):
        report = assess(DocumentInfo(ok=False, error="open_failed: PdfiumError"))
        assert report.verdict is DocumentQuality.REJECTED

    def test_page_limit_rejects(self, digital_pdf: Path):
        report = assess(analyse(digital_pdf), QualityThresholds(max_pages=3))
        assert report.verdict is DocumentQuality.REJECTED
        assert QualityCode.TOO_MANY_PAGES in {f.code for f in report.findings}

    def test_thresholds_are_tunable(self, poor_scan_pdf: Path):
        """Relaxing thresholds must actually change the verdict."""
        lenient = QualityThresholds(min_dpi=100, min_sharpness=1)
        assert assess(analyse(poor_scan_pdf), lenient).verdict is DocumentQuality.OK

    def test_metrics_are_carried_forward(self, good_scan_pdf: Path):
        m = assess(analyse(good_scan_pdf)).metrics
        assert m["page_count"] == 6
        assert m["needs_ocr"] is True
        assert m["text_layer"] == TextLayer.SCANNED


# =====================================================================================
# Content store
# =====================================================================================


class TestContentStore:
    def test_put_is_content_addressed_and_idempotent(self):
        s = InMemoryContentStore()
        assert s.put(b"hello") == s.put(b"hello") == sha256_hex(b"hello")

    def test_round_trip(self):
        s = InMemoryContentStore()
        assert s.get(s.put(b"payload")) == b"payload"

    def test_missing_digest_raises(self):
        with pytest.raises(KeyError):
            InMemoryContentStore().get("0" * 64)

    def test_local_store_shards_and_persists(self, tmp_path: Path):
        s = LocalContentStore(tmp_path / "blobs")
        digest = s.put(b"contents")
        assert s.exists(digest)
        assert s.get(digest) == b"contents"
        p = s.path_for(digest)
        assert p is not None and p.parts[-3:-1] == (digest[:2], digest[2:4])

    def test_local_store_leaves_no_partial_files(self, tmp_path: Path):
        s = LocalContentStore(tmp_path / "blobs")
        s.put(b"contents")
        assert list((tmp_path / "blobs").rglob("*.partial")) == []


# =====================================================================================
# Ingestion service
# =====================================================================================


class TestIngestionService:
    def test_happy_path_attaches_document(self, digital_pdf: Path):
        case, svc = make_case(), service()
        r = svc.ingest_path(case, digital_pdf, document_type=DocumentType.SALE_DEED)
        assert r.accepted
        assert r.document in case.documents
        assert r.document.document_type is DocumentType.SALE_DEED
        assert r.document.has_text_layer
        assert r.document.page_count == 12

    def test_blocked_content_is_never_stored(self):
        """Refusing to persist active content is the point."""
        case = make_case()
        store = InMemoryContentStore()
        svc = IngestionService(store)
        data = b"%PDF-1.4\n<< /JavaScript (x) >>\n%%EOF\n"
        r = svc.ingest_bytes(case, data, filename="deed.pdf")
        assert not r.accepted
        assert r.document is None
        assert not store.exists(sha256_hex(data))
        assert case.documents == []

    def test_duplicate_content_is_detected_by_hash(self, digital_pdf: Path):
        case, svc = make_case(), service()
        first = svc.ingest_path(case, digital_pdf)
        second = svc.ingest_path(case, digital_pdf)
        assert second.duplicate_of == first.document.document_id
        assert not second.accepted
        assert len(case.documents) == 1

    def test_renamed_duplicate_is_still_a_duplicate(self, digital_pdf: Path, tmp_path: Path):
        """Dedupe is by content, not filename."""
        case, svc = make_case(), service()
        svc.ingest_path(case, digital_pdf)
        copy = tmp_path / "totally_different_name.pdf"
        copy.write_bytes(digital_pdf.read_bytes())
        assert svc.ingest_path(case, copy).duplicate_of is not None

    def test_degraded_document_is_accepted_with_notes(self, poor_scan_pdf: Path):
        case, svc = make_case(), service()
        r = svc.ingest_path(case, poor_scan_pdf)
        assert r.accepted
        assert r.document.quality is DocumentQuality.DEGRADED
        assert r.document.confidence_capped
        assert r.document.quality_notes

    def test_rejected_document_is_still_attached(self, digital_pdf: Path):
        """A rejected file must be visible, not a silent gap in the bundle."""
        case = make_case()
        svc = IngestionService(InMemoryContentStore(), QualityThresholds(max_pages=1))
        r = svc.ingest_path(case, digital_pdf)
        assert not r.accepted
        assert r.document in case.documents
        assert r.document.quality is DocumentQuality.REJECTED
        assert r.reason

    def test_strict_mode_refuses_suspicious_files(self):
        case = make_case()
        svc = IngestionService(InMemoryContentStore(), allow_suspicious=False)
        r = svc.ingest_bytes(case, b"%PDF-1.4\n<< /OpenAction 3 0 R >>\n%%EOF\n")
        assert not r.accepted
        assert case.documents == []

    def test_image_upload_is_handled(self, photo_jpg: Path):
        case, svc = make_case(), service()
        r = svc.ingest_path(case, photo_jpg,
                            document_type=DocumentType.POSSESSION_DOCUMENT)
        assert r.accepted
        assert not r.document.has_text_layer

    def test_local_store_avoids_writing_image_twice(self, photo_jpg: Path, tmp_path: Path):
        case = make_case()
        svc = IngestionService(LocalContentStore(tmp_path / "blobs"))
        r = svc.ingest_path(case, photo_jpg)
        assert r.accepted
        assert len(list((tmp_path / "blobs").rglob("*"))) >= 1

    def test_ingest_directory_and_summary(self, fixtures_dir: Path):
        case, svc = make_case(), service()
        results = svc.ingest_directory(case, fixtures_dir)
        s = summarise_ingest(results)
        assert s["submitted"] == 5
        assert s["blocked"] == 0
        assert s["duplicates"] == 0
        assert s["needs_ocr"] == 3          # 2 scanned PDFs + 1 photo
        assert s["degraded"] >= 2           # poor scan + photo

    def test_tenant_mismatch_is_refused_by_the_case(self, digital_pdf: Path):
        case, svc = make_case(), service()
        case.tenant_id = "T1"
        r = svc.ingest_path(case, digital_pdf)
        assert r.document.tenant_id == "T1"
