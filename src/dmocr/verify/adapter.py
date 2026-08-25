"""Verification adapters.

One uniform contract for every source, whatever its access tier. An adapter receives the
**minimum keys the planner decided to send** and returns an `ExternalObservation` whose
fields use canonical attributes, so comparison is like-for-like.

Adapters must not decide scope. Whether a source applies to a property is the planner's
job; an adapter that also filtered would put jurisdiction rules in two places.

`SourceUnavailable` exists so that "the portal is down" is a distinct, first-class outcome
rather than an exception that reads as failure. It never counts against the case.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..model.claims import ClaimValue
from .results import AccessTier, ExternalObservation, Snapshot


class SourceUnavailable(Exception):
    """The source could not be reached. NOT a compliance failure."""

    def __init__(self, source_id: str, reason: str):
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"{source_id} unavailable: {reason}")


class ManualRetrievalRequired(Exception):
    """The source needs a human operator; a task should be queued instead."""

    def __init__(self, source_id: str, reason: str):
        self.source_id = source_id
        self.reason = reason
        super().__init__(f"{source_id} requires manual retrieval: {reason}")


class VerificationAdapter(ABC):
    """Fetches an observation from one authoritative source."""

    @property
    @abstractmethod
    def source_id(self) -> str: ...

    @property
    @abstractmethod
    def authority(self) -> str: ...

    @property
    @abstractmethod
    def tier(self) -> AccessTier: ...

    @abstractmethod
    def fetch(self, keys: dict[str, str]) -> ExternalObservation:
        """Retrieve a record. Raise `SourceUnavailable` if the source cannot be reached."""

    @property
    def available(self) -> bool:
        """Whether this adapter can run at all in this environment."""
        return True


class NotImplementedAdapter(VerificationAdapter):
    """Placeholder for a source we have registered but cannot yet call.

    Reports unavailability rather than raising at construction, so a plan can name the
    source, explain why it is not callable, and route it to a human - instead of the
    source silently disappearing from the review package.
    """

    def __init__(self, source_id: str, authority: str, tier: AccessTier, reason: str):
        self._source_id = source_id
        self._authority = authority
        self._tier = tier
        self.reason = reason

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def authority(self) -> str:
        return self._authority

    @property
    def tier(self) -> AccessTier:
        return self._tier

    @property
    def available(self) -> bool:
        return False

    def fetch(self, keys: dict[str, str]) -> ExternalObservation:
        raise SourceUnavailable(self._source_id, self.reason)


class StaticAdapter(VerificationAdapter):
    """Adapter returning a fixed observation. Test and demonstration support.

    Lets the orchestrator, comparison and task logic be exercised without any external
    dependency - which matters because none of the real sources is reachable from the
    development environment (ADR-0006).
    """

    def __init__(
        self,
        source_id: str,
        authority: str,
        tier: AccessTier,
        fields: dict[str, ClaimValue] | None = None,
        *,
        record_found: bool = True,
        record_as_of: datetime | None = None,
        unavailable: str | None = None,
    ):
        self._source_id = source_id
        self._authority = authority
        self._tier = tier
        self._fields = fields or {}
        self._record_found = record_found
        self._record_as_of = record_as_of
        self._unavailable = unavailable
        self.calls: list[dict[str, str]] = []

    @property
    def source_id(self) -> str:
        return self._source_id

    @property
    def authority(self) -> str:
        return self._authority

    @property
    def tier(self) -> AccessTier:
        return self._tier

    def fetch(self, keys: dict[str, str]) -> ExternalObservation:
        self.calls.append(dict(keys))
        if self._unavailable:
            raise SourceUnavailable(self._source_id, self._unavailable)
        return ExternalObservation(
            snapshot=Snapshot(
                source_id=self._source_id,
                authority=self._authority,
                retrieved_at=datetime.now(),
                tier=self._tier,
                request_keys=dict(keys),
            ),
            fields=dict(self._fields),
            record_found=self._record_found,
            record_as_of=self._record_as_of,
        )


class AdapterRegistry:
    """Maps source ids to adapters."""

    def __init__(self, adapters: list[VerificationAdapter] | None = None):
        self._adapters: dict[str, VerificationAdapter] = {
            a.source_id: a for a in (adapters or [])
        }

    def register(self, adapter: VerificationAdapter) -> None:
        self._adapters[adapter.source_id] = adapter

    def get(self, source_id: str) -> VerificationAdapter | None:
        return self._adapters.get(source_id)

    def __contains__(self, source_id: object) -> bool:
        return source_id in self._adapters

    def __len__(self) -> int:
        return len(self._adapters)

    def __bool__(self) -> bool:
        # Always truthy. An empty registry is a legitimate configuration, and `registry or
        # default` would silently replace it - the same trap that shipped once in the OCR
        # cache.
        return True
