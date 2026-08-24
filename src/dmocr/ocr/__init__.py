"""Text extraction: embedded text layer plus OCR, routed per page."""

from .cache import (
    CACHE_SCHEMA_VERSION,
    FileOcrCache,
    InMemoryOcrCache,
    NullOcrCache,
    OcrCache,
    cache_key,
)
from .engine import EngineBlock, FakeOcrEngine, OcrEngine, RenderedPage, UnavailableEngine
from .service import (
    DEFAULT_MIN_TEXT_CHARS,
    ExtractionStats,
    TextExtractionService,
    default_engine,
)
from .types import OcrDocument, OcrPage, TextBlock, TextSource, assemble_page

__all__ = [
    "CACHE_SCHEMA_VERSION",
    "DEFAULT_MIN_TEXT_CHARS",
    "EngineBlock",
    "ExtractionStats",
    "FakeOcrEngine",
    "FileOcrCache",
    "InMemoryOcrCache",
    "NullOcrCache",
    "OcrCache",
    "OcrDocument",
    "OcrEngine",
    "OcrPage",
    "RenderedPage",
    "TextBlock",
    "TextExtractionService",
    "TextSource",
    "UnavailableEngine",
    "assemble_page",
    "cache_key",
    "default_engine",
]
