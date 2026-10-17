"""任务元数据持久化。

布局::

    <state_dir>/
    ├── config.json
    ├── tasks/
    │   ├── index.json        # 任务顺序 + schema 版本
    │   └── <task_id>.json    # 每个任务一份，含分片表
    └── history.jsonl         # 追加式历史（完成 / 失败 / 取消）

可靠性要求：

1. **原子写**：先写 ``*.tmp``，fsync，再 ``os.replace``。断电只会丢一次检查点，不会写坏文件。
2. **单任务隔离**：任务之间互不影响，读坏一个文件不会让整个库起不来。
3. **schema 版本**：低版本文件读入时按需迁移。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..version import SCHEMA_VERSION


class MetadataError(RuntimeError):
    pass


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(data)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


class MetadataStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.tasks_dir = self.root / "tasks"
        self.index_path = self.tasks_dir / "index.json"
        self.history_path = self.root / "history.jsonl"
        self._lock = threading.RLock()
        self._order: list[str] = []

    # ---- 初始化 -------------------------------------------------------
    def ensure(self) -> None:
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        if self.index_path.exists():
            self._order = self._load_index()
        else:
            self._order = []
            self._flush_index()

    def _load_index(self) -> list[str]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        order = data.get("order") if isinstance(data, dict) else None
        return [str(x) for x in order] if isinstance(order, list) else []

    def _flush_index(self) -> None:
        atomic_write_json(
            self.index_path,
            {
                "schema_version": SCHEMA_VERSION,
                "order": self._order,
                "updated_at": time.time(),
            },
        )

    # ---- 路径 ---------------------------------------------------------
    def task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    # ---- 读 -----------------------------------------------------------
    def load_task(self, task_id: str) -> dict[str, Any] | None:
        path = self.task_path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return self._migrate(data)

    def load_all_tasks(self) -> list[dict[str, Any]]:
        """按 index 顺序返回全部任务；index 未收录的孤儿文件按文件名补进来。"""
        with self._lock:
            found: list[dict[str, Any]] = []
            seen: set[str] = set()
            for task_id in list(self._order):
                data = self.load_task(task_id)
                if data is not None:
                    found.append(data)
                    seen.add(task_id)

            orphans: list[str] = []
            for path in sorted(self.tasks_dir.glob("*.json")):
                if path.name == "index.json":
                    continue
                task_id = path.stem
                if task_id in seen:
                    continue
                data = self.load_task(task_id)
                if data is not None:
                    found.append(data)
                    orphans.append(task_id)
            if orphans:
                self._order.extend(orphans)
                self._flush_index()
            return found

    # ---- 写 -----------------------------------------------------------
    def save_task(self, payload: dict[str, Any]) -> None:
        task_id = payload.get("id")
        if not task_id:
            raise MetadataError("任务缺少 id，无法持久化")
        payload = {**payload, "schema_version": SCHEMA_VERSION}
        with self._lock:
            atomic_write_json(self.task_path(task_id), payload)
            if task_id not in self._order:
                self._order.append(task_id)
                self._flush_index()

    def save_tasks(self, payloads: Iterable[dict[str, Any]]) -> None:
        with self._lock:
            changed = False
            for payload in payloads:
                task_id = payload.get("id")
                if not task_id:
                    continue
                atomic_write_json(self.task_path(task_id), {**payload, "schema_version": SCHEMA_VERSION})
                if task_id not in self._order:
                    self._order.append(task_id)
                    changed = True
            if changed:
                self._flush_index()

    def delete_task(self, task_id: str) -> bool:
        with self._lock:
            path = self.task_path(task_id)
            existed = path.exists()
            if existed:
                try:
                    path.unlink()
                except OSError:
                    pass
            if task_id in self._order:
                self._order.remove(task_id)
                self._flush_index()
            return existed

    # ---- 历史 ---------------------------------------------------------
    def append_history(self, record: dict[str, Any]) -> None:
        record = {**record, "schema_version": SCHEMA_VERSION}
        self.root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.history_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def read_history(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        lines = self.history_path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records[offset : offset + limit]

    # ---- schema 迁移 --------------------------------------------------
    @staticmethod
    def _migrate(data: dict[str, Any]) -> dict[str, Any]:
        version = int(data.get("schema_version", 0) or 0)
        if version < 1:
            # v0（无版本） -> v1：补齐 v1 引入的字段
            data.setdefault("segments", [])
            data.setdefault("mode", "segmented")
            data.setdefault("priority", 5)
            data.setdefault("retry_count", 0)
            data.setdefault("updated_at", data.get("created_at", time.time()))
        if version < 2:
            # v1 的 HLS 专用字段 -> v2 的通用"流轨"结构（HLS / DASH 共用）
            urls = [str(u) for u in (data.get("hls_playlist") or [])]
            if urls and not data.get("stream_tracks"):
                data["stream_tracks"] = [
                    {
                        "kind": "media",
                        "init": str(data.get("hls_init") or ""),
                        "urls": urls,
                        "done": [int(i) for i in (data.get("hls_done") or [])],
                    }
                ]
                data["stream_count"] = len(urls)
            for key in ("hls_playlist", "hls_init", "hls_done", "hls_total"):
                data.pop(key, None)
        data["schema_version"] = SCHEMA_VERSION
        return data
