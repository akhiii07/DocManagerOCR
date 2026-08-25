"""Human-operated verification tasks.

The B0 research concluded that of six candidate sources, only CERSAI plausibly supports an
automated adapter. MahaRERA, IGR eSearch and MCGM property tax are all CAPTCHA-gated or
subject to terms that do not permit automation — and bypassing bot detection is out of
scope by explicit requirement.

So **T4/T5 is the primary delivery mechanism for external verification in the MVP, not a
fallback.** The design consequence: the operator supplies *access*; the system supplies
the comparison, the evidence capture and the audit trail. A task carries everything an
operator needs and nothing more, and its result re-enters the same comparison path as an
automated one.

This is also why moving a source from T4 to T1 later is a configuration change rather than
a rewrite: the plumbing on either side of the retrieval is identical.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .results import AccessTier


class TaskStatus(StrEnum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    #: Operator attempted retrieval and the source was down or the record absent.
    UNOBTAINABLE = "UNOBTAINABLE"
    CANCELLED = "CANCELLED"


class ManualVerificationTask(BaseModel):
    """One instruction for an operator to retrieve a record."""

    model_config = ConfigDict(frozen=False)

    task_id: str = Field(default_factory=lambda: f"TASK_{uuid.uuid4().hex[:10]}")
    case_id: str
    source_id: str
    authority: str
    tier: AccessTier
    status: TaskStatus = TaskStatus.OPEN
    priority: int = 99

    #: Exactly what to send. This is the data-minimisation decision made by the planner,
    #: and the operator must not widen it.
    lookup_keys: dict[str, str] = Field(default_factory=dict)
    #: Attributes we intend to compare once the record is retrieved.
    attributes: list[str] = Field(default_factory=list)
    instruction: str = ""
    reason: str = ""

    created_at: datetime = Field(default_factory=lambda: datetime.now())
    completed_at: datetime | None = None
    operator_id: str | None = None
    #: Reference to the artefact the operator captured.
    artefact_ref: str | None = None
    operator_note: str | None = None

    @property
    def is_open(self) -> bool:
        return self.status in (TaskStatus.OPEN, TaskStatus.IN_PROGRESS)

    def render_instruction(self) -> str:
        keys = ", ".join(f"{k}={v}" for k, v in self.lookup_keys.items()) or "(no key)"
        return (
            f"[{self.source_id}] {self.authority}\n"
            f"  Retrieve the record for: {keys}\n"
            f"  Capture: {', '.join(self.attributes) or '(record as displayed)'}\n"
            f"  Send only the identifiers listed above - do not widen the query.\n"
            f"  {self.instruction}".rstrip()
        )


class TaskQueue:
    """In-memory task queue.

    Deliberately narrow. A production deployment backs this with the durable workflow
    store so a case can wait days for an operator without losing state - which is one of
    the arguments for Temporal in open decision O2.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, ManualVerificationTask] = {}

    def add(self, task: ManualVerificationTask) -> ManualVerificationTask:
        self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> ManualVerificationTask | None:
        return self._tasks.get(task_id)

    def open_tasks(self, case_id: str | None = None) -> list[ManualVerificationTask]:
        tasks = [t for t in self._tasks.values() if t.is_open]
        if case_id is not None:
            tasks = [t for t in tasks if t.case_id == case_id]
        return sorted(tasks, key=lambda t: (t.priority, t.created_at))

    def for_case(self, case_id: str) -> list[ManualVerificationTask]:
        return sorted(
            (t for t in self._tasks.values() if t.case_id == case_id),
            key=lambda t: (t.priority, t.created_at),
        )

    def complete(
        self,
        task_id: str,
        *,
        operator_id: str,
        artefact_ref: str | None = None,
        note: str | None = None,
    ) -> ManualVerificationTask:
        task = self._tasks[task_id]
        task.status = TaskStatus.COMPLETED
        task.operator_id = operator_id
        task.artefact_ref = artefact_ref
        task.operator_note = note
        task.completed_at = datetime.now()
        return task

    def mark_unobtainable(
        self, task_id: str, *, operator_id: str, reason: str
    ) -> ManualVerificationTask:
        """The operator tried and could not retrieve it.

        Distinct from COMPLETED and from cancellation: it means the check did not happen,
        which affects case completeness but never pass/fail.
        """
        task = self._tasks[task_id]
        task.status = TaskStatus.UNOBTAINABLE
        task.operator_id = operator_id
        task.operator_note = reason
        task.completed_at = datetime.now()
        return task

    def __len__(self) -> int:
        return len(self._tasks)

    def __bool__(self) -> bool:
        return True
