#!/usr/bin/env python3
"""Consistency checks over the regulatory knowledge base.

The knowledge base carries invariants that are easy to state and easy to violate
silently. The most important one is that a requirement must not be grounded on a source
we have not actually read from an authoritative copy. A violation there does not crash
anything - it just quietly produces a compliance rule with no real legal basis, which is
the single worst failure mode this project has.

Run in CI.

    python tools/check_regulatory.py

Exit code 0 = clean, 1 = error, 2 = warnings only.
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("pyyaml required: pip install pyyaml")

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / "docs/regulatory/sources.yaml"
REQUIREMENTS = ROOT / "docs/regulatory/requirements.yaml"
RULES_DIR = ROOT / "rules"

# A requirement may only be PROMOTED TO A RULE if its source is verified.
RULE_READY_STATUS = "PRIMARY_VERIFIED"

errors: list[str] = []
warnings: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)


def warn(msg: str) -> None:
    warnings.append(msg)


def load(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        sys.exit(f"FATAL: cannot parse {path.relative_to(ROOT)}: {exc}")


def main() -> int:
    src = load(SOURCES)
    req = load(REQUIREMENTS)

    instruments = {i["id"]: i for i in src.get("instruments", [])}
    requirements = req.get("requirements", []) or []
    negatives = req.get("negative_findings", []) or []

    # --- ids unique -------------------------------------------------------------
    for label, items in (("instrument", src.get("instruments", [])),
                         ("verification_source", src.get("verification_sources", [])),
                         ("requirement", requirements),
                         ("negative_finding", negatives)):
        seen: set[str] = set()
        for it in items:
            i = it.get("id")
            if not i:
                err(f"{label} with no id: {it!r:.80}")
            elif i in seen:
                err(f"duplicate {label} id: {i}")
            else:
                seen.add(i)

    # --- every requirement resolves to a known instrument ------------------------
    for r in requirements + negatives:
        s = r.get("source")
        if not s:
            err(f"{r.get('id')}: no source")
        elif s not in instruments:
            err(f"{r.get('id')}: source {s} is not in sources.yaml")

    # --- THE INVARIANT ----------------------------------------------------------
    # A requirement grounded on an unverified source is not yet rule-ready.
    for r in requirements:
        inst = instruments.get(r.get("source"))
        if not inst:
            continue
        status = inst.get("verification_status")
        if status != RULE_READY_STATUS:
            warn(
                f"{r['id']}: source {inst['id']} is {status}, not {RULE_READY_STATUS}. "
                f"NOT rule-ready - must be confirmed against an authoritative copy before "
                f"being promoted to a rule."
            )

    # --- requirements needing human resolution should be visible -----------------
    for r in requirements:
        if r.get("status") == "REQUIRES_LEGAL_REVIEW":
            warn(f"{r['id']}: REQUIRES_LEGAL_REVIEW - must not be auto-enabled.")

    # --- declared local copies actually exist ------------------------------------
    for inst in instruments.values():
        lc = inst.get("local_copy")
        if lc and not (ROOT / lc).is_file():
            err(f"{inst['id']}: local_copy missing on disk: {lc}")

    # --- provenance covers every file present, and vice versa --------------------
    graded = {p["file"] for p in src.get("local_copy_provenance", []) or []}
    src_dir = ROOT / "docs/regulatory/sources"
    if src_dir.is_dir():
        present = {p.name for p in src_dir.iterdir() if p.suffix.lower() == ".pdf"}
        for f in sorted(present - graded):
            err(f"{f} is in sources/ but has no local_copy_provenance entry. "
                f"An ungraded document is an unverified document.")
        for f in sorted(graded - present):
            warn(f"provenance entry for {f}, but the file is not in sources/")

    # --- rules: citations must resolve, and APPROVED rules must be well-founded ---
    req_by_id = {r["id"]: r for r in requirements}
    rule_count = approved_count = business_rule_count = 0

    for rules_file in sorted(RULES_DIR.glob("*.yaml")) if RULES_DIR.is_dir() else []:
        rs = load(rules_file)
        for rule in rs.get("rules", []) or []:
            rule_count += 1
            rid = rule.get("rule_id", "<no id>")
            status = rule.get("status", "DRAFT")
            citations = rule.get("citations") or []

            if not citations:
                business_rule_count += 1

            for cit in citations:
                if cit not in req_by_id:
                    err(f"rule {rid}: citation {cit} does not resolve to a requirement")
                    continue
                req = req_by_id[cit]
                inst = instruments.get(req.get("source"), {})
                if status == "APPROVED":
                    if inst.get("verification_status") != RULE_READY_STATUS:
                        err(
                            f"rule {rid} is APPROVED but cites {cit}, whose source "
                            f"{inst.get('id')} is {inst.get('verification_status')}. "
                            f"An approved rule must rest on a verified source."
                        )
                    if req.get("status") == "REQUIRES_LEGAL_REVIEW":
                        err(
                            f"rule {rid} is APPROVED but cites {cit}, which is "
                            f"REQUIRES_LEGAL_REVIEW."
                        )

            if status == "APPROVED":
                approved_count += 1
                if not rule.get("legal_signoff"):
                    err(f"rule {rid} is APPROVED without legal_signoff.")

    # --- report -----------------------------------------------------------------
    verified = sum(1 for i in instruments.values()
                   if i.get("verification_status") == RULE_READY_STATUS)
    rule_ready = sum(
        1 for r in requirements
        if instruments.get(r.get("source"), {}).get("verification_status") == RULE_READY_STATUS
        and r.get("status") != "REQUIRES_LEGAL_REVIEW"
    )

    print(f"instruments        : {len(instruments)} ({verified} {RULE_READY_STATUS})")
    print(f"requirements       : {len(requirements)}")
    print(f"  rule-ready       : {rule_ready}")
    print(f"  blocked          : {len(requirements) - rule_ready}")
    print(f"negative findings  : {len(negatives)}")
    print(f"rules              : {rule_count}")
    print(f"  approved         : {approved_count}")
    print(f"  regulatory       : {rule_count - business_rule_count}")
    print(f"  business rules   : {business_rule_count}")

    if warnings:
        print(f"\n{len(warnings)} warning(s):")
        for w in warnings:
            print(f"  ! {w}")
    if errors:
        print(f"\n{len(errors)} ERROR(s):")
        for e in errors:
            print(f"  x {e}")
        return 1

    print("\nOK" + (" (with warnings)" if warnings else ""))
    return 2 if warnings else 0


if __name__ == "__main__":
    raise SystemExit(main())
