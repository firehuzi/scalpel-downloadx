"""单实例锁：防止 CLI 与后台服务同时写同一份状态目录。

用"锁文件 + 心跳时间戳"实现：

* 加锁 = 原子创建锁文件（O_CREAT|O_EXCL）
* 持锁进程周期性刷新 mtime
* 发现锁文件心跳过期（默认 30s）则视为崩溃残留，可安全接管
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

from ..version import LOCK_FILE


class LockError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, state_dir: str | Path, stale_after: float = 30.0) -> None:
        self.path = Path(state_dir) / LOCK_FILE
        self.stale_after = stale_after
        self._acquired = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- 状态 ---------------------------------------------------------
    @property
    def acquired(self) -> bool:
        return self._acquired

    def holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return True
        return age > self.stale_after

    # ---- 加解锁 -------------------------------------------------------
    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self._try_create():
            if not self._is_stale():
                return False
            # 崩溃残留：清理后重试一次
            try:
                self.path.unlink()
            except OSError:
                return False
            if not self._try_create():
                return False
        self._acquired = True
        self._start_heartbeat()
        return True

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        except OSError:
            return False
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "started_at": time.time(),
            }
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        return True

    def _start_heartbeat(self) -> None:
        def beat() -> None:
            while not self._stop.wait(5.0):
                try:
                    os.utime(self.path, None)
                except OSError:
                    return

        self._thread = threading.Thread(target=beat, name="downloadx-lock", daemon=True)
        self._thread.start()

    def release(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._acquired:
            # 只删自己的锁：确认文件里的 pid 仍是本进程
            holder = self.holder() or {}
            if holder.get("pid") == os.getpid():
                try:
                    self.path.unlink()
                except OSError:
                    pass
        self._acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire():
            holder = self.holder() or {}
            raise LockError(
                f"状态目录已被占用（pid={holder.get('pid')}），"
                "请使用 --server 连接正在运行的服务。"
            )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
