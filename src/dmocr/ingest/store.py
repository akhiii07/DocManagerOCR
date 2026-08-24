"""Content-addressed document storage.

Documents are stored under the SHA-256 of their bytes. That gives deduplication for free
and, more importantly, ties every piece of evidence to exact content: a finding citing
page 4 of `DOC123` refers to a specific immutable byte sequence, not to whatever currently
sits at a mutable path.

Encryption at rest, per-tenant keys and object-lock are deployment concerns rather than
application logic. The interface is deliberately narrow so a production implementation can
supply them without any caller changing — and the local implementation is dev-only.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from pathlib import Path


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContentStore(ABC):
    """Store and retrieve immutable blobs by content hash."""

    @abstractmethod
    def put(self, data: bytes) -> str:
        """Store `data`; return its SHA-256. Idempotent by construction."""

    @abstractmethod
    def get(self, digest: str) -> bytes: ...

    @abstractmethod
    def exists(self, digest: str) -> bool: ...

    @abstractmethod
    def path_for(self, digest: str) -> Path | None:
        """Local path, where one exists. None for stores with no local filesystem."""


class LocalContentStore(ContentStore):
    """Filesystem-backed store, sharded by hash prefix.

    Development and single-node use only. It provides no encryption at rest, so the
    directory it points at must live outside the repository and outside any backup that
    leaves the machine — see docs/privacy/data-handling-policy.md.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        # Two levels of sharding keeps directory sizes reasonable at scale.
        return self.root / digest[:2] / digest[2:4] / digest

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        target = self._path(digest)
        if target.exists():
            return digest  # identical content already stored
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp name then rename, so a crash cannot leave a partial blob
        # visible under a hash that promises complete content.
        tmp = target.with_suffix(".partial")
        tmp.write_bytes(data)
        tmp.replace(target)
        return digest

    def get(self, digest: str) -> bytes:
        p = self._path(digest)
        if not p.exists():
            raise KeyError(digest)
        return p.read_bytes()

    def exists(self, digest: str) -> bool:
        return self._path(digest).exists()

    def path_for(self, digest: str) -> Path | None:
        p = self._path(digest)
        return p if p.exists() else None


class InMemoryContentStore(ContentStore):
    """Test support."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, data: bytes) -> str:
        digest = sha256_hex(data)
        self._blobs.setdefault(digest, data)
        return digest

    def get(self, digest: str) -> bytes:
        return self._blobs[digest]

    def exists(self, digest: str) -> bool:
        return digest in self._blobs

    def path_for(self, digest: str) -> Path | None:
        return None
