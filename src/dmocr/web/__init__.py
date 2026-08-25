"""Risk Manager review UI.

    python -m dmocr.web

Binds to localhost only. There is no authentication (ADR-0002).
"""

from .service import BOXES, OTHER_BOX, BoxStatus, ReviewSession, StageStatus, box_label

__all__ = [
    "BOXES",
    "OTHER_BOX",
    "BoxStatus",
    "ReviewSession",
    "StageStatus",
    "box_label",
]
