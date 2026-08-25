"""Verification orchestrator.

Runs a plan: automated sources are called, human-operated ones become tasks, out-of-scope
ones are reported as `NOT_APPLICABLE` rather than dropped.

A completed manual task re-enters through `ingest_manual_observation`, which uses the
**same comparison path** as an automated result. That symmetry is what makes moving a
source from T4 to T1 later a configuration change rather than a rewrite — and it means the
operator supplies only *access*, while the system supplies comparison, evidence and audit.

Nothing here raises on a failed source. Every outcome becomes a `VerificationResult` with
a status the reviewer can act on, because a check that silently vanished is worse than one
that failed loudly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..model.case import Case
from ..model.entities import Property
from .adapter import AdapterRegistry, SourceUnavailable
from .compare import compare_observation, unavailable_result
from .planner import Execution, PlanItem, VerificationPlan, VerificationPlanner
from .results import (
    ExternalObservation,
    Snapshot,
    VerificationResult,
    VerificationStatus,
)
from .sources import SourceSpec
from .tasks import ManualVerificationTask, TaskQueue

log = logging.getLogger(__name__)


@dataclass
class VerificationRun:
    case_id: str
    plan: VerificationPlan
    results: list[VerificationResult] = field(default_factory=list)
    tasks: list[ManualVerificationTask] = field(default_factory=list)
    snapshots: list[Snapshot] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def by_status(self, status: VerificationStatus) -> list[VerificationResult]:
        return [r for r in self.results if r.status is status]

    @property
    def adverse(self) -> list[VerificationResult]:
        return [r for r in self.results if r.status.is_adverse]

    @property
    def needs_review(self) -> list[VerificationResult]:
        return [r for r in self.results if r.review_required]

    def summary(self) -> dict[str, int]:
        """Counts for the review package.

        `checks_performed` counts only results where a source actually answered.
        Unavailable sources and pending tasks are reported separately, under case
        completeness - never folded into pass/fail.
        """
        return {
            "results": len(self.results),
            "checks_performed": sum(1 for r in self.results if r.counts_as_a_check),
            "match": len(self.by_status(VerificationStatus.MATCH)),
            "partial_match": len(self.by_status(VerificationStatus.PARTIAL_MATCH)),
            "mismatch": len(self.by_status(VerificationStatus.MISMATCH)),
            "not_found_in_source": len(
                self.by_status(VerificationStatus.NOT_FOUND_IN_SOURCE)),
            "source_unavailable": len(
                self.by_status(VerificationStatus.SOURCE_UNAVAILABLE)),
            "not_applicable": len(self.by_status(VerificationStatus.NOT_APPLICABLE)),
            "stale": len(self.by_status(VerificationStatus.STALE)),
            "pending_manual": len(self.by_status(VerificationStatus.PENDING_MANUAL)),
            "open_tasks": sum(1 for t in self.tasks if t.is_open),
        }


class VerificationOrchestrator:
    def __init__(
        self,
        registry: AdapterRegistry | None = None,
        planner: VerificationPlanner | None = None,
        task_queue: TaskQueue | None = None,
    ):
        # `is None`, not `or`: an empty registry and an empty queue are legitimate
        # configurations and both define __len__.
        self.registry = registry if registry is not None else AdapterRegistry()
        self.planner = planner if planner is not None else VerificationPlanner()
        self.tasks = task_queue if task_queue is not None else TaskQueue()

    # -- running a plan ----------------------------------------------------------

    def run(self, case: Case) -> VerificationRun:
        plan = self.planner.plan(case)
        run = VerificationRun(case_id=case.case_id, plan=plan)
        run.notes.extend(plan.notes)

        prop = case.properties[0] if case.properties else None
        if prop is None:
            run.notes.append("No property on the case; no verification attempted.")
            return run

        for item in plan.items:
            if item.execution is Execution.SKIP:
                self._record_skip(run, item)
            elif item.execution is Execution.MANUAL:
                self._queue_manual(run, case, item)
            else:
                self._run_automated(run, prop, item)

        for item in plan.items:
            for key in item.ambiguous_keys:
                run.notes.append(
                    f"{item.source_id}: lookup key {key!r} is disputed across documents, "
                    f"so it was not used. Querying on a disputed identifier would "
                    f"retrieve the wrong record."
                )

        # Record on the case so verification-aware rules can read them through the same
        # engine as everything else, rather than through a parallel reporting path.
        case.verification_results = list(run.results)
        return run

    # -- outcomes ----------------------------------------------------------------

    @staticmethod
    def _record_skip(run: VerificationRun, item: PlanItem) -> None:
        """An out-of-scope source is reported, not dropped.

        The reviewer needs to see that a source was considered and why it did not apply.
        """
        run.results.append(VerificationResult(
            source_id=item.source_id,
            authority=item.source.authority,
            attribute="*",
            tier=item.tier,
            status=VerificationStatus.NOT_APPLICABLE,
            detail=item.reason,
        ))

    def _queue_manual(self, run: VerificationRun, case: Case, item: PlanItem) -> None:
        task = self.tasks.add(ManualVerificationTask(
            case_id=case.case_id,
            source_id=item.source_id,
            authority=item.source.authority,
            tier=item.tier,
            priority=item.source.priority,
            lookup_keys=dict(item.lookup_keys),
            attributes=list(item.attributes),
            reason=item.reason,
            instruction=item.source.access_note or "",
        ))
        run.tasks.append(task)

        for attribute in item.attributes or ["*"]:
            run.results.append(VerificationResult(
                source_id=item.source_id,
                authority=item.source.authority,
                attribute=attribute,
                tier=item.tier,
                status=VerificationStatus.PENDING_MANUAL,
                detail=(
                    f"Queued for a human operator ({task.task_id}). {item.reason} "
                    f"This is an open item for case completeness, not a failure."
                ),
            ))

    def _run_automated(
        self, run: VerificationRun, prop: Property, item: PlanItem
    ) -> None:
        adapter = self.registry.get(item.source_id)
        if adapter is None or not adapter.available:
            reason = (
                "no adapter registered" if adapter is None
                else getattr(adapter, "reason", "adapter unavailable")
            )
            for attribute in item.attributes or ["*"]:
                run.results.append(unavailable_result(
                    item.source_id, item.source.authority, attribute, item.tier, reason
                ))
            return

        try:
            observation = adapter.fetch(item.lookup_keys)
        except SourceUnavailable as exc:
            for attribute in item.attributes or ["*"]:
                run.results.append(unavailable_result(
                    item.source_id, item.source.authority, attribute, item.tier,
                    exc.reason,
                ))
            return
        except Exception as exc:  # noqa: BLE001 - an adapter bug must not lose the case
            log.exception("adapter %s raised", item.source_id)
            for attribute in item.attributes or ["*"]:
                run.results.append(unavailable_result(
                    item.source_id, item.source.authority, attribute, item.tier,
                    f"adapter error: {type(exc).__name__}: {exc}",
                ))
            return

        self._compare_all(run, prop, item.source, observation, item.attributes)

    def _compare_all(
        self,
        run: VerificationRun,
        prop: Property,
        source: SourceSpec,
        observation: ExternalObservation,
        attributes: list[str],
    ) -> None:
        run.snapshots.append(observation.snapshot)
        for attribute in attributes or list(observation.fields):
            run.results.append(compare_observation(
                attribute,
                prop.resolve(attribute),
                observation,
                tier=source.tier,
                freshness=source.freshness,
            ))

    # -- manual results re-entering ----------------------------------------------

    def ingest_manual_observation(
        self,
        case: Case,
        task_id: str,
        observation: ExternalObservation,
        *,
        operator_id: str,
    ) -> list[VerificationResult]:
        """Feed a completed manual retrieval back through the same comparison path."""
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"unknown task {task_id}")

        prop = case.properties[0] if case.properties else None
        if prop is None:
            return []

        self.tasks.complete(
            task_id,
            operator_id=operator_id,
            artefact_ref=observation.snapshot.artefact_ref,
        )

        source = self.planner.sources.get(task.source_id)
        tier = source.tier if source else task.tier
        freshness = source.freshness if source else None

        return [
            compare_observation(
                attribute, prop.resolve(attribute), observation,
                tier=tier, freshness=freshness,
            )
            for attribute in (task.attributes or list(observation.fields))
        ]


def render_verification(run: VerificationRun) -> str:
    """Reviewer-first text block for the review package."""
    lines: list[str] = ["EXTERNAL VERIFICATION", "-" * 60]
    plan = run.plan.summary()
    lines.append(
        f"  Sources considered: {plan['sources_considered']}  "
        f"automated: {plan['automated']}  manual: {plan['manual']}  "
        f"skipped: {plan['skipped']}"
    )
    lines.append("")

    shown = [r for r in run.results
             if r.status is not VerificationStatus.NOT_APPLICABLE or r.attribute == "*"]
    for r in sorted(shown, key=lambda r: (r.source_id, r.attribute)):
        head = f"  {r.status.value:20s} {r.source_id:22s} {r.attribute}"
        lines.append(head)
        if r.internal_value or r.external_value:
            lines.append(f"       internal={r.internal_value!r} "
                         f"external={r.external_value!r}")
        if r.detail:
            lines.append(f"       {r.detail}")

    if run.tasks:
        lines.append("")
        lines.append("  OPERATOR TASKS")
        for t in run.tasks:
            for line in t.render_instruction().splitlines():
                lines.append(f"    {line}")

    lines.append("")
    lines.append(f"  {run.summary()}")
    if run.notes:
        for n in run.notes:
            lines.append(f"  - {n}")
    return "\n".join(lines)
