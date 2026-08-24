"""Canonical data model for collateral cases.

Read `docs/canonical-model.md` before changing anything here. The shape is driven by
specific regulatory findings recorded in `docs/regulatory/requirements.yaml`, not by
convenience, and several apparently redundant distinctions exist to prevent false
positives on real Mumbai cases.
"""

from .case import (
    Case,
    CustodyStatus,
    Document,
    DocumentQuality,
    LenderType,
    Product,
)
from .claims import (
    AreaValue,
    BoolValue,
    Claim,
    ClaimSet,
    ClaimValue,
    DateValue,
    MoneyValue,
    ParcelValue,
    Resolution,
    TextValue,
)
from .common import (
    Area,
    AreaUnit,
    ConfidenceTier,
    Determination,
    DocumentType,
    InstrumentStrength,
    Money,
    ParcelIdentifier,
    ParcelIdentifierType,
    SecurityType,
    Severity,
    instrument_strength_of,
)
from .entities import Party, Project, Property
from .provenance import (
    BoundingBox,
    DerivedProvenance,
    DocumentProvenance,
    ExternalProvenance,
    HumanProvenance,
    ProcessingContext,
    Provenance,
    TextSpan,
)

__all__ = [
    "Area", "AreaUnit", "AreaValue", "BoolValue", "BoundingBox", "Case", "Claim",
    "ClaimSet", "ClaimValue", "ConfidenceTier", "CustodyStatus", "DateValue",
    "DerivedProvenance", "Determination", "Document", "DocumentProvenance",
    "DocumentQuality", "DocumentType", "ExternalProvenance", "HumanProvenance",
    "InstrumentStrength", "LenderType", "Money", "MoneyValue", "ParcelIdentifier",
    "ParcelIdentifierType", "ParcelValue", "Party", "ProcessingContext", "Product",
    "Project", "Property", "Provenance", "Resolution", "SecurityType", "Severity",
    "TextSpan", "TextValue", "instrument_strength_of",
]
