"""Ingestion: upload -> safety scan -> store -> analysis -> quality gate -> Document."""

from .pdfinfo import DocumentInfo, PageInfo, TextLayer, analyse, analyse_image, analyse_pdf
from .quality import QualityCode, QualityFinding, QualityReport, QualityThresholds, assess
from .sanitize import SafetyFinding, SafetyReport, SafetyVerdict, scan, sniff_type
from .service import IngestionService, IngestResult, summarise_ingest
from .store import ContentStore, InMemoryContentStore, LocalContentStore, sha256_hex

__all__ = [
    "ContentStore",
    "DocumentInfo",
    "InMemoryContentStore",
    "IngestResult",
    "IngestionService",
    "LocalContentStore",
    "PageInfo",
    "QualityCode",
    "QualityFinding",
    "QualityReport",
    "QualityThresholds",
    "SafetyFinding",
    "SafetyReport",
    "SafetyVerdict",
    "TextLayer",
    "analyse",
    "analyse_image",
    "analyse_pdf",
    "assess",
    "scan",
    "sha256_hex",
    "sniff_type",
    "summarise_ingest",
]
