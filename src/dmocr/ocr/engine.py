"""OCR engine abstraction.

The engine is replaceable by design (Rule 6). Concretely: swapping RapidOCR for a
different recogniser must not touch the service, the cache key format, the coordinate
convention, or anything downstream.

The contract is narrow on purpose. An engine receives a rendered page image and the scale
it was rendered at, and returns blocks in **top-left PDF points**. Converting from
whatever the engine natively produces is the adapter's job, not the caller's.

`engine_id` is pinned into `ProcessingContext.model_versions` and into the cache key, so a
document re-extracted after an engine upgrade is recomputed rather than served stale.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..model.provenance import BoundingBox


@dataclass(frozen=True)
class RenderedPage:
    """A page image handed to an engine."""

    #: RGB array, shape (height, width, 3). Typed loosely to avoid importing numpy here.
    image: object
    #: Points-to-pixels factor used when rendering. Adapters divide by this to return
    #: coordinates in points.
    scale: float
    width_pt: float
    height_pt: float

    @property
    def dpi(self) -> float:
        return 72.0 * self.scale


#: (text, bbox in top-left PDF points, confidence 0..1 or None)
EngineBlock = tuple[str, BoundingBox, float | None]


class OcrEngine(ABC):
    """Recognises text in a rendered page."""

    @property
    @abstractmethod
    def engine_id(self) -> str:
        """Stable identifier including version, e.g. 'rapidocr-onnxruntime/1.2.3'.

        Part of the cache key. Changing the engine or its version must change this, or
        stale results will be served after an upgrade.
        """

    @property
    def target_dpi(self) -> float:
        """Rendering resolution this engine wants. 200 dpi suits printed legal text."""
        return 200.0

    @abstractmethod
    def recognise(self, page: RenderedPage) -> list[EngineBlock]:
        """Return blocks in top-left PDF points."""

    @property
    def available(self) -> bool:
        """Whether the engine can actually run in this environment.

        Optional dependencies and model files are checked here rather than at import, so
        the package imports cleanly on a machine with no OCR installed and the failure is
        reported where it can be handled.
        """
        return True


class FakeOcrEngine(OcrEngine):
    """Deterministic engine for tests.

    Returns pre-programmed blocks per page. Lets the routing, caching, assembly and
    provenance logic be tested without a real recogniser, which keeps those tests fast and
    independent of model behaviour.
    """

    def __init__(
        self,
        pages: dict[int, list[EngineBlock]] | None = None,
        *,
        engine_id: str = "fake/1",
    ):
        self._pages = pages or {}
        self._engine_id = engine_id
        self.calls: list[int] = []

    @property
    def engine_id(self) -> str:
        return self._engine_id

    def recognise(self, page: RenderedPage) -> list[EngineBlock]:
        # RenderedPage has no page number; the service records call order instead, which
        # is what the cache tests need.
        self.calls.append(len(self.calls) + 1)
        return self._pages.get(len(self.calls), [])

    def set_page(self, number: int, blocks: list[EngineBlock]) -> None:
        self._pages[number] = blocks


class UnavailableEngine(OcrEngine):
    """Stands in when no OCR engine is installed.

    Reports unavailability rather than raising at import, so a deployment without OCR
    degrades to text-layer-only extraction with a clear reason instead of failing to start.
    """

    def __init__(self, reason: str):
        self.reason = reason

    @property
    def engine_id(self) -> str:
        return "unavailable"

    @property
    def available(self) -> bool:
        return False

    def recognise(self, page: RenderedPage) -> list[EngineBlock]:
        raise RuntimeError(f"No OCR engine available: {self.reason}")
