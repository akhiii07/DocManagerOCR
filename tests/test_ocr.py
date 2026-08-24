"""Tests for text extraction.

Three themes:

* **Coordinates.** Both sources use different native systems. Getting the conversion wrong
  does not crash - it silently highlights the wrong part of the page when a reviewer
  clicks a finding, which quietly destroys trust in every citation.
* **Per-page routing.** A digital deed with a scanned annexure must route each page
  independently.
* **Degrading honestly.** No engine, a failed page, or an unreadable cache entry must all
  produce a visible EMPTY page or a recompute - never a silent gap.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dmocr.ingest import sha256_hex
from dmocr.model.provenance import BoundingBox
from dmocr.ocr import (
    FakeOcrEngine,
    FileOcrCache,
    InMemoryOcrCache,
    NullOcrCache,
    OcrPage,
    TextExtractionService,
    TextSource,
    UnavailableEngine,
    assemble_page,
    cache_key,
    default_engine,
)
from dmocr.ocr.rapid import RapidOcrEngine, _parse_confidence


def bb(x0, y0, x1, y1) -> BoundingBox:
    return BoundingBox(x0=x0, y0=y0, x1=x1, y1=y1)


def fake(blocks_by_page: dict | None = None, engine_id: str = "fake/1") -> FakeOcrEngine:
    return FakeOcrEngine(blocks_by_page or {}, engine_id=engine_id)


# =====================================================================================
# Page assembly and offsets
# =====================================================================================


class TestAssembly:
    def test_blocks_are_ordered_top_to_bottom(self):
        page = assemble_page(1, 595, 842, [
            ("second", bb(50, 100, 200, 120), 0.9),
            ("first", bb(50, 50, 200, 70), 0.9),
        ], TextSource.OCR, "fake/1")
        assert [b.text for b in page.blocks] == ["first", "second"]

    def test_same_line_orders_left_to_right(self):
        page = assemble_page(1, 595, 842, [
            ("right", bb(300, 50, 400, 70), 0.9),
            ("left", bb(50, 50, 150, 70), 0.9),
        ], TextSource.OCR, "fake/1")
        assert [b.text for b in page.blocks] == ["left", "right"]

    def test_char_offsets_index_into_page_text(self):
        page = assemble_page(1, 595, 842, [
            ("alpha", bb(50, 50, 200, 70), 0.9),
            ("beta", bb(50, 100, 200, 120), 0.9),
        ], TextSource.OCR, "fake/1")
        for b in page.blocks:
            assert page.text[b.char_start:b.char_end] == b.text

    def test_empty_blocks_are_dropped(self):
        page = assemble_page(1, 595, 842, [
            ("", bb(50, 50, 200, 70), 0.9),
            ("kept", bb(50, 100, 200, 120), 0.9),
        ], TextSource.OCR, "fake/1")
        assert len(page.blocks) == 1

    def test_no_blocks_yields_empty_source(self):
        page = assemble_page(1, 595, 842, [], TextSource.OCR, "fake/1")
        assert page.source is TextSource.EMPTY
        assert page.engine is None

    def test_block_at_maps_an_offset_back_to_a_bbox(self):
        page = assemble_page(1, 595, 842, [
            ("alpha", bb(50, 50, 200, 70), 0.9),
            ("beta", bb(50, 100, 200, 120), 0.9),
        ], TextSource.OCR, "fake/1")
        blk = page.block_at(page.text.index("beta"))
        assert blk is not None and blk.text == "beta"
        assert blk.bbox.y0 == 100

    def test_blocks_for_span_covers_multi_line_values(self):
        page = assemble_page(1, 595, 842, [
            ("Rs. 1,25,00,000", bb(50, 50, 200, 70), 0.9),
            ("only", bb(50, 100, 200, 120), 0.9),
        ], TextSource.OCR, "fake/1")
        assert len(page.blocks_for_span(0, len(page.text))) == 2

    def test_offset_outside_any_block_returns_none(self):
        page = assemble_page(1, 595, 842, [("a", bb(1, 1, 2, 2), 0.9)],
                             TextSource.OCR, "fake/1")
        assert page.block_at(999) is None


# =====================================================================================
# Confidence semantics
# =====================================================================================


class TestConfidence:
    def test_text_layer_confidence_is_none_not_one(self):
        """No recognition step happened. That is not the same as perfect confidence."""
        page = assemble_page(1, 595, 842, [("exact", bb(1, 1, 2, 2), None)],
                             TextSource.TEXT_LAYER)
        assert page.blocks[0].confidence is None
        assert page.blocks[0].is_exact
        assert page.mean_confidence is None

    def test_ocr_confidence_is_averaged(self):
        page = assemble_page(1, 595, 842, [
            ("a", bb(1, 1, 2, 2), 0.8), ("b", bb(1, 3, 2, 4), 0.6),
        ], TextSource.OCR, "fake/1")
        assert page.mean_confidence == pytest.approx(0.7)
        assert page.min_confidence == pytest.approx(0.6)

    @pytest.mark.parametrize("raw,expected", [
        ("0.8959", 0.8959), (0.5, 0.5), ("1.5", 1.0), ("-0.2", 0.0),
        ("not a number", None), (None, None),
    ])
    def test_confidence_parsing_is_defensive(self, raw, expected):
        """A malformed score becomes None - never 0.0 or 1.0, which are assertions."""
        assert _parse_confidence(raw) == expected


# =====================================================================================
# Coordinates
# =====================================================================================


class TestCoordinates:
    def test_text_layer_y_axis_is_flipped_to_top_left(self, digital_pdf: Path):
        """PDFium origin is bottom-left. The evidence model's is top-left."""
        from dmocr.ocr.textlayer import extract_page
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(digital_pdf))
        try:
            page = extract_page(pdf, 1)
        finally:
            pdf.close()

        first = page.blocks[0]
        # The fixture's first line sits near the top of the page, so in top-left
        # coordinates its y0 must be small relative to page height.
        assert first.bbox.y0 < page.height_pt * 0.2
        assert all(0 <= b.bbox.y0 <= page.height_pt for b in page.blocks)

    def test_text_layer_blocks_are_in_reading_order(self, digital_pdf: Path):
        from dmocr.ocr.textlayer import extract_page
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(digital_pdf))
        try:
            page = extract_page(pdf, 1)
        finally:
            pdf.close()
        ys = [b.bbox.y0 for b in page.blocks]
        assert ys == sorted(ys)

    def test_ocr_pixels_are_converted_to_points(self):
        """A quad at scale 2.0 must halve into points."""
        quad = [[100, 200], [300, 200], [300, 260], [100, 260]]
        box = RapidOcrEngine._quad_to_bbox(quad, scale=2.0)
        assert (box.x0, box.y0, box.x1, box.y1) == (50.0, 100.0, 150.0, 130.0)

    def test_rotated_quad_becomes_enclosing_box(self):
        quad = [[110, 200], [300, 190], [305, 250], [115, 262]]
        box = RapidOcrEngine._quad_to_bbox(quad, scale=1.0)
        assert (box.x0, box.y0, box.x1, box.y1) == (110.0, 190.0, 305.0, 262.0)

    def test_malformed_quad_is_skipped_not_crashed(self):
        assert RapidOcrEngine._quad_to_bbox("nonsense", scale=1.0) is None
        assert RapidOcrEngine._quad_to_bbox([[1, 2]], scale=0) is None


