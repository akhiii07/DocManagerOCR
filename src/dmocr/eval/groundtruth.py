"""Ground truth.

PRIVACY WARNING
---------------
A ground-truth file for real documents contains **transcribed customer data** — owner
names, consideration amounts, parcel identifiers, sometimes whole pages of reference text.
It is customer content and is subject to the same rules as the documents themselves:

* it lives **outside the repository** (gitignored, and the loader warns if it is inside)
* it never leaves the machine
* evaluation reports carry **metrics and identifiers only, never values**, unless a local
  operator explicitly asks for values while debugging

That last point shapes the whole harness. A report you cannot share is a report nobody
reads, so the default output is safe to circulate.

FORMAT
------
One YAML file per case:

    case_id: GT_001
    notes: optional free text
    documents:
      - file: bundle_sale_deed.pdf
        document_type: sale_deed
        fields:
          consideration: "12500000"          # rupees
          execution_date: "2024-03-14"
          cts_number: "1234/5A"
          area: {value: 1150, unit: sq_ft, basis: carpet}
          seller: "Ramesh Patil"
        absent_fields: [maharera_number]      # asserted NOT to be in this document
        reference_text: |                     # optional, enables CER/WER
          DEED OF SALE ...
    expected_findings:
      - {rule_id: XDOC_AREA_001, determination: MISMATCH}

`absent_fields` matters: without it a spurious extraction cannot be distinguished from an
unlabelled one, and the harness would silently ignore invented values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass
class DocumentTruth:
    file: str
    document_type: str | None = None
    #: field name -> expected value (scalar, or a mapping for structured values).
    fields: dict[str, object] = field(default_factory=dict)
    #: Fields asserted NOT to be present. Enables spurious-extraction detection.
    absent_fields: list[str] = field(default_factory=list)
    #: Full page text, for CER/WER. Optional and expensive to produce.
    reference_text: str | None = None
    notes: str | None = None

    @property
    def has_ocr_reference(self) -> bool:
        return bool(self.reference_text and self.reference_text.strip())


@dataclass
class ExpectedFinding:
    rule_id: str
    determination: str | None = None
    disposition: str | None = None


@dataclass
class CaseTruth:
    case_id: str
    documents: list[DocumentTruth] = field(default_factory=list)
    expected_findings: list[ExpectedFinding] = field(default_factory=list)
    #: Rules asserted NOT to fire adversely. Without these, false positives are invisible.
    expected_clear: list[str] = field(default_factory=list)
    notes: str | None = None
    source_path: Path | None = None
    #: Set on files describing generated fixtures. Suppresses the in-repo warning, since
    #: synthetic ground truth carries no customer data and belongs in version control.
    synthetic: bool = False

    def document(self, file: str) -> DocumentTruth | None:
        return next((d for d in self.documents if d.file == file), None)

    @property
    def labelled_field_count(self) -> int:
        return sum(len(d.fields) for d in self.documents)


def _warn_if_inside_repo(path: Path, synthetic: bool) -> None:
    if synthetic:
        return
    try:
        path.resolve().relative_to(REPO_ROOT)
    except ValueError:
        return
    log.warning(
        "ground truth at %s is inside the repository. It contains transcribed customer "
        "data and must live outside the repo - see docs/privacy/data-handling-policy.md",
        path,
    )


def load_case_truth(path: str | Path) -> CaseTruth:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    _warn_if_inside_repo(p, bool(data.get("synthetic")))

    documents = [
        DocumentTruth(
            file=d["file"],
            document_type=d.get("document_type"),
            fields=dict(d.get("fields") or {}),
            absent_fields=list(d.get("absent_fields") or []),
            reference_text=d.get("reference_text"),
            notes=d.get("notes"),
        )
        for d in (data.get("documents") or [])
    ]
    findings = [
        ExpectedFinding(
            rule_id=f["rule_id"],
            determination=f.get("determination"),
            disposition=f.get("disposition"),
        )
        for f in (data.get("expected_findings") or [])
    ]
    return CaseTruth(
        case_id=data.get("case_id", p.stem),
        documents=documents,
        expected_findings=findings,
        expected_clear=list(data.get("expected_clear") or []),
        notes=data.get("notes"),
        source_path=p,
        synthetic=bool(data.get("synthetic")),
    )


def load_corpus(directory: str | Path) -> list[CaseTruth]:
    """Load every ground-truth file in a directory, sorted for determinism."""
    root = Path(directory)
    if not root.is_dir():
        return []
    out: list[CaseTruth] = []
    for p in sorted(root.glob("*.y*ml")):
        try:
            out.append(load_case_truth(p))
        except Exception as exc:  # a bad label file must not stop the run
            log.error("could not load ground truth %s: %s", p, exc)
    return out


def coverage_summary(corpus: list[CaseTruth]) -> dict:
    """What the labelled corpus actually covers.

    Reported at the top of every evaluation, because a metric computed over three
    documents means something very different from the same metric over three hundred, and
    the number is easy to lose sight of.
    """
    docs = [d for c in corpus for d in c.documents]
    types: dict[str, int] = {}
    for d in docs:
        if d.document_type:
            types[d.document_type] = types.get(d.document_type, 0) + 1
    return {
        "cases": len(corpus),
        "documents": len(docs),
        "labelled_fields": sum(len(d.fields) for d in docs),
        "documents_with_ocr_reference": sum(1 for d in docs if d.has_ocr_reference),
        "document_types": types,
        "expected_findings": sum(len(c.expected_findings) for c in corpus),
    }
