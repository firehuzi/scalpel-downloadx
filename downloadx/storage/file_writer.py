"""文件写入器：预分配 + 随机区间写入（Windows 友好）。

两种模式：

* ``ranged``     —— 已知总大小。先 truncate 预分配，再按绝对偏移写入。
                    多连接并发写同一文件，靠一把锁串行化（磁盘顺序写更友好）。
* ``append``     —— 未知总大小（服务端不给 Content-Length）。顺序追加，不可续传。

``os.pwrite`` 在 Windows 上不可用，所以这里用 "seek + write + 锁" 的等价实现。
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path


class FileWriter:
    def __init__(self, path: str | Path, mode: str = "ranged", size: int | None = None) -> None:
        if mode not in ("ranged", "append"):
            raise ValueError(f"未知写入模式: {mode}")
        if mode == "ranged" and (size is None or size < 0):
            raise ValueError("ranged 模式必须提供有效 size")
        self.path = Path(path)
        self.mode = mode
        self.size = int(size) if size is not None else None
        self._fh = None
        self._lock = threading.Lock()
        self._io_lock = asyncio.Lock()
        self._written = 0
        self._closed = False

    # ---- 生命周期 -----------------------------------------------------
    @property
    def written(self) -> int:
        return self._written

    @property
    def closed(self) -> bool:
        return self._closed

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        exists = self.path.exists()
        if self.mode == "ranged":
            self._fh = open(self.path, "r+b" if exists else "w+b")
            if not exists or self._fh.seek(0, os.SEEK_END) != self.size:
                self._fh.truncate(self.size)
            # 预分配：把区间写满，避免边下边扩导致碎片与失败
            if not exists:
                self._preallocate(self.size)
        else:
            self._fh = open(self.path, "ab" if exists else "wb")
        self._closed = False

    def _preallocate(self, size: int) -> None:
        """尽力预分配（NTFS 上 truncate 的结果是稀疏文件，零填充由写入覆盖）。"""
        try:
            self._fh.truncate(size)
        except OSError:
            pass

    async def close(self) -> None:
        if self._fh is None or self._closed:
            return
        await self.flush()
        fh, self._fh, self._closed = self._fh, None, True
        await asyncio.to_thread(fh.close)

    async def flush(self) -> None:
        if self._fh is None:
            return
        fh = self._fh

        def _flush() -> None:
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass

        await asyncio.to_thread(_flush)

    # ---- 写入 ---------------------------------------------------------
    async def write_at(self, offset: int, data: bytes) -> int:
        """在绝对偏移写入。offset 相对整个文件（不是相对分片）。"""
        if self._fh is None:
            raise RuntimeError("文件未打开")
        if self.mode != "ranged":
            raise RuntimeError("append 模式不支持随机写，请使用 append()")
        fh, lock = self._fh, self._lock

        def _write() -> int:
            with lock:
                fh.seek(offset)
                fh.write(data)
                return len(data)

        async with self._io_lock:
            n = await asyncio.to_thread(_write)
        self._written += n
        return n

    async def append(self, data: bytes) -> int:
        if self._fh is None:
            raise RuntimeError("文件未打开")
        if self.mode != "append":
            raise RuntimeError("ranged 模式请使用 write_at()")
        fh, lock = self._fh, self._lock

        def _write() -> int:
            with lock:
                fh.seek(0, os.SEEK_END)
                fh.write(data)
                return len(data)

        async with self._io_lock:
            n = await asyncio.to_thread(_write)
        self._written += n
        return n

    async def truncate_to_pos(self, size: int) -> None:
        """append 模式失败后回滚到已确认进度（防止半截数据留在尾部）。"""
        if self._fh is None or self.mode != "append":
            return
        fh, lock = self._fh, self._lock

        def _truncate() -> None:
            with lock:
                fh.truncate(size)
                fh.flush()

        async with self._io_lock:
            await asyncio.to_thread(_truncate)

    async def __aenter__(self) -> "FileWriter":
        self.open()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()


def finalize_part(part_path: str | Path, target_path: str | Path) -> None:
    """下载完成：把 .dxpart 原子改名为最终文件（同卷 os.replace 是原子的）。"""
    part_path, target_path = Path(part_path), Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    os.replace(part_path, target_path)