# =====================================================================================
# Per-page routing
# =====================================================================================


class TestRouting:
    def test_digital_pdf_uses_text_layer_only(self, digital_pdf: Path):
        engine = fake()
        svc = TextExtractionService(engine, InMemoryOcrCache())
        doc, stats = svc.extract(digital_pdf.read_bytes(), "d")
        assert doc.sources == {"text_layer": 12}
        assert stats.ocr_pages == 0
        assert engine.calls == []          # never OCR what you can read

    def test_scanned_pdf_uses_ocr(self, good_scan_pdf: Path):
        engine = fake({n: [("x", bb(1, n, 2, n + 1), 0.9)] for n in range(1, 7)})
        svc = TextExtractionService(engine, InMemoryOcrCache())
        doc, stats = svc.extract(good_scan_pdf.read_bytes(), "s")
        assert stats.ocr_pages == 6
        assert stats.text_layer_pages == 0

    def test_mixed_bundle_routes_each_page_independently(self, mixed_bundle_pdf: Path):
        """The case the quality gate flags as MIXED. Closes per-page routing."""
        engine = fake({1: [("scanned annexure", bb(10, 10, 100, 30), 0.85)]})
        svc = TextExtractionService(engine, InMemoryOcrCache())
        doc, stats = svc.extract(mixed_bundle_pdf.read_bytes(), "m")

        assert doc.sources == {"text_layer": 2, "ocr": 1}
        assert stats.text_layer_pages == 2 and stats.ocr_pages == 1
        assert doc.page(1).source is TextSource.TEXT_LAYER
        assert doc.page(3).source is TextSource.OCR
        assert doc.page(1).mean_confidence is None      # exact
        assert doc.page(3).mean_confidence == pytest.approx(0.85)

    def test_page_subset_can_be_requested(self, digital_pdf: Path):
        svc = TextExtractionService(fake(), InMemoryOcrCache())
        doc, _ = svc.extract(digital_pdf.read_bytes(), "d", pages=[2, 4])
        assert [p.page for p in doc.pages] == [2, 4]

    def test_out_of_range_page_is_recorded_not_raised(self, digital_pdf: Path):
        svc = TextExtractionService(fake(), InMemoryOcrCache())
        doc, stats = svc.extract(digital_pdf.read_bytes(), "d", pages=[99])
        assert doc.pages == []
        assert any("out of range" in f for f in stats.failures)

    def test_threshold_controls_routing(self, digital_pdf: Path):
        """Raising min_text_chars pushes text-layer pages onto the OCR path."""
        engine = fake()
        svc = TextExtractionService(engine, InMemoryOcrCache(), min_text_chars=100_000)
        _, stats = svc.extract(digital_pdf.read_bytes(), "d")
        assert stats.text_layer_pages == 0
        assert stats.ocr_pages == 12


