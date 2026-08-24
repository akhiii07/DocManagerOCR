"""End-to-end case pipeline.

    files -> ingest -> text extraction -> classify -> extract -> assemble -> rules

Deliberately linear and explicit. Reproducibility is a hard requirement, so there is no
model deciding which stage runs next: the same inputs and the same pinned versions must
produce the same findings.

Every stage degrades rather than aborting. A rejected document, an unclassifiable one, or
one with no schema all leave the case processable, and each is reported so the reviewer
sees what was skipped and why. A gap in the bundle must never be silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .classify import ClassifierConfig, RuleClassifier, apply_to_document
from .extract import ExtractionResult, ExtractionService
from .ingest import (
    ContentStore,
    IngestionService,
    IngestResult,
    QualityThresholds,
    summarise_ingest,
)
from .model.case import Case, Document, DocumentQuality
from .model.common import DocumentType
from .model.findings import Finding
from .model.provenance import ProcessingContext
from .ocr import (
    NullOcrCache,
    OcrCache,
    OcrDocument,
    OcrEngine,
    TextExtractionService,
    default_engine,
)
from .resolve import AssemblyResult, CaseAssembler
from .rules import ExecutionMode, RuleEngine, RuleSet, summarise

log = logging.getLogger(__name__)

PIPELINE_VERSION = "0.1.0"


@dataclass
class DocumentOutcome:
    """What happened to one document."""

    document_id: str
    filename: str | None = None
    ingested: bool = False
    quality: DocumentQuality | None = None
    document_type: DocumentType = DocumentType.UNKNOWN
    classification_note: str = ""
    ocr_pages: int = 0
    text_layer_pages: int = 0
    fields_extracted: int = 0
    missing_required: list[str] = field(default_factory=list)
    skipped_reason: str | None = None

    @property
    def needs_human(self) -> bool:
        return (
            self.skipped_reason is not None
            or self.document_type is DocumentType.UNKNOWN
            or bool(self.missing_required)
        )


@dataclass
class CaseResult:
    case: Case
    documents: list[DocumentOutcome] = field(default_factory=list)
    assembly: AssemblyResult | None = None
    findings: list[Finding] = field(default_factory=list)
    ingest_summary: dict = field(default_factory=dict)
    finding_summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def documents_needing_human(self) -> list[DocumentOutcome]:
        return [d for d in self.documents if d.needs_human]


class CasePipeline:
    def __init__(
        self,
        store: ContentStore,
        *,
        ocr_engine: OcrEngine | None = None,
        ocr_cache: OcrCache | None = None,
        quality_thresholds: QualityThresholds | None = None,
        classifier_config: ClassifierConfig | None = None,
        rule_set: RuleSet | None = None,
        rule_mode: ExecutionMode = ExecutionMode.ENFORCE,
    ):
        self.ingestion = IngestionService(store, quality_thresholds)
        self.text = TextExtractionService(
            ocr_engine if ocr_engine is not None else default_engine(),
            ocr_cache if ocr_cache is not None else NullOcrCache(),
        )
        self.classifier = RuleClassifier(classifier_config)
        self.extractor = ExtractionService()
        self.assembler = CaseAssembler()
        self.rule_set = rule_set
        self.rule_mode = rule_mode

    # -- public API --------------------------------------------------------------

    def process(
        self,
        case: Case,
        files: list[tuple[str, bytes]],
        *,
        regulatory_as_of: date | None = None,
    ) -> CaseResult:
        """Run the whole pipeline over a set of (filename, bytes) uploads."""
        result = CaseResult(case=case)

        case.processing_context = ProcessingContext(
            pipeline_version=PIPELINE_VERSION,
            rule_set_version=self.rule_set.version if self.rule_set else "none",
            model_versions={"ocr": self.text.engine.engine_id},
            regulatory_as_of=regulatory_as_of or date.today(),
            processed_at=datetime.now(),
        )

        ingest_results: list[IngestResult] = []
        extractions: dict[str, ExtractionResult] = {}

        for filename, data in files:
            ingested = self.ingestion.ingest_bytes(case, data, filename=filename)
            ingest_results.append(ingested)
            outcome = self._process_document(ingested, filename, data, extractions)
            result.documents.append(outcome)

        result.ingest_summary = summarise_ingest(ingest_results)
        result.assembly = self.assembler.assemble(case, extractions)
        result.notes.extend(result.assembly.notes)

        if self.rule_set is not None:
            engine = RuleEngine(self.rule_set)
            result.findings = engine.evaluate(case, mode=self.rule_mode)
            result.finding_summary = summarise(result.findings)
            if self.rule_mode is ExecutionMode.ENFORCE and not result.findings:
                result.notes.append(
                    "No rules are APPROVED, so nothing was evaluated. Rules ship "
                    "disabled until legal sign-off; use DRY_RUN to evaluate drafts."
                )
        else:
            result.notes.append("No rule set configured; no checks were run.")

        return result

    def process_directory(self, case: Case, directory: str | Path, **kw) -> CaseResult:
        root = Path(directory)
        exts = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}
        files = [
            (p.name, p.read_bytes())
            for p in sorted(root.iterdir())
            if p.is_file() and p.suffix.lower() in exts
        ]
        return self.process(case, files, **kw)

    # -- per document ------------------------------------------------------------

    def _process_document(
        self,
        ingested: IngestResult,
        filename: str,
        data: bytes,
        extractions: dict[str, ExtractionResult],
    ) -> DocumentOutcome:
        if ingested.document is None:
            return DocumentOutcome(
                document_id="-", filename=filename, ingested=False,
                skipped_reason=ingested.reason or "Rejected at ingest.",
            )

        doc: Document = ingested.document
        outcome = DocumentOutcome(
            document_id=doc.document_id,
            filename=filename,
            ingested=ingested.accepted,
            quality=doc.quality,
            document_type=doc.document_type,
        )

        if ingested.duplicate_of:
            outcome.skipped_reason = "Duplicate of a document already on the case."
            return outcome
        if doc.quality is DocumentQuality.REJECTED:
            outcome.skipped_reason = ingested.reason or "Rejected by the quality gate."
            return outcome

        ocr_doc, stats = self.text.extract(data, doc.sha256)
        outcome.ocr_pages = stats.ocr_pages
        outcome.text_layer_pages = stats.text_layer_pages
        if stats.failures:
            outcome.classification_note = "; ".join(stats.failures[:3])

        classification = self.classifier.classify(
            ocr_doc.page_texts(), quality=doc.quality
        )
        apply_to_document(doc, classification)
        outcome.document_type = doc.document_type
        if classification.note:
            outcome.classification_note = classification.note

        if doc.document_type is DocumentType.UNKNOWN:
            outcome.skipped_reason = (
                f"Not classified ({classification.unknown_reason}). Routed to human "
                f"review rather than parsed with a guessed schema."
            )
            return outcome

        extraction = self._extract(ocr_doc, doc)
        if extraction is None:
            outcome.skipped_reason = (
                f"No extraction schema for {doc.document_type.value}."
            )
            return outcome

        extractions[doc.document_id] = extraction
        outcome.fields_extracted = extraction.extracted_count
        outcome.missing_required = list(extraction.missing_required)
        return outcome

    def _extract(self, ocr_doc: OcrDocument, doc: Document) -> ExtractionResult | None:
        result = self.extractor.extract(
            ocr_doc, document_id=doc.document_id, document_type=doc.document_type
        )
        if not result.fields and any("No extraction schema" in n for n in result.notes):
            return None
        return result


def render_summary(result: CaseResult) -> str:
    """A compact, reviewer-first text summary. What needs attention, not what was parsed."""
    lines: list[str] = []
    add = lines.append

    add("=" * 60)
    add("COLLATERAL DOCUMENT REVIEW")
    add("=" * 60)
    add("")
    add(f"Case: {result.case.case_id}")
    add(f"Lender: {result.case.lender_type.value}  Product: {result.case.product.value}")
    add(f"Jurisdiction: {result.case.city}, {result.case.state}")
    add("")

    add("DOCUMENTS")
    add("-" * 60)
    for d in result.documents:
        status = "OK" if not d.needs_human else "ATTENTION"
        add(f"  [{status:9s}] {d.document_type.value:22s} "
            f"{d.fields_extracted:2d} fields  {d.filename or d.document_id}")
        if d.skipped_reason:
            add(f"              -> {d.skipped_reason}")
        if d.missing_required:
            add(f"              -> missing: {', '.join(d.missing_required)}")
    add("")

    if result.assembly:
        a = result.assembly
        add("ENTITY RESOLUTION")
        add("-" * 60)
        add(f"  Claims attached: {a.claims_added}")
        add(f"  Parties resolved: {len(a.parties)}")
        for decision in a.decisions:
            add(f"    {decision}")
        if a.needs_identity_review:
            add("    ** Identity uncertain on at least one party - review required.")
        add("")

    if result.findings:
        add("FINDINGS")
        add("-" * 60)
        for f in result.findings:
            if f.disposition.value in ("CLEARED", "NOT_APPLICABLE"):
                continue
            tag = "[advisory]" if f.advisory_only else ""
            add(f"  {f.disposition.value:16s} {f.severity.value:8s} {f.title} {tag}")
            add(f"       {f.message}")
        add("")
        add(f"  {result.finding_summary}")
        add("")

    if result.notes:
        add("NOTES")
        add("-" * 60)
        for n in result.notes:
            add(f"  - {n}")
    return "\n".join(lines)
