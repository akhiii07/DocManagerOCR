"""Verification source registry.

Loaded from `docs/regulatory/sources.yaml`, the same file the B0 authority-map research
produced. The registry is not duplicated in code, so a source's tier, what it verifies and
what it is keyed by cannot drift from the research that established them.

Every source declares its **access tier**, which decides whether an adapter may run
unattended or must go to a human, and caps the confidence of anything it reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache
from pathlib import Path

import yaml

from .results import AccessTier

log = logging.getLogger(__name__)

DEFAULT_REGISTRY = Path(__file__).resolve().parents[3] / "docs/regulatory/sources.yaml"

#: How long a record from each source stays useful. Municipal dues change constantly;
#: a registered deed does not. None means freshness is not assessed.
DEFAULT_FRESHNESS: dict[str, timedelta | None] = {
    "SRC_MCGM_PTAX": timedelta(days=90),
    "SRC_CERSAI": timedelta(days=30),
    "SRC_MAHARERA": timedelta(days=180),
    "SRC_IGR_ESEARCH": None,
    "SRC_PROPERTY_CARD_MH": timedelta(days=365),
}


def _parse_tier(raw: str | None) -> AccessTier:
    """Normalise a research-note tier, which may be a range like 'T1_OR_T2'.

    Ranges resolve to the WORSE (higher-numbered) tier. B0 recorded these as preliminary
    with low confidence, and planning on the optimistic end would produce an automation
    plan the environment cannot actually deliver.
    """
    if not raw:
        return AccessTier.T6_UNAVAILABLE
    tokens = [t for t in raw.upper().replace("_OR_", " ").split() if t.startswith("T")]
    if not tokens:
        return AccessTier.T6_UNAVAILABLE
    worst = max(tokens, key=lambda t: t[1:])
    try:
        return AccessTier(worst[:2])
    except ValueError:
        return AccessTier.T6_UNAVAILABLE


@dataclass(frozen=True)
class SourceSpec:
    """One external verification source."""

    source_id: str
    authority: str
    tier: AccessTier
    #: Canonical attributes this source can speak to.
    verifies: tuple[str, ...] = ()
    #: Identifiers the source is looked up by, best first. Used for data minimisation.
    keyed_by: tuple[str, ...] = ()
    priority: int = 99
    #: Free-text condition under which the source applies at all.
    applicability_gate: str | None = None
    tier_confidence: str = "UNKNOWN"
    access_note: str | None = None
    blocked_on: tuple[str, ...] = ()
    freshness: timedelta | None = None

    @property
    def is_automatable(self) -> bool:
        return self.tier.is_automatable and not self.blocked_on

    @property
    def needs_human(self) -> bool:
        return self.tier.needs_human or bool(self.blocked_on)


#: Maps the research vocabulary in sources.yaml onto canonical model attributes.
#: Kept explicit rather than inferred: a silent mismapping would compare the wrong things.
_VERIFIES_TO_ATTRIBUTE: dict[str, str] = {
    "project_name": "project.name",
    "promoter": "project.promoter",
    "registration_number": "registration.number",
    "project_status": "project.status",
    "phase": "project.phase",
    "registration_validity": "project.registration_validity",
    "registration_particulars": "registration.number",
    "index_ii": "registration.index_ii",
    "parties": "party.owner",
    "consideration": "transaction.consideration",
    "property_description": "property.address",
    "assessee_name": "party.assessee",
    "property_address": "property.address",
    "assessment_number": "tax.assessment_number",
    "outstanding_dues": "tax.outstanding_dues",
    "payment_history": "tax.payment_history",
    "owner_of_record": "party.owner",
    "cts_number": "property.parcel_identifier",
    "area": "property.area",
    "boundaries": "property.boundaries",
    "encumbrance_notes": "property.encumbrance",
    "existing_charges": "property.encumbrance",
    "encumbrance": "property.encumbrance",
    "multiple_lending_against_same_collateral": "property.encumbrance",
}


def _map_verifies(raw: list[str] | None) -> tuple[str, ...]:
    out: list[str] = []
    for item in raw or []:
        mapped = _VERIFIES_TO_ATTRIBUTE.get(item)
        if mapped is None:
            log.debug("no attribute mapping for verifies=%r", item)
            continue
        if mapped not in out:
            out.append(mapped)
    return tuple(out)


def load_sources(path: str | Path | None = None) -> dict[str, SourceSpec]:
    """Read the verification source registry from the B0 research file."""
    p = Path(path) if path else DEFAULT_REGISTRY
    if not p.is_file():
        log.warning("source registry not found at %s; no sources available", p)
        return {}

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    authorities = {a["id"]: a for a in data.get("authorities", []) or []}

    out: dict[str, SourceSpec] = {}
    for raw in data.get("verification_sources", []) or []:
        source_id = raw["id"]
        authority = authorities.get(raw.get("authority"), {}).get(
            "name", raw.get("authority", "unknown")
        )
        blocked = raw.get("blocked_on") or []
        out[source_id] = SourceSpec(
            source_id=source_id,
            authority=authority,
            tier=_parse_tier(raw.get("tier_preliminary")),
            verifies=_map_verifies(raw.get("verifies")),
            keyed_by=tuple(raw.get("keyed_by") or ()),
            priority=int(raw.get("priority", 99)),
            applicability_gate=raw.get("applicability_gate"),
            tier_confidence=str(raw.get("tier_confidence", "UNKNOWN")),
            access_note=raw.get("access_note"),
            blocked_on=tuple(blocked),
            freshness=DEFAULT_FRESHNESS.get(source_id),
        )
    return out


@lru_cache(maxsize=1)
def default_sources() -> dict[str, SourceSpec]:
    return load_sources()


def sources_for_attribute(
    attribute: str, registry: dict[str, SourceSpec] | None = None
) -> list[SourceSpec]:
    """Sources able to speak to an attribute, best-priority first."""
    reg = registry if registry is not None else default_sources()
    matches = [s for s in reg.values() if attribute in s.verifies]
    return sorted(matches, key=lambda s: s.priority)
