"""OCR result cache.

OCR is the most expensive step in the pipeline, and it is deterministic for a given
(content, page, engine, render settings). Caching on that tuple makes reprocessing a case
cheap, which matters because reproducibility requires that reprocessing be routine.

The engine id is part of the key, so upgrading the recogniser invalidates prior results
rather than silently serving output from a different model.

PRIVACY WARNING
---------------
A cache entry contains extracted document text. It is customer content and is subject to
the same handling rules as the documents themselves: the cache directory must live outside
the repository, must not be included in any backup that leaves the machine, and must be
covered by the retention policy. See docs/privacy/data-handling-policy.md.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path

from .types import OcrPage

log = logging.getLogger(__name__)

#: Bumped when the OcrPage shape changes, so old entries are not deserialised wrongly.
CACHE_SCHEMA_VERSION = 1


def cache_key(sha256: str, page: int, engine_id: str, dpi: float) -> str:
    return f"v{CACHE_SCHEMA_VERSION}:{sha256}:{page}:{engine_id}:{dpi:.0f}"


class OcrCache(ABC):
    @abstractmethod
    def get(self, key: str) -> OcrPage | None: ...

    @abstractmethod
    def put(self, key: str, page: OcrPage) -> None: ...


class NullOcrCache(OcrCache):
    """Caches nothing. The default, so caching is an explicit choice."""

    def get(self, key: str) -> OcrPage | None:
        return None

    def put(self, key: str, page: OcrPage) -> None:
        return None


class InMemoryOcrCache(OcrCache):
    def __init__(self) -> None:
        self._store: dict[str, OcrPage] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> OcrPage | None:
        hit = self._store.get(key)
        if hit is None:
            self.misses += 1
        else:
            self.hits += 1
        return hit

    def put(self, key: str, page: OcrPage) -> None:
        self._store[key] = page

    def __len__(self) -> int:
        return len(self._store)

    def __bool__(self) -> bool:
        """Always truthy.

        Without this, an empty cache is falsy because __len__ returns 0, and any caller
        writing `cache or default` silently drops it. That exact bug shipped once here.
        """
        return True


class FileOcrCache(OcrCache):
    """JSON files on disk, sharded by content hash.

    The directory holds customer document text - see the module warning.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        import hashlib

        h = hashlib.sha256(key.encode()).hexdigest()
        return self.root / h[:2] / f"{h}.json"

    def get(self, key: str) -> OcrPage | None:
        p = self._path(key)
        if not p.exists():
            self.misses += 1
            return None
        try:
            page = OcrPage.model_validate_json(p.read_text(encoding="utf-8"))
            self.hits += 1
            return page
        except Exception as exc:
            # A corrupt entry must not break extraction - recompute instead.
            log.warning("discarding unreadable cache entry: %s", exc)
            self.misses += 1
            return None

    def put(self, key: str, page: OcrPage) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".partial")
        tmp.write_text(page.model_dump_json(), encoding="utf-8")
        tmp.replace(p)
