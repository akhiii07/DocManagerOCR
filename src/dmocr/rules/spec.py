"""Rule specifications.

Rules live in YAML, not in code. They must be diffable, reviewable by risk and compliance
staff who do not read Python, versioned, and dated — because regulation changes and a case
must remain explainable under the rules in force when it was processed.

What stays in code is the **predicate**: a named, registered function implementing the
actual comparison. The YAML says *what is being checked, how badly it matters, under what
circumstances, and on whose authority*. The Python says *how to compute it*. Neither can
express the other's part, which is the point.

Two gates prevent an unsound rule from acting on a real case:

* `status` must be `APPROVED` — rules ship as `DRAFT` and require explicit legal sign-off.
* `citations` must resolve to requirements whose source is `PRIMARY_VERIFIED`, enforced by
  `tools/check_regulatory.py`.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..model.common import DocumentType, Severity
from ..model.findings import Determinacy


class RuleStatus(StrEnum):
    """Lifecycle of a rule. Nothing enforces until APPROVED."""

    DRAFT = "DRAFT"
    #: Authored, but the underlying requirement is ambiguous or its source unverified.
    PENDING_LEGAL_REVIEW = "PENDING_LEGAL_REVIEW"
    APPROVED = "APPROVED"
    #: Kept for reproducibility of past cases, not applied to new ones.
    RETIRED = "RETIRED"


class LegalSignoff(BaseModel):
    model_config = ConfigDict(frozen=True)
    by: str
    at: date
    note: str | None = None


class Applicability(BaseModel):
    """When this rule is in scope.

    Everything is optional and an empty list means "no constraint on this dimension".
    Applicability is evaluated BEFORE the predicate, so an out-of-scope rule yields
    `NOT_APPLICABLE` rather than running and being ignored — the distinction matters
    because NOT_APPLICABLE is reported to the reviewer as a deliberate non-check.
    """

    model_config = ConfigDict(frozen=True)

    lender_types: list[str] = Field(default_factory=list)
    products: list[str] = Field(default_factory=list)
    states: list[str] = Field(default_factory=list)
    transaction_types: list[str] = Field(default_factory=list)
    security_types: list[str] = Field(default_factory=list)

    #: Rule only applies if the case contains ALL of these document types.
    requires_all_documents: list[DocumentType] = Field(default_factory=list)
    #: Rule only applies if the case contains AT LEAST ONE of these.
    requires_any_document: list[DocumentType] = Field(default_factory=list)

    #: Regulatory validity window. Compared against ProcessingContext.regulatory_as_of,
    #: never against "today" — a case reprocessed next year must evaluate under the rules
    #: in force at its stated as-of date.
    effective_from: date | None = None
    effective_to: date | None = None

    def applies_to(self, case, as_of: date) -> tuple[bool, str]:
        """Return (applies, reason_if_not)."""
        if self.effective_from and as_of < self.effective_from:
            return False, f"rule not in force until {self.effective_from}"
        if self.effective_to and as_of > self.effective_to:
            return False, f"rule superseded on {self.effective_to}"

        checks = (
            (self.lender_types, str(case.lender_type.value), "lender type"),
            (self.products, str(case.product.value), "product"),
            (self.states, str(case.state), "state"),
            (self.transaction_types, str(case.loan.transaction_type.value), "transaction type"),
            (self.security_types, str(case.security_type.value), "security type"),
        )
        for allowed, actual, label in checks:
            if allowed and actual not in allowed:
                return False, f"{label} {actual!r} not in {allowed}"

        present = {d.document_type for d in case.documents}
        missing = [t for t in self.requires_all_documents if t not in present]
        if missing:
            return False, f"missing required document types: {[t.value for t in missing]}"
        if self.requires_any_document and not (set(self.requires_any_document) & present):
            return False, (
                f"none of {[t.value for t in self.requires_any_document]} present"
            )
        return True, ""


class RuleSpec(BaseModel):
    """One rule."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    version: str
    title: str
    category: str
    severity: Severity
    #: How the result is established. Declared here rather than inferred, because it
    #: determines whether an adverse result may ever become a BLOCKER.
    determinacy: Determinacy

    #: Name of a registered predicate.
    check: str
    params: dict = Field(default_factory=dict)

    applicability: Applicability = Field(default_factory=Applicability)

    #: Requirement ids from docs/regulatory/requirements.yaml. EMPTY IS MEANINGFUL: it
    #: marks a business rule rather than a regulatory checkpoint, and the review package
    #: must present it as such. Mislabelling a business rule as regulatory would be a
    #: serious defect in an audit.
    citations: list[str] = Field(default_factory=list)

    status: RuleStatus = RuleStatus.DRAFT
    legal_signoff: LegalSignoff | None = None

    #: str.format template; predicates supply the variables.
    message: str = ""
    recommended_action: str | None = None

    @model_validator(mode="after")
    def _approved_needs_signoff(self) -> "RuleSpec":
        if self.status is RuleStatus.APPROVED and self.legal_signoff is None:
            raise ValueError(
                f"{self.rule_id}: APPROVED rules require legal_signoff. "
                f"Rules ship DRAFT until a human signs them off."
            )
        return self

    @property
    def is_enforceable(self) -> bool:
        return self.status is RuleStatus.APPROVED

    @property
    def is_regulatory(self) -> bool:
        return bool(self.citations)


class RuleSet(BaseModel):
    """A versioned collection of rules.

    The version is pinned into `ProcessingContext.rule_set_version` so a finding can be
    reproduced later against exactly these rules.
    """

    model_config = ConfigDict(frozen=True)

    version: str
    rules: list[RuleSpec]

    @model_validator(mode="after")
    def _unique_ids(self) -> "RuleSet":
        seen: set[str] = set()
        for r in self.rules:
            if r.rule_id in seen:
                raise ValueError(f"duplicate rule_id: {r.rule_id}")
            seen.add(r.rule_id)
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RuleSet":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def enforceable(self) -> list[RuleSpec]:
        return [r for r in self.rules if r.is_enforceable]

    def by_id(self, rule_id: str) -> RuleSpec | None:
        return next((r for r in self.rules if r.rule_id == rule_id), None)
