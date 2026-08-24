"""RapidOCR adapter.

RapidOCR is an ONNX packaging of the PP-OCR models. Chosen over PaddleOCR itself for two
reasons, both recorded in ADR-0013:

* **PaddlePaddle publishes no wheels for this Python version**, so the reference
  implementation is not installable here at all.
* `rapidocr-onnxruntime` **bundles its models in the wheel**. The newer `rapidocr` 3.x
  downloads them at runtime, which is outbound network activity on a machine that is
  supposed to have none. Under the privacy constraint, a self-contained wheel is not a
  convenience — it is the requirement.

Coordinate conversion happens here: RapidOCR returns top-left pixel quadrilaterals at the
render scale, and the contract is top-left PDF points.
"""

from __future__ import annotations

import logging
from functools import cached_property

from ..model.provenance import BoundingBox
from .engine import EngineBlock, OcrEngine, RenderedPage

log = logging.getLogger(__name__)


class RapidOcrEngine(OcrEngine):
    """PP-OCR via onnxruntime, CPU by default."""

    def __init__(self, *, target_dpi: float = 200.0, **rapidocr_kwargs):
        self._target_dpi = target_dpi
        self._kwargs = rapidocr_kwargs
        self._unavailable_reason: str | None = None

    @cached_property
    def _ocr(self):
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:
            self._unavailable_reason = f"rapidocr-onnxruntime not installed ({exc})"
            return None
        try:
            return RapidOCR(**self._kwargs)
        except Exception as exc:  # model files missing, bad config
            self._unavailable_reason = f"RapidOCR failed to initialise: {exc}"
            return None

    @cached_property
    def _version(self) -> str:
        try:
            from importlib.metadata import version

            return version("rapidocr-onnxruntime")
        except Exception:
            return "unknown"

    @property
    def engine_id(self) -> str:
        return f"rapidocr-onnxruntime/{self._version}"

    @property
    def target_dpi(self) -> float:
        return self._target_dpi

    @property
    def available(self) -> bool:
        return self._ocr is not None

    def recognise(self, page: RenderedPage) -> list[EngineBlock]:
        ocr = self._ocr
        if ocr is None:
            raise RuntimeError(self._unavailable_reason or "RapidOCR unavailable")

        result, _elapse = ocr(page.image)
        if not result:
            return []

        blocks: list[EngineBlock] = []
        for item in result:
            try:
                quad, text, score = item[0], item[1], item[2]
            except (IndexError, TypeError):
                log.warning("unexpected RapidOCR item shape; skipping")
                continue
            if not text:
                continue

            bbox = self._quad_to_bbox(quad, page.scale)
            if bbox is None:
                continue
            blocks.append((text, bbox, _parse_confidence(score)))
        return blocks

    @staticmethod
    def _quad_to_bbox(quad, scale: float) -> BoundingBox | None:
        """Axis-aligned bounds of a (possibly rotated) quadrilateral, converted to points.

        RapidOCR returns four corners so it can represent skewed text. The evidence model
        uses axis-aligned boxes, so the enclosing rectangle is taken. That slightly
        over-highlights rotated text, which is the right direction to err for a reviewer
        looking for a value on a page.
        """
        try:
            xs = [float(p[0]) for p in quad]
            ys = [float(p[1]) for p in quad]
        except (TypeError, ValueError, IndexError):
            return None
        if not xs or not ys or scale <= 0:
            return None
        return BoundingBox(
            x0=min(xs) / scale,
            y0=min(ys) / scale,
            x1=max(xs) / scale,
            y1=max(ys) / scale,
        )


def _parse_confidence(score) -> float | None:
    """RapidOCR returns confidence as a string. Parse defensively.

    A malformed score becomes None (unknown) rather than 0.0 (certainly wrong) or 1.0
    (certainly right) - both of those would be assertions we cannot support.
    """
    try:
        value = float(score)
    except (TypeError, ValueError):
        return None
    return round(min(max(value, 0.0), 1.0), 4)