# =====================================================================================
# Degrading honestly
# =====================================================================================


class TestDegradation:
    def test_no_engine_still_extracts_digital_pages(self, digital_pdf: Path):
        svc = TextExtractionService(UnavailableEngine("none installed"))
        doc, stats = svc.extract(digital_pdf.read_bytes(), "d")
        assert stats.text_layer_pages == 12
        assert doc.page(1).text

    def test_no_engine_marks_scanned_pages_empty_not_missing(self, good_scan_pdf: Path):
        """A page we cannot read must still appear, so the gap is visible."""
        svc = TextExtractionService(UnavailableEngine("none installed"))
        doc, stats = svc.extract(good_scan_pdf.read_bytes(), "s")
        assert doc.page_count == 6
        assert stats.empty_pages == 6
        assert all(p.source is TextSource.EMPTY for p in doc.pages)

    def test_engine_failure_on_one_page_does_not_lose_the_others(self, good_scan_pdf: Path):
        class Flaky(FakeOcrEngine):
            def recognise(self, page):
                super().recognise(page)
                if len(self.calls) == 2:
                    raise RuntimeError("boom")
                return [("ok", bb(1, 1, 2, 2), 0.9)]

        svc = TextExtractionService(Flaky(), InMemoryOcrCache())
        doc, stats = svc.extract(good_scan_pdf.read_bytes(), "s")
        assert doc.page_count == 6
        assert stats.empty_pages == 1
        assert any("OCR failed" in f for f in stats.failures)

    def test_unparseable_pdf_is_reported_not_raised(self):
        svc = TextExtractionService(fake())
        doc, stats = svc.extract(b"%PDF-1.4 not really", "bad")
        assert doc.pages == []
        assert stats.failures

    def test_unavailable_engine_raises_only_if_called_directly(self):
        with pytest.raises(RuntimeError, match="No OCR engine available"):
            UnavailableEngine("nope").recognise(None)


# =====================================================================================
# Caching
# =====================================================================================


