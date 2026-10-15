"""SHA-256 校验。"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from typing import Callable

DEFAULT_CHUNK = 1024 * 1024


class IncrementalHasher:
    """流式哈希器，可在下载过程中增量喂数据。"""

    __slots__ = ("_h", "_size")

    def __init__(self, algorithm: str = "sha256") -> None:
        self._h = hashlib.new(algorithm)
        self._size = 0

    def update(self, data: bytes) -> None:
        self._h.update(data)
        self._size += len(data)

    @property
    def byte_count(self) -> int:
        return self._size

    def hexdigest(self) -> str:
        return self._h.hexdigest()


def sha256_file(
    path: str | Path,
    chunk_size: int = DEFAULT_CHUNK,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    """分块计算文件 SHA-256，避免把大文件读进内存。"""
    path = Path(path)
    total = path.stat().st_size
    hasher = hashlib.sha256()
    done = 0
    with path.open("rb", buffering=0) as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            hasher.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return hasher.hexdigest()


def verify_file(path: str | Path, expected_sha256: str, chunk_size: int = DEFAULT_CHUNK) -> bool:
    expected = (expected_sha256 or "").strip().lower()
    if not expected:
        return True
    actual = sha256_file(path, chunk_size=chunk_size)
    return hmac.compare_digest(actual, expected)


def digest_matches(actual: str | None, expected: str | None) -> bool:
    if not expected:
        return True
    if not actual:
        return False
    return hmac.compare_digest(actual.strip().lower(), expected.strip().lower())
