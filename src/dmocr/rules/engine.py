"""Rule engine.

Evaluates a rule set against a case and produces findings. Deliberately boring: explicit
iteration, no dynamic control flow, no model deciding which rules to run. Reproducibility
is a hard requirement, and an agentic controller is anti-correlated with it — the same
case and the same rule-set version must yield the same findings.

Two execution modes:

* `ENFORCE`  — only APPROVED rules run. This is production.
* `DRY_RUN`  — every rule runs, and results from unapproved rules are marked
  `advisory_only`. This is how a draft rule is evaluated against real cases to measure its
  false-positive rate *before* anyone signs it off.

A rule that raises is reported as `NOT_DETERMINABLE`, never silently skipped. A check that
crashed is a check that did not happen, and the reviewer is entitled to know.
"""

from __future__ import annotations

import logging
from datetime import date
from enum import StrEnum

from ..model.case import Case
from ..model.common import ConfidenceTier, Determination
from ..model.findings import (
    CheckResult,
    Determinacy,
    Evidence,
    Finding,
    derive_disposition,
    sort_findings,
)
from .registry import PredicateOutcome, get_predicate
from .spec import RuleSet, RuleSpec

log = logging.getLogger(__name__)


class ExecutionMode(StrEnum):
    ENFORCE = "ENFORCE"
    DRY_RUN = "DRY_RUN"


class RuleEngine:
    def __init__(self, rule_set: RuleSet):
        self.rule_set = rule_set

    # -- evaluation --------------------------------------------------------------

    def evaluate(
        self,
        case: Case,
        *,
        mode: ExecutionMode = ExecutionMode.ENFORCE,
        as_of: date | None = None,
    ) -> list[Finding]:
        """Run the rule set against `case` and return findings, worst first.

        `as_of` selects which regulatory rules were in force. It defaults to the case's
        `ProcessingContext.regulatory_as_of` so that reprocessing a case reproduces its
        original result rather than re-judging it under today's rules.
        """
        if as_of is None:
            as_of = (
                case.processing_context.regulatory_as_of
                if case.processing_context
                else date.today()
            )

        rules = (
            self.rule_set.enforceable()
            if mode is ExecutionMode.ENFORCE
            else self.rule_set.rules
        )

        findings: list[Finding] = []
        for rule in rules:
            result = self._run_rule(rule, case, as_of, mode)
            findings.append(self._to_finding(rule, case, result))
        return sort_findings(findings)

    def _run_rule(
        self, rule: RuleSpec, case: Case, as_of: date, mode: ExecutionMode
    ) -> CheckResult:
        advisory = mode is ExecutionMode.DRY_RUN and not rule.is_enforceable

        applies, why_not = rule.applicability.applies_to(case, as_of)
        if not applies:
            return CheckResult(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                determination=Determination.NOT_APPLICABLE,
                determinacy=rule.determinacy,
                evidence=Evidence(note=why_not),
                message=f"Not applicable: {why_not}",
                advisory_only=advisory,
            )

        try:
            outcome: PredicateOutcome = get_predicate(rule.check)(case, rule.params)
        except Exception as exc:  # noqa: BLE001 - a crashed check must be visible
            log.exception("rule %s raised", rule.rule_id)
            return CheckResult(
                rule_id=rule.rule_id,
                rule_version=rule.version,
                determination=Determination.NOT_DETERMINABLE,
                determinacy=rule.determinacy,
                evidence=Evidence(note=f"{type(exc).__name__}: {exc}"),
                message=(
                    f"Check could not be completed ({type(exc).__name__}). "
                    f"This is not a pass."
                ),
                advisory_only=advisory,
            )

        return CheckResult(
            rule_id=rule.rule_id,
            rule_version=rule.version,
            determination=outcome.determination,
            determinacy=rule.determinacy,
            evidence=outcome.evidence,
            message=self._render(rule, outcome),
            advisory_only=advisory,
        )

    @staticmethod
    def _render(rule: RuleSpec, outcome: PredicateOutcome) -> str:
        if not rule.message:
            return outcome.evidence.note or ""
        try:
            return rule.message.format(**outcome.message_vars)
        except KeyError as exc:
            # A template referring to a variable the predicate does not supply is an
            # authoring bug. Degrade to the evidence note rather than losing the finding.
            log.warning("rule %s message missing var %s", rule.rule_id, exc)
            return outcome.evidence.note or rule.title

    @staticmethod
    def _to_finding(rule: RuleSpec, case: Case, result: CheckResult) -> Finding:
        disposition = derive_disposition(
            result.determination, rule.severity, rule.determinacy
        )
        # Confidence tracks how the result was established, not how alarming it is.
        if result.determination is Determination.NOT_DETERMINABLE:
            confidence = ConfidenceTier.INSUFFICIENT
        elif rule.determinacy.is_machine_certain:
            confidence = ConfidenceTier.HIGH
        else:
            confidence = ConfidenceTier.MEDIUM

        return Finding(
            case_id=case.case_id,
            rule_id=rule.rule_id,
            rule_version=rule.version,
            title=rule.title,
            category=rule.category,
            severity=rule.severity,
            determinacy=rule.determinacy,
            determination=result.determination,
            disposition=disposition,
            message=result.message,
            recommended_action=rule.recommended_action,
            evidence=result.evidence,
            confidence=confidence,
            citations=rule.citations,
            advisory_only=result.advisory_only,
        )


def summarise(findings: list[Finding]) -> dict[str, int]:
    """Counts for the review package header.

    `not_determinable` is reported separately and never folded into failures — the
    reviewer needs to distinguish "checked and failed" from "could not check".
    """
    from ..model.findings import Disposition

    return {
        "total": len(findings),
        "blockers": sum(1 for f in findings if f.disposition is Disposition.BLOCKER),
        "review_required": sum(
            1 for f in findings if f.disposition is Disposition.REVIEW_REQUIRED
        ),
        "cleared": sum(1 for f in findings if f.disposition is Disposition.CLEARED),
        "informational": sum(
            1 for f in findings if f.disposition is Disposition.INFORMATIONAL
        ),
        "not_applicable": sum(
            1 for f in findings if f.disposition is Disposition.NOT_APPLICABLE
        ),
        "not_determinable": sum(
            1 for f in findings if f.determination is Determination.NOT_DETERMINABLE
        ),
        "regulatory": sum(1 for f in findings if f.is_regulatory),
        "business_rules": sum(1 for f in findings if not f.is_regulatory),
        "advisory_only": sum(1 for f in findings if f.advisory_only),
    }
