"""Review session: the per-box state machine behind the UI.

The user's design is a chain — upload, check the type, extract, confirm, analyse — and the
instinct to gate each step on the previous one is right in exactly ONE place and wrong
everywhere else.

**Where gating is right.** If the classifier confidently says the file in the "Sale Deed"
box is a Property Tax bill, extracting it with the Sale Deed schema produces a full set of
plausible, wrong fields. That is the failure this whole platform exists to prevent, so the
document is HELD and the user is asked to confirm or move it.

**Where gating is wrong.** Everywhere else. The most valuable checks are case-level and
need several documents: the area conflict between a deed and a tax bill cannot be seen from
either alone. If an uncertain classification blocked the pipeline, that conflict would never
surface. So stages advance and each reports its own status, and downstream work runs on
whatever is available while saying what it could not do.

A third outcome matters too: the classifier DEFERS by design. A scanned document is
`UNKNOWN` until OCR has run, and `UNKNOWN` is a correct answer, not "wrong". The user's box
choice is itself a human classification, which outranks the classifier's.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum

from ..classify import ClassificationResult, RuleClassifier, UnknownReason
from ..extract import ExtractionResult, ExtractionService
from ..ingest import IngestionService, InMemoryContentStore
from ..model.case import Case, Document, DocumentQuality, LenderType, Product
from ..model.common import ConfidenceTier, DocumentType
from ..model.findings import Finding
from ..model.provenance import ProcessingContext
from ..ocr import InMemoryOcrCache, OcrDocument, TextExtractionService, default_engine
from ..resolve import CaseAssembler
from ..rules import ExecutionMode, RuleEngine, RuleSet, summarise
from ..verify import VerificationOrchestrator, VerificationRun
from .feedback import (
    CorrectionError,
    FeedbackAction,
    FeedbackLog,
    FieldFeedback,
    parse_correction,
)

log = logging.getLogger(__name__)


class BoxStatus(StrEnum):
    EMPTY = "empty"
    PROCESSING = "processing"
    OK = "ok"
    #: Processed, but something needs a human. The common case, not an error state.
    ATTENTION = "attention"
    #: Waiting for the user to confirm or move a document whose type is disputed.
    NEEDS_CONFIRMATION = "needs_confirmation"
    #: Genuinely unusable - encrypted, unreadable, or refused at ingest.
    BLOCKED = "blocked"


class StageStatus(StrEnum):
    OK = "ok"
    ATTENTION = "attention"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


#: The boxes shown on the board.
#:
#: PROPERTY_PAPERS is deliberately absent. It has no classifier signals and no extraction
#: schema - it is a catch-all label for a bundle, not a recognisable document - so a box
#: named that would fail its own "is this the right document?" check every time. Anything
#: that does not belong in a named box goes to the unvalidated "Other" tray.
BOXES: list[tuple[DocumentType, str, bool]] = [
    (DocumentType.AGREEMENT_OF_SALE, "Agreement of Sale", True),
    (DocumentType.SALE_DEED, "Sale Deed", True),
    (DocumentType.PROPERTY_TAX, "Property Tax", True),
    (DocumentType.POSSESSION_DOCUMENT, "Possession Document", False),
]

OTHER_BOX = "other"


@dataclass
class StageResult:
    key: str
    label: str
    status: StageStatus
    detail: str = ""


@dataclass
class FieldView:
    name: str
    label: str
    value: str
    confidence: str
    page: int
    #: Present when the value can be shown on the page.
    evidence: str | None = None
    notes: list[str] = field(default_factory=list)
    #: None until a reviewer decides: "accepted" or "corrected".
    feedback: str | None = None
    #: What the system originally produced, kept visible after a correction so the
    #: reviewer can see what they changed.
    original_value: str | None = None


@dataclass
class BoxView:
    key: str
    label: str
    required: bool
    status: BoxStatus = BoxStatus.EMPTY
    document_id: str | None = None
    filename: str | None = None
    stages: list[StageResult] = field(default_factory=list)
    fields: list[FieldView] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    #: Set when the classifier confidently disagrees with the box.
    suggested_type: str | None = None
    suggested_label: str | None = None


@dataclass
class DocumentContext:
    """Everything retained about one uploaded document."""

    document_id: str
    box_key: str
    filename: str
    data: bytes
    document: Document | None = None
    ocr: OcrDocument | None = None
    classification: ClassificationResult | None = None
    extraction: ExtractionResult | None = None
    stages: list[StageResult] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    status: BoxStatus = BoxStatus.PROCESSING
    suggested_type: DocumentType | None = None
    #: field name -> reviewer-corrected value. Applied on every recompute rather than
    #: written into the extraction, so re-running the pipeline never silently discards a
    #: human's correction.
    corrections: dict = field(default_factory=dict)

    #: Everything the System view needs to explain what actually happened. Kept here
    #: rather than recomputed, because a trace of a past run must not change when the
    #: code does.
    quality_metrics: dict = field(default_factory=dict)
    ocr_stats: dict = field(default_factory=dict)
    classification_detail: dict = field(default_factory=dict)


_CONFIDENCE_LABEL = {
    ConfidenceTier.HIGH: "high",
    ConfidenceTier.MEDIUM: "medium",
    ConfidenceTier.LOW: "low",
    ConfidenceTier.INSUFFICIENT: "insufficient",
}


def box_label(key: str) -> str:
    for doc_type, label, _ in BOXES:
        if doc_type.value == key:
            return label
    return "Other documents" if key == OTHER_BOX else key


class ReviewSession:
    """One case, held in memory.

    Single-case and in-process on purpose: the MVP has no authentication (ADR-0002), so
    the server binds to localhost and there is nothing to isolate between users yet. The
    case_id and tenant_id are still carried through, so adding real sessions later is a
    storage change rather than a redesign.
    """

    def __init__(self, rules_path: str = "rules/mvp.yaml"):
        self._lock = threading.Lock()
        self.store = InMemoryContentStore()
        self.ingestion = IngestionService(self.store)
        self.text = TextExtractionService(default_engine(), InMemoryOcrCache())
        self.classifier = RuleClassifier()
        self.extractor = ExtractionService()
        self.assembler = CaseAssembler()
        # No adapters registered: CERSAI is the only automatable source and it is blocked
        # on whether the lender holds an entity account (OPEN-ITEMS 7). The orchestrator
        # still produces the plan and the operator tasks, which is the honest picture and
        # what the System view shows.
        self.verifier = VerificationOrchestrator()
        self.verification: VerificationRun | None = None

        self.rule_set: RuleSet | None = None
        try:
            self.rule_set = RuleSet.from_yaml(rules_path)
        except Exception as exc:  # pragma: no cover
            log.error("could not load rules: %s", exc)

        self.documents: dict[str, DocumentContext] = {}
        self.findings: list[Finding] = []
        self.finding_summary: dict = {}
        self.case_notes: list[str] = []
        #: Reviewer decisions. Also the calibration dataset - see feedback.py.
        self.feedback = FeedbackLog()
        self._new_case()

    # -- case --------------------------------------------------------------------

    def _new_case(self) -> None:
        self.case = Case(
            tenant_id="local",
            lender_type=LenderType.HFC,
            product=Product.HOUSING_LOAN,
            expected_documents=[t for t, _, required in BOXES if required],
        )
        self.case.processing_context = ProcessingContext(
            pipeline_version="0.1.0",
            rule_set_version=self.rule_set.version if self.rule_set else "none",
            model_versions={"ocr": self.text.engine.engine_id},
            regulatory_as_of=date.today(),
            processed_at=datetime.now(),
        )

    def reset(self) -> None:
        with self._lock:
            self.documents.clear()
            self.findings.clear()
            self.finding_summary = {}
            self.case_notes.clear()
            self.feedback.clear()
            self._new_case()

    # -- reviewer feedback -------------------------------------------------------

    def accept_field(self, document_id: str, field_name: str) -> None:
        """Reviewer confirms the extracted value is right."""
        found = self._find_field(document_id, field_name)
        if found is None:
            return
        ctx, extracted = found
        self.feedback.record(FieldFeedback(
            document_id=document_id,
            field_name=field_name,
            action=FeedbackAction.ACCEPTED,
            original_value=_display_value(extracted),
            original_confidence=_CONFIDENCE_LABEL.get(extracted.confidence, "unknown"),
        ))
        # No recompute needed: accepting does not change the value, only its standing.

    def correct_field(self, document_id: str, field_name: str, text: str) -> None:
        """Reviewer replaces the extracted value.

        Raises CorrectionError if the text cannot be read as the right kind of value -
        the UI shows that back rather than storing a guess.
        """
        found = self._find_field(document_id, field_name)
        if found is None:
            raise CorrectionError("That field is no longer on the case.")
        ctx, extracted = found

        # Raises before anything is recorded, so a rejected correction leaves no trace.
        new_value = parse_correction(extracted.value, text)

        self.feedback.record(FieldFeedback(
            document_id=document_id,
            field_name=field_name,
            action=FeedbackAction.CORRECTED,
            original_value=_display_value(extracted),
            original_confidence=_CONFIDENCE_LABEL.get(extracted.confidence, "unknown"),
            corrected_value=text.strip(),
        ))
        ctx.corrections[field_name] = new_value
        # A correction changes the facts, so cross-document checks must run again.
        self._recompute_case()

    def _find_field(self, document_id: str, field_name: str):
        ctx = self.documents.get(document_id)
        if ctx is None or ctx.extraction is None:
            return None
        extracted = next(
            (f for f in ctx.extraction.fields if f.field_name == field_name), None)
        return (ctx, extracted) if extracted is not None else None

    # -- upload ------------------------------------------------------------------

    def accept_upload(self, box_key: str, filename: str, data: bytes) -> str:
        """Register an upload and return its id. Processing happens separately."""
        document_id = f"UP_{uuid.uuid4().hex[:10]}"
        with self._lock:
            # One document per box. Replacing clears the previous one so the board never
            # shows two answers for the same slot.
            for existing in [d for d in self.documents.values() if d.box_key == box_key]:
                if box_key != OTHER_BOX:
                    self.documents.pop(existing.document_id, None)
            self.documents[document_id] = DocumentContext(
                document_id=document_id, box_key=box_key,
                filename=filename, data=data,
            )
        return document_id

    def process(self, document_id: str) -> None:
        """Run ingest -> text -> classify -> (gate) -> extract for one upload."""
        ctx = self.documents.get(document_id)
        if ctx is None:
            return
        try:
            self._process(ctx)
        except Exception as exc:  # noqa: BLE001 - a crash must not leave a stuck spinner
            log.exception("processing failed for %s", document_id)
            ctx.status = BoxStatus.BLOCKED
            ctx.issues.append(f"Processing failed: {type(exc).__name__}: {exc}")
            ctx.stages.append(StageResult(
                "error", "Processing", StageStatus.BLOCKED, str(exc)))
        finally:
            self._recompute_case()

    def _process(self, ctx: DocumentContext) -> None:
        # -- 1. ingest -----------------------------------------------------------
        result = self.ingestion.ingest_bytes(
            self.case, ctx.data, filename=ctx.filename)
        if result.document is None:
            ctx.status = BoxStatus.BLOCKED
            ctx.issues.append(result.reason or "Upload refused.")
            ctx.stages.append(StageResult(
                "ingest", "File check", StageStatus.BLOCKED,
                result.reason or "Upload refused."))
            return

        ctx.document = result.document
        quality = result.document.quality
        if quality is DocumentQuality.REJECTED:
            ctx.status = BoxStatus.BLOCKED
            ctx.issues.append(result.reason or "Unreadable.")
            ctx.stages.append(StageResult(
                "ingest", "File check", StageStatus.BLOCKED, result.reason or ""))
            return

        ctx.stages.append(StageResult(
            "ingest", "File check",
            StageStatus.ATTENTION if quality is DocumentQuality.DEGRADED else StageStatus.OK,
            "Scan quality is poor; confidence is capped."
            if quality is DocumentQuality.DEGRADED else "Readable.",
        ))
        ctx.quality_metrics = {
            "verdict": quality.value,
            "sha256": result.document.sha256[:16],
            "page_count": result.document.page_count,
            "has_text_layer": result.document.has_text_layer,
            "notes": list(result.document.quality_notes),
            "safety": result.safety.verdict.value,
            "safety_findings": [f.code for f in result.safety.findings],
        }

        # -- 2. text -------------------------------------------------------------
        ocr_doc, stats = self.text.extract(ctx.data, result.document.sha256)
        ctx.ocr = ocr_doc
        if stats.ocr_pages:
            detail = f"{stats.ocr_pages} page(s) read by OCR"
            if stats.text_layer_pages:
                detail += f", {stats.text_layer_pages} from the embedded text layer"
        else:
            detail = f"{stats.text_layer_pages} page(s) read from the embedded text layer"
        ctx.stages.append(StageResult(
            "text", "Reading", StageStatus.OK if not stats.failures else StageStatus.ATTENTION,
            detail + ("; " + "; ".join(stats.failures[:2]) if stats.failures else ""),
        ))
        ctx.ocr_stats = {
            "ocr_pages": stats.ocr_pages,
            "text_layer_pages": stats.text_layer_pages,
            "empty_pages": stats.empty_pages,
            "cache_hits": stats.cache_hits,
            "failures": list(stats.failures),
            "mean_confidence": ocr_doc.mean_confidence,
            "page_count": ocr_doc.page_count,
            "sources": ocr_doc.sources,
            "engine": self.text.engine.engine_id,
        }

        # -- 3. the box check ----------------------------------------------------
        classification = self.classifier.classify(ocr_doc.page_texts(), quality=quality)
        ctx.classification = classification
        expected = _box_type(ctx.box_key)

        stage, detail, suggested = _check_box(expected, classification)
        ctx.suggested_type = suggested
        ctx.stages.append(StageResult("classify", "Document type", stage, detail))
        ctx.classification_detail = {
            "predicted": classification.document_type.value,
            "confidence": classification.confidence.value,
            "score": round(classification.score, 2),
            "runner_up": (classification.runner_up.value
                          if classification.runner_up else None),
            "unknown_reason": (classification.unknown_reason.value
                               if classification.unknown_reason else None),
            # All candidate scores, so a misfire can be diagnosed rather than guessed at.
            "scores": {k.value: v for k, v in classification.scores.items()},
            "signals": [
                {"name": h.signal_name, "page": h.page,
                 "text": h.matched_text[:60], "weight": h.contribution}
                for h in classification.hits[:12]
            ],
        }

        if stage is StageStatus.BLOCKED:
            # The one place gating is correct: a confident type mismatch would otherwise
            # extract with the wrong schema and produce plausible, wrong fields.
            ctx.status = BoxStatus.NEEDS_CONFIRMATION
            ctx.issues.append(detail)
            return

        if stage is StageStatus.ATTENTION:
            ctx.issues.append(detail)

        # -- 4. extract ----------------------------------------------------------
        # Extract using the BOX type. The user placing a file in a named box is a human
        # classification, and a human's classification outranks the classifier's.
        self._extract(ctx, expected or classification.document_type)

    def _extract(self, ctx: DocumentContext, document_type: DocumentType) -> None:
        if ctx.document is None or ctx.ocr is None:
            return
        if document_type is DocumentType.UNKNOWN:
            ctx.stages.append(StageResult(
                "extract", "Data extraction", StageStatus.SKIPPED,
                "No document type established, so no schema applies."))
            ctx.status = BoxStatus.ATTENTION
            return

        ctx.document.document_type = document_type
        extraction = self.extractor.extract(
            ctx.ocr, document_id=ctx.document.document_id, document_type=document_type)
        ctx.extraction = extraction

        if extraction.missing_required:
            names = ", ".join(extraction.missing_required)
            ctx.stages.append(StageResult(
                "extract", "Data extraction", StageStatus.ATTENTION,
                f"{extraction.extracted_count} field(s) found; could not find: {names}."))
            ctx.issues.append(f"Could not find: {names}")
        elif not extraction.fields:
            ctx.stages.append(StageResult(
                "extract", "Data extraction", StageStatus.ATTENTION,
                "No fields could be read from this document."))
            ctx.issues.append("No fields could be read.")
        else:
            ctx.stages.append(StageResult(
                "extract", "Data extraction", StageStatus.OK,
                f"{extraction.extracted_count} field(s) found."))

        if extraction.rejected_ungrounded:
            ctx.issues.append(
                f"{len(extraction.rejected_ungrounded)} value(s) were discarded because "
                f"they could not be located on the page.")

        ctx.status = BoxStatus.ATTENTION if ctx.issues else BoxStatus.OK

    # -- user decisions ----------------------------------------------------------

    def confirm_type(self, document_id: str) -> None:
        """User asserts the document really does belong in the box it was put in."""
        ctx = self.documents.get(document_id)
        if ctx is None or ctx.status is not BoxStatus.NEEDS_CONFIRMATION:
            return
        expected = _box_type(ctx.box_key)
        ctx.issues.append(
            f"Type confirmed by reviewer despite the system reading it as "
            f"{ctx.suggested_type.value if ctx.suggested_type else 'something else'}."
        )
        ctx.stages.append(StageResult(
            "confirm", "Type confirmed", StageStatus.ATTENTION,
            "Confirmed by reviewer. The disagreement is recorded."))
        self._extract(ctx, expected or DocumentType.UNKNOWN)
        self._recompute_case()

    def move_document(self, document_id: str, target_box: str) -> None:
        """Move a document to the box the system thinks it belongs in."""
        ctx = self.documents.get(document_id)
        if ctx is None:
            return
        with self._lock:
            for existing in [d for d in self.documents.values()
                             if d.box_key == target_box and d.document_id != document_id]:
                self.documents.pop(existing.document_id, None)
        ctx.box_key = target_box
        ctx.status = BoxStatus.PROCESSING
        ctx.stages.clear()
        ctx.issues.clear()
        ctx.suggested_type = None
        self.process(document_id)

    def remove(self, document_id: str) -> None:
        with self._lock:
            ctx = self.documents.pop(document_id, None)
        if ctx and ctx.document is not None:
            self.case.documents = [
                d for d in self.case.documents
                if d.document_id != ctx.document.document_id]
        self._recompute_case()

    # -- case level --------------------------------------------------------------

    def _recompute_case(self) -> None:
        """Re-assemble and re-run rules over whatever documents are usable.

        Runs after every change, on whatever is available. Waiting for a complete bundle
        would hide the cross-document conflicts that are the most valuable thing here.
        """
        extractions = {
            ctx.document.document_id: _with_corrections(ctx)
            for ctx in self.documents.values()
            if ctx.document is not None and ctx.extraction is not None
        }
        self.case.properties.clear()
        self.case.parties.clear()

        assembly = self.assembler.assemble(self.case, extractions)
        self.case_notes = list(assembly.notes)
        for decision in assembly.decisions:
            self.case_notes.append(str(decision))

        # Runs AFTER assembly, because the planner needs resolved canonical values to know
        # what to look up. Writes results onto the case so verification-aware rules see
        # them in the same pass.
        if self.case.properties:
            self.verification = self.verifier.run(self.case)

        if self.rule_set is None:
            self.findings = []
            self.finding_summary = {}
            return

        # DRY_RUN because no rule is APPROVED. In ENFORCE mode this board would show zero
        # findings and look broken; every finding is labelled advisory in the UI instead.
        self.findings = RuleEngine(self.rule_set).evaluate(
            self.case, mode=ExecutionMode.DRY_RUN)
        self.finding_summary = summarise(self.findings)

    # -- views -------------------------------------------------------------------

    def boxes(self) -> list[BoxView]:
        views: list[BoxView] = []
        for doc_type, label, required in BOXES:
            views.append(self._box_view(doc_type.value, label, required))
        views.append(self._box_view(OTHER_BOX, "Other documents", False))
        return views

    def _box_view(self, key: str, label: str, required: bool) -> BoxView:
        ctx = next((d for d in self.documents.values() if d.box_key == key), None)
        if ctx is None:
            return BoxView(key=key, label=label, required=required)
        return BoxView(
            key=key, label=label, required=required, status=ctx.status,
            document_id=ctx.document_id, filename=ctx.filename,
            stages=list(ctx.stages), fields=self._fields(ctx), issues=list(ctx.issues),
            suggested_type=ctx.suggested_type.value if ctx.suggested_type else None,
            suggested_label=(box_label(ctx.suggested_type.value)
                             if ctx.suggested_type else None),
        )

    def _fields(self, ctx: DocumentContext) -> list[FieldView]:
        if ctx.extraction is None:
            return []
        out: list[FieldView] = []
        for f in ctx.extraction.fields:
            prov = f.provenance
            has_box = getattr(prov, "bbox", None) is not None
            decision = self.feedback.get(ctx.document_id, f.field_name)
            corrected = ctx.corrections.get(f.field_name)

            out.append(FieldView(
                name=f.field_name,
                label=f.field_name.replace("_", " ").title(),
                # Show the corrected value where there is one - the reviewer's answer is
                # the one the case now runs on.
                value=(_display_claim_value(corrected) if corrected is not None
                       else _display_value(f)),
                confidence=("confirmed" if corrected is not None
                            else _CONFIDENCE_LABEL.get(f.confidence, "unknown")),
                page=prov.page,
                # Evidence still points at the ORIGINAL region, so a reviewer can check
                # what the system read even after overriding it.
                evidence=(f"/evidence/{ctx.document_id}/{prov.page}"
                          f"?x0={prov.bbox.x0}&y0={prov.bbox.y0}"
                          f"&x1={prov.bbox.x1}&y1={prov.bbox.y1}") if has_box else None,
                notes=list(f.notes),
                feedback=decision.action.value if decision else None,
                original_value=(_display_value(f) if corrected is not None else None),
            ))
        return out

    def context(self, document_id: str) -> DocumentContext | None:
        return self.documents.get(document_id)


# =====================================================================================
# Helpers
# =====================================================================================


def _box_type(box_key: str) -> DocumentType | None:
    if box_key == OTHER_BOX:
        return None
    try:
        return DocumentType(box_key)
    except ValueError:
        return None


def _check_box(
    expected: DocumentType | None, classification: ClassificationResult
) -> tuple[StageStatus, str, DocumentType | None]:
    """Three outcomes, not two.

    A mismatch is never BLOCKED outright at the file level - the user may well be right,
    and a human classification outranks the classifier's. It is held for confirmation.
    """
    got = classification.document_type

    if expected is None:  # the "Other" tray validates nothing
        if got is DocumentType.UNKNOWN:
            return (StageStatus.ATTENTION,
                    "Could not identify this document type.", None)
        return (StageStatus.OK, f"Read as {box_label(got.value)}.", None)

    if got == expected:
        return (StageStatus.OK,
                f"Confirmed as {box_label(expected.value)} "
                f"({classification.confidence.value.lower()} confidence).", None)

    if got is DocumentType.UNKNOWN:
        reason = {
            UnknownReason.NO_TEXT: (
                "No readable text was found, so the type could not be checked. If this "
                "is a scan, it may need a better quality copy."),
            UnknownReason.AMBIGUOUS: (
                "This document matches more than one type closely, so it was not "
                "guessed."),
            UnknownReason.WEAK: (
                "Nothing in this document clearly identifies its type."),
        }.get(classification.unknown_reason, "The document type could not be determined.")
        return (StageStatus.ATTENTION,
                f"{reason} Proceeding on your selection of "
                f"{box_label(expected.value)}.", None)

    return (
        StageStatus.BLOCKED,
        f"This looks like a {box_label(got.value)}, not a {box_label(expected.value)}. "
        f"Extracting it as a {box_label(expected.value)} would produce wrong values, so "
        f"nothing further was read.",
        got,
    )


def _with_corrections(ctx: DocumentContext):
    """The document's extraction with reviewer corrections applied.

    A correction becomes a field carrying `HumanProvenance`, so downstream it is a claim
    asserted by a person rather than read from the page. Two consequences that matter:
    the claim is legitimately ungrounded (ADR-0004 constrains the MODEL, not the reviewer),
    and the audit shows who said it.
    """
    from dataclasses import replace

    if ctx.extraction is None or not ctx.corrections:
        return ctx.extraction

    from ..model.common import ConfidenceTier as _Tier
    from ..model.provenance import HumanProvenance

    fields = []
    for f in ctx.extraction.fields:
        new_value = ctx.corrections.get(f.field_name)
        if new_value is None:
            fields.append(f)
            continue
        fields.append(replace(
            f,
            value=new_value,
            provenance=HumanProvenance(
                actor="local-operator",
                asserted_at=datetime.now(),
                rationale=f"Reviewer corrected {f.field_name}.",
            ),
            confidence=_Tier.HIGH,
            notes=[*f.notes, "Value corrected by the reviewer."],
        ))
    return replace(ctx.extraction, fields=fields)


def _display_claim_value(v) -> str:
    from ..model.claims import AreaValue, DateValue, MoneyValue, ParcelValue, TextValue

    if isinstance(v, MoneyValue):
        return str(v.amount)
    if isinstance(v, AreaValue):
        basis = "" if v.basis == "unspecified" else f" ({v.basis.replace('_', ' ')})"
        return f"{v.area}{basis}"
    if isinstance(v, DateValue):
        return v.value.isoformat()
    if isinstance(v, ParcelValue):
        return f"{v.identifier.id_type.value.upper()} {v.identifier.value}"
    if isinstance(v, TextValue):
        return v.raw
    return str(v.comparable())


def _display_value(f) -> str:
    from ..model.claims import AreaValue, DateValue, MoneyValue, ParcelValue, TextValue

    v = f.value
    if isinstance(v, MoneyValue):
        return str(v.amount)
    if isinstance(v, AreaValue):
        basis = "" if v.basis == "unspecified" else f" ({v.basis.replace('_', ' ')})"
        return f"{v.area}{basis}"
    if isinstance(v, DateValue):
        return v.value.isoformat()
    if isinstance(v, ParcelValue):
        return f"{v.identifier.id_type.value.upper()} {v.identifier.value}"
    if isinstance(v, TextValue):
        return v.raw
    return str(v.comparable())
