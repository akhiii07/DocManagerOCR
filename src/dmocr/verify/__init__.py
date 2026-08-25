"""External verification: planning, adapters, human tasks, comparison."""

from .adapter import (
    AdapterRegistry,
    ManualRetrievalRequired,
    NotImplementedAdapter,
    SourceUnavailable,
    StaticAdapter,
    VerificationAdapter,
)
from .compare import compare_observation, compare_values, unavailable_result
from .orchestrator import (
    VerificationOrchestrator,
    VerificationRun,
    render_verification,
)
from .planner import Execution, PlanItem, VerificationPlan, VerificationPlanner
from .results import (
    AccessTier,
    ExternalObservation,
    Snapshot,
    VerificationResult,
    VerificationStatus,
)
from .sources import SourceSpec, default_sources, load_sources, sources_for_attribute
from .tasks import ManualVerificationTask, TaskQueue, TaskStatus

__all__ = [
    "AccessTier",
    "AdapterRegistry",
    "Execution",
    "ExternalObservation",
    "ManualRetrievalRequired",
    "ManualVerificationTask",
    "NotImplementedAdapter",
    "PlanItem",
    "Snapshot",
    "SourceSpec",
    "SourceUnavailable",
    "StaticAdapter",
    "TaskQueue",
    "TaskStatus",
    "VerificationAdapter",
    "VerificationOrchestrator",
    "VerificationPlan",
    "VerificationPlanner",
    "VerificationResult",
    "VerificationRun",
    "VerificationStatus",
    "compare_observation",
    "compare_values",
    "default_sources",
    "load_sources",
    "render_verification",
    "sources_for_attribute",
    "unavailable_result",
]