class TestCache:
    def test_second_pass_hits_the_cache(self, good_scan_pdf: Path):
        engine = fake({n: [("x", bb(1, 1, 2, 2), 0.9)] for n in range(1, 7)})
        cache = InMemoryOcrCache()
        svc = TextExtractionService(engine, cache)
        data = good_scan_pdf.read_bytes()

        svc.extract(data, "s")
        calls_after_first = len(engine.calls)
        _, stats = svc.extract(data, "s")
        assert stats.cache_hits == 6
        assert len(engine.calls) == calls_after_first    # engine not called again

    def test_empty_cache_is_not_discarded_as_falsy(self):
        """Regression: `cache or NullOcrCache()` dropped a fresh cache, because caches
        define __len__ and an empty one is falsy. Caching then silently never stored."""
        cache = InMemoryOcrCache()
        assert len(cache) == 0
        assert bool(cache) is True
        svc = TextExtractionService(fake(), cache)
        assert svc.cache is cache

    def test_key_includes_engine_so_an_upgrade_invalidates(self):
        a = cache_key("sha", 1, "rapidocr/1.2.3", 200)
        b = cache_key("sha", 1, "rapidocr/1.3.0", 200)
        assert a != b

    def test_key_includes_page_and_dpi(self):
        assert cache_key("s", 1, "e", 200) != cache_key("s", 2, "e", 200)
        assert cache_key("s", 1, "e", 200) != cache_key("s", 1, "e", 300)

    def test_engine_change_forces_recompute(self, good_scan_pdf: Path):
        data = good_scan_pdf.read_bytes()
        cache = InMemoryOcrCache()
        blocks = {n: [("x", bb(1, 1, 2, 2), 0.9)] for n in range(1, 7)}

        TextExtractionService(fake(blocks, "old/1"), cache).extract(data, "s")
        new_engine = fake(blocks, "new/2")
        _, stats = TextExtractionService(new_engine, cache).extract(data, "s")
        assert stats.cache_hits == 0
        assert len(new_engine.calls) == 6

    def test_null_cache_never_stores(self, good_scan_pdf: Path):
        engine = fake({n: [("x", bb(1, 1, 2, 2), 0.9)] for n in range(1, 7)})
        svc = TextExtractionService(engine, NullOcrCache())
        data = good_scan_pdf.read_bytes()
        svc.extract(data, "s")
        _, stats = svc.extract(data, "s")
        assert stats.cache_hits == 0

    def test_file_cache_round_trips(self, tmp_path: Path):
        cache = FileOcrCache(tmp_path / "ocr")
        page = assemble_page(3, 595, 842, [("hello", bb(1, 2, 3, 4), 0.77)],
                             TextSource.OCR, "fake/1")
        cache.put("k", page)
        got = cache.get("k")
        assert got is not None
        assert got.text == "hello"
        assert got.blocks[0].confidence == pytest.approx(0.77)
        assert got.page == 3

    def test_file_cache_recomputes_on_corrupt_entry(self, tmp_path: Path):
        """A corrupt entry must not break extraction."""
        cache = FileOcrCache(tmp_path / "ocr")
        page = assemble_page(1, 1, 1, [("x", bb(1, 1, 2, 2), 0.5)], TextSource.OCR, "f/1")
        cache.put("k", page)
        cache._path("k").write_text("{ this is not json", encoding="utf-8")
        assert cache.get("k") is None

    def test_file_cache_leaves_no_partial_files(self, tmp_path: Path):
        cache = FileOcrCache(tmp_path / "ocr")
        cache.put("k", assemble_page(1, 1, 1, [("x", bb(1, 1, 2, 2), 0.5)],
                                     TextSource.OCR, "f/1"))
        assert list((tmp_path / "ocr").rglob("*.partial")) == []


# =====================================================================================
# Document-level views
# =====================================================================================


class TestDocumentViews:
    def test_page_texts_are_ordered_for_the_classifier(self, mixed_bundle_pdf: Path):
        engine = fake({1: [("annexure", bb(1, 1, 2, 2), 0.9)]})
        doc, _ = TextExtractionService(engine, InMemoryOcrCache()).extract(
            mixed_bundle_pdf.read_bytes(), "m")
        texts = doc.page_texts()
        assert len(texts) == 3
        assert "annexure" in texts[2]

    def test_document_mean_confidence_ignores_exact_pages(self, mixed_bundle_pdf: Path):
        engine = fake({1: [("a", bb(1, 1, 2, 2), 0.6)]})
        doc, _ = TextExtractionService(engine, InMemoryOcrCache()).extract(
            mixed_bundle_pdf.read_bytes(), "m")
        assert doc.mean_confidence == pytest.approx(0.6)
        assert doc.ocr_page_count == 1


# =====================================================================================
# Real engine
# =====================================================================================


@pytest.mark.slow
class TestRealEngine:
    def test_rapidocr_reads_the_rendered_fixture(self, text_scan_pdf: Path):
        engine = default_engine()
        if not engine.available:
            pytest.skip("no OCR engine installed")

        data = text_scan_pdf.read_bytes()
        svc = TextExtractionService(engine, InMemoryOcrCache())
        doc, stats = svc.extract(data, sha256_hex(data))

        assert stats.ocr_pages == 1
        page = doc.page(1)
        assert page.source is TextSource.OCR
        assert page.engine.startswith("rapidocr")

        upper = page.text.upper().replace(" ", "")
        assert "DEEDOFSALE" in upper
        assert "MUMBAI" in upper
        assert page.mean_confidence and page.mean_confidence > 0.5

        # The title must sit near the top of the page in top-left coordinates.
        title = next(b for b in page.blocks if "DEED" in b.text.upper())
        assert title.bbox.y0 < page.height_pt * 0.25

    def test_ocr_output_classifies_correctly(self, text_scan_pdf: Path):
        """OCR -> classification, the link that unblocks scanned documents."""
        from dmocr.classify import RuleClassifier
        from dmocr.model import DocumentType

        engine = default_engine()
        if not engine.available:
            pytest.skip("no OCR engine installed")

        data = text_scan_pdf.read_bytes()
        doc, _ = TextExtractionService(engine, InMemoryOcrCache()).extract(
            data, sha256_hex(data))
        result = RuleClassifier().classify(doc.page_texts())
        assert result.document_type is DocumentType.SALE_DEED
