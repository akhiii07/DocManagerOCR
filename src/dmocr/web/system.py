"""Data for the System view.

The Review board answers "what does a Risk Manager need to do about this case?". This
answers a different question: **"what is the system actually doing, and what is it not
doing yet?"**

That distinction matters because the honest answers are frequently negative — 3 of 21
requirements are blocked on documents we do not hold, no rule is approved, only one
verification source is automatable, and there are no accuracy numbers on real documents.
Those facts live in YAML and Markdown today, which means they are invisible while you
work. Surfacing them is the point.

Everything here is read-only and derived. Nothing in this module changes a case.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
REQUIREMENTS_PATH = REPO_ROOT / "docs/regulatory/requirements.yaml"
SOURCES_PATH = REPO_ROOT / "docs/regulatory/sources.yaml"
OPEN_ITEMS_PATH = REPO_ROOT / "docs/OPEN-ITEMS.md"
EVAL_REPORT_PATH = REPO_ROOT / "eval-output/evaluation.json"


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}


# =====================================================================================
# Rules
# =====================================================================================


def rules_view(session) -> dict:
    """Every rule, with what it did on the current case.

    A rule that produced nothing is as interesting as one that fired - "why did this not
    check anything?" is the question you actually have while tuning.
    """
    if session.rule_set is None:
        return {"version": None, "rules": [], "note": "No rule set loaded."}

    findings = {f.rule_id: f for f in session.findings}
    requirements = {
        r["id"]: r for r in (_load_yaml(REQUIREMENTS_PATH).get("requirements") or [])
    }

    rules = []
    for rule in session.rule_set.rules:
        finding = findings.get(rule.rule_id)
        cites = [
            {
                "id": c,
                "location": requirements.get(c, {}).get("location"),
                "quote": (requirements.get(c, {}).get("quote") or "")[:300],
                "resolves": c in requirements,
            }
            for c in rule.citations
        ]
        rules.append({
            "rule_id": rule.rule_id,
            "title": rule.title,
            "category": rule.category,
            "severity": rule.severity.value,
            "determinacy": rule.determinacy.value,
            "status": rule.status.value,
            "enforceable": rule.is_enforceable,
            "check": rule.check,
            "params": rule.params,
            # An empty citation list is meaningful: a BUSINESS rule, not a regulatory
            # checkpoint. The UI must never imply backing a rule does not have.
            "regulatory": rule.is_regulatory,
            "citations": cites,
            "applicability": {
                k: v for k, v in
                (rule.applicability.model_dump() if rule.applicability else {}).items()
                if v
            },
            "recommended_action": rule.recommended_action,
            "outcome": None if finding is None else {
                "determination": finding.determination.value,
                "disposition": finding.disposition.value,
                "message": finding.message,
                "advisory": finding.advisory_only,
                "evidence": finding.evidence.note,
            },
        })

    approved = sum(1 for r in session.rule_set.rules if r.is_enforceable)
    return {
        "version": session.rule_set.version,
        "total": len(rules),
        "approved": approved,
        "regulatory": sum(1 for r in rules if r["regulatory"]),
        "business": sum(1 for r in rules if not r["regulatory"]),
        "rules": rules,
        "note": (
            "No rule is APPROVED, so nothing is enforced. The board evaluates in DRY_RUN "
            "and labels every finding advisory. Rules ship disabled until legal sign-off."
            if approved == 0 else None
        ),
    }


# =====================================================================================
# Regulatory knowledge base
# =====================================================================================

#: A requirement may only become a rule when its source is verified.
RULE_READY_STATUS = "PRIMARY_VERIFIED"


def regulatory_view() -> dict:
    """The 21 requirements, with what blocks the ones that are blocked."""
    reqs_doc = _load_yaml(REQUIREMENTS_PATH)
    sources_doc = _load_yaml(SOURCES_PATH)
    instruments = {i["id"]: i for i in (sources_doc.get("instruments") or [])}

    requirements = []
    for r in reqs_doc.get("requirements") or []:
        inst = instruments.get(r.get("source"), {})
        verification = inst.get("verification_status", "UNKNOWN")
        needs_review = r.get("status") == "REQUIRES_LEGAL_REVIEW"
        blocked_because = None
        if verification != RULE_READY_STATUS:
            blocked_because = (
                f"Source {inst.get('id', r.get('source'))} is {verification}, not "
                f"{RULE_READY_STATUS}. It must be confirmed against an authoritative "
                f"copy before this can become a rule."
            )
        elif needs_review:
            blocked_because = (
                "Flagged REQUIRES_LEGAL_REVIEW - the requirement is ambiguous and must "
                "not be auto-enabled."
            )
        requirements.append({
            "id": r["id"],
            "source": r.get("source"),
            "instrument": inst.get("title"),
            "location": r.get("location"),
            "quote": r.get("quote"),
            "feasibility": r.get("feasibility"),
            "obligation_kind": r.get("obligation_kind"),
            "priority": r.get("priority"),
            "notes": r.get("notes"),
            "verification_status": verification,
            "rule_ready": blocked_because is None,
            "blocked_because": blocked_because,
        })

    negatives = [
        {"id": n["id"], "source": n.get("source"),
         "searched_for": n.get("searched_for"),
         "conclusion": n.get("conclusion"), "consequence": n.get("consequence")}
        for n in (reqs_doc.get("negative_findings") or [])
    ]

    return {
        "requirements": requirements,
        "total": len(requirements),
        "rule_ready": sum(1 for r in requirements if r["rule_ready"]),
        "blocked": sum(1 for r in requirements if not r["rule_ready"]),
        # What an instrument does NOT say is a finding too, and it is the kind that gets
        # silently re-derived - or invented - if it is not written down.
        "negative_findings": negatives,
        "instruments": [
            {"id": i["id"], "title": i.get("title"),
             "status": i.get("verification_status"),
             "reference": i.get("reference")}
            for i in instruments.values()
        ],
        "provenance": sources_doc.get("local_copy_provenance") or [],
    }


# =====================================================================================
# Pipeline trace
# =====================================================================================


def trace_view(session) -> dict:
    """Stage-by-stage detail for each uploaded document."""
    from .service import box_label

    documents = []
    for ctx in session.documents.values():
        extraction = ctx.extraction
        documents.append({
            "document_id": ctx.document_id,
            "box": box_label(ctx.box_key),
            "filename": ctx.filename,
            "status": ctx.status.value,
            "size_bytes": len(ctx.data),
            "quality": ctx.quality_metrics,
            "reading": ctx.ocr_stats,
            "classification": ctx.classification_detail,
            "extraction": None if extraction is None else {
                "fields": extraction.extracted_count,
                "missing_required": list(extraction.missing_required),
                # Values the model produced but could not point at on the page. Every one
                # of these is a hallucination that ADR-0004 caught.
                "discarded_ungrounded": list(extraction.rejected_ungrounded),
                "notes": list(extraction.notes),
                "by_field": [
                    {"name": f.field_name, "attribute": f.attribute,
                     "confidence": f.confidence.value, "page": f.provenance.page,
                     "source": f.provenance.kind,
                     "notes": list(f.notes)}
                    for f in extraction.fields
                ],
            },
            "corrections": sorted(ctx.corrections),
            "stages": [
                {"key": s.key, "label": s.label, "status": s.status.value,
                 "detail": s.detail}
                for s in ctx.stages
            ],
        })

    prop = session.case.properties[0] if session.case.properties else None
    claims = []
    if prop is not None:
        for attribute, claim_set in sorted(prop.claim_sets.items()):
            resolution = prop.resolve(attribute)
            claims.append({
                "attribute": attribute,
                "claims": len(claim_set.claims),
                "determination": resolution.determination.value,
                "rationale": resolution.rationale,
                "confidence": resolution.confidence.value,
            })

    return {
        "documents": documents,
        "case_id": session.case.case_id,
        "claims": claims,
        "parties": [
            {"roles": sorted(p.roles), "variants": p.name_variants}
            for p in session.case.parties
        ],
        "notes": list(session.case_notes),
        "context": (
            None if session.case.processing_context is None else {
                "pipeline_version": session.case.processing_context.pipeline_version,
                "rule_set_version": session.case.processing_context.rule_set_version,
                "models": session.case.processing_context.model_versions,
                "regulatory_as_of": session.case.processing_context.regulatory_as_of.isoformat(),
            }
        ),
    }


# =====================================================================================
# Verification
# =====================================================================================


def verification_view(session) -> dict:
    run = session.verification
    if run is None:
        return {"planned": False,
                "note": "No property on the case yet, so nothing has been planned."}

    return {
        "planned": True,
        "summary": run.summary(),
        "plan": [
            {
                "source_id": item.source_id,
                "authority": item.source.authority,
                "tier": item.tier.value,
                "tier_confidence": item.source.tier_confidence,
                "execution": item.execution.value,
                "attributes": item.attributes,
                # Exactly what would be sent. An external lookup is an outbound
                # disclosure of customer data, so the minimisation decision is shown.
                "lookup_keys": item.lookup_keys,
                "ambiguous_keys": item.ambiguous_keys,
                "reason": item.reason,
                "blocked_on": list(item.source.blocked_on),
            }
            for item in run.plan.items
        ],
        "tasks": [
            {"task_id": t.task_id, "source_id": t.source_id, "authority": t.authority,
             "tier": t.tier.value, "status": t.status.value,
             "lookup_keys": t.lookup_keys, "attributes": t.attributes,
             "instruction": t.render_instruction()}
            for t in run.tasks
        ],
        "results": [
            {"source_id": r.source_id, "attribute": r.attribute,
             "status": r.status.value, "detail": r.detail,
             "internal": r.internal_value, "external": r.external_value}
            for r in run.results
        ],
        "notes": list(run.notes),
    }


# =====================================================================================
# Evaluation
# =====================================================================================


def evaluation_view() -> dict:
    """The last harness run, if one has been done."""
    if not EVAL_REPORT_PATH.is_file():
        return {
            "available": False,
            "note": ("No evaluation has been run in this workspace. Run it from this "
                     "page, or with tools/evaluate.py."),
        }
    try:
        report = json.loads(EVAL_REPORT_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"available": False, "note": f"Could not read the report: {exc}"}

    return {
        "available": True,
        "started_at": report.get("started_at"),
        "coverage": report.get("coverage", {}),
        "classification": report.get("classification", {}),
        "ocr": report.get("ocr", {}),
        "extraction": report.get("extraction", {}).get("overall", {}),
        "by_field": report.get("extraction", {}).get("by_field", {}),
        "findings": report.get("findings", {}),
        "dangerous_errors": report.get("dangerous_errors", []),
        "notes": report.get("notes", []),
        # Reading a rate over a handful of synthetic documents as an accuracy figure is
        # the mistake this warning exists to prevent.
        "caveat": (
            "Measured on synthetic fixtures. The values are the ones the generator wrote, "
            "so this demonstrates plumbing, not competence on real documents."
        ),
    }


# =====================================================================================
# Open items
# =====================================================================================

_ROW = re.compile(r"^\|\s*(?P<num>~*\d+~*)\s*\|\s*(?P<item>.+?)\s*\|(?P<rest>.*)\|\s*$")


def open_items_view() -> dict:
    """The tracked deferrals, parsed out of OPEN-ITEMS.md.

    Read from the file rather than duplicated, so the board cannot drift from the record.
    """
    if not OPEN_ITEMS_PATH.is_file():
        return {"items": [], "note": "OPEN-ITEMS.md not found."}

    section = ""
    items = []
    for line in OPEN_ITEMS_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = _ROW.match(line)
        if not m:
            continue
        num = m.group("num")
        closed = num.startswith("~")
        detail = " ".join(
            c.strip() for c in m.group("rest").split("|") if c.strip())
        items.append({
            "number": num.strip("~"),
            "section": section,
            "item": m.group("item").replace("**", "").replace("~~", "").strip(),
            "detail": detail,
            "closed": closed,
        })

    open_count = sum(1 for i in items if not i["closed"])
    return {
        "items": items,
        "total": len(items),
        "open": open_count,
        "closed": len(items) - open_count,
        "by_section": {
            s: sum(1 for i in items if i["section"] == s and not i["closed"])
            for s in dict.fromkeys(i["section"] for i in items)
        },
    }
