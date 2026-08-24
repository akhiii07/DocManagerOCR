"""Ingestion service.

The path from an uploaded byte string to a `Document` attached to a `Case`:

    bytes
      -> safety scan        (refuse active content before anything parses it)
      -> store              (content-addressed; dedupe falls out of this)
      -> structural analysis
      -> quality gate       (OK | DEGRADED | REJECTED)
      -> Document on the Case

Ordering matters. The safety scan runs on raw bytes **before** any parser touches the
file, because the parser is the thing we are protecting. Storage happens before analysis
so that a file which crashes the parser is still retained for a human to look at — losing
the evidence because we could not read it would be the wrong failure.

Classification and extraction are deliberately NOT here. Ingestion establishes that we
have a readable artefact and what condition it is in; deciding what the document *is* is a
separate stage with its own failure modes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..model.case import Case, CustodyStatus, Document, DocumentQuality
from ..model.common import DocumentType
from . import pdfinfo
from .quality import QualityReport, QualityThresholds, assess
from .sanitize import SafetyReport, SafetyVerdict, scan
from .store import ContentStore

log = logging.getLogger(__name__)


@dataclass
class IngestResult:
    """Outcome of ingesting one file."""

    accepted: bool
    document: Document | None
    safety: SafetyReport
    quality: QualityReport | None
    #: Set when identical content is already attached to this case.
    duplicate_of: str | None = None
    reason: str | None = None

    @property
    def digest(self) -> str | None:
        return self.document.sha256 if self.document else None


class IngestionService:
    def __init__(
        self,
        store: ContentStore,
        thresholds: QualityThresholds | None = None,
        *,
        allow_suspicious: bool = True,
    ):
        self.store = store
        self.thresholds = thresholds or QualityThresholds()
        #: SUSPICIOUS files (e.g. an /OpenAction) are processed by default with a note.
        #: Set False for a stricter posture.
        self.allow_suspicious = allow_suspicious

    # -- public API --------------------------------------------------------------

    def ingest_bytes(
        self,
        case: Case,
        data: bytes,
        *,
        filename: str | None = None,
        document_type: DocumentType = DocumentType.UNKNOWN,
        custody: CustodyStatus = CustodyStatus.UNKNOWN,
    ) -> IngestResult:
        safety = scan(data, declared_name=filename)

        if safety.is_blocked or (
            safety.verdict is SafetyVerdict.SUSPICIOUS and not self.allow_suspicious
        ):
            # Not stored. Refusing to persist active content is the point - keeping it
            # "just in case" would mean the risky bytes live in our object store.
            reasons = "; ".join(f.detail for f in safety.findings if f.blocking) or (
                "Upload rejected by safety policy."
            )
            log.warning("ingest blocked for case %s: %s", case.case_id, reasons)
            return IngestResult(False, None, safety, None, reason=reasons)

        digest = self.store.put(data)

        existing = next((d for d in case.documents if d.sha256 == digest), None)
        if existing is not None:
            return IngestResult(
                accepted=False,
                document=existing,
                safety=safety,
                quality=None,
                duplicate_of=existing.document_id,
                reason="Identical content already attached to this case.",
            )

        info = (
            pdfinfo.analyse_pdf(data)
            if safety.declared_type == "pdf"
            else self._analyse_image_bytes(data, digest)
        )
        report = assess(info, self.thresholds)

        doc = Document(
            case_id=case.case_id,
            tenant_id=case.tenant_id,
            document_type=document_type,
            sha256=digest,
            page_count=info.page_count or None,
            quality=report.verdict,
            quality_notes=report.notes + [f.detail for f in safety.findings],
            has_text_layer=not info.needs_ocr,
            custody=custody,
            received_at=datetime.now(),
        )
        case.add_document(doc)

        # A REJECTED document is still attached. The reviewer needs to see that a file
        # arrived and why it was unusable, rather than finding a silent gap in the bundle.
        return IngestResult(
            accepted=report.verdict is not DocumentQuality.REJECTED,
            document=doc,
            safety=safety,
            quality=report,
            reason=None if report.verdict is not DocumentQuality.REJECTED
            else "; ".join(report.notes),
        )

    def ingest_path(self, case: Case, path: str | Path, **kw) -> IngestResult:
        p = Path(path)
        kw.setdefault("filename", p.name)
        return self.ingest_bytes(case, p.read_bytes(), **kw)

    def ingest_directory(self, case: Case, directory: str | Path, **kw) -> list[IngestResult]:
        """Ingest every supported file in a directory, sorted for determinism."""
        root = Path(directory)
        exts = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
        results = []
        for p in sorted(root.iterdir()):
            if p.is_file() and p.suffix.lower() in exts:
                results.append(self.ingest_path(case, p, **kw))
        return results

    # -- helpers -----------------------------------------------------------------

    def _analyse_image_bytes(self, data: bytes, digest: str) -> pdfinfo.DocumentInfo:
        """Analyse image bytes.

        Prefers the stored blob's own path so the bytes are never written twice; falls
        back to a temporary file for stores with no local filesystem.
        """
        p = self.store.path_for(digest)
        if p is not None:
            return pdfinfo.analyse_image(p)

        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".img", delete=False) as fh:
            fh.write(data)
            tmp = Path(fh.name)
        try:
            return pdfinfo.analyse_image(tmp)
        finally:
            tmp.unlink(missing_ok=True)


def summarise_ingest(results: list[IngestResult]) -> dict[str, int]:
    """Counts for an upload response."""
    return {
        "submitted": len(results),
        "accepted": sum(1 for r in results if r.accepted),
        "blocked": sum(1 for r in results if r.safety.is_blocked),
        "duplicates": sum(1 for r in results if r.duplicate_of),
        "rejected_quality": sum(
            1 for r in results if r.quality and r.quality.is_rejected
        ),
        "degraded": sum(
            1 for r in results
            if r.quality and r.quality.verdict is DocumentQuality.DEGRADED
        ),
        "needs_ocr": sum(
            1 for r in results if r.document and not r.document.has_text_layer
        ),
    }
