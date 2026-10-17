"""运行配置：默认值 -> config.json -> 环境变量（后者覆盖前者）。

所有字段都可以用 ``DOWNLOADX_<字段名大写>`` 覆盖，例如::

    DOWNLOADX_MAX_CONCURRENT_TASKS=5
    DOWNLOADX_GLOBAL_SPEED_LIMIT=2097152   # 2 MiB/s
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .version import APP_NAME, CONFIG_FILE

ENV_PREFIX = "DOWNLOADX_"

_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f"}


def default_state_dir() -> Path:
    env = os.environ.get(ENV_PREFIX + "STATE_DIR")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / ".downloadx"


def default_download_dir() -> Path:
    env = os.environ.get(ENV_PREFIX + "DOWNLOAD_DIR")
    if env:
        return Path(env).expanduser()
    downloads = Path.home() / "Downloads"
    return downloads if downloads.exists() else Path.home() / "Downloads"


@dataclass
class Config:
    # ---- 路径 ----
    download_dir: Path = field(default_factory=default_download_dir)
    state_dir: Path = field(default_factory=default_state_dir)

    # ---- 并发与分片 ----
    max_concurrent_tasks: int = 3
    max_connections_per_task: int = 8
    max_global_connections: int = 32
    max_connections_per_host: int = 16
    min_segment_size: int = 4 * 1024 * 1024
    chunk_size: int = 64 * 1024
    default_priority: int = 5

    # ---- 带宽 ----
    global_speed_limit: int = 0  # bytes/s，0 表示不限速

    # ---- 可靠性 ----
    auto_resume: bool = True
    verify_sha256: bool = True
    checkpoint_interval: float = 2.0
    max_retries: int = 5
    retry_base_delay: float = 0.5
    retry_max_delay: float = 30.0
    connect_timeout: float = 15.0
    read_timeout: float = 30.0
    sock_read_timeout: float = 30.0

    # ---- 并发自校准（对抗"静默按连接限速"的站点）----
    #: 先用 1 连接量一段、再放开到 N 量一段；聚合吞吐没有提升就退回单连接。
    ramp_enabled: bool = True
    #: 小于这个体积不做校准（为测量浪费的几秒不值得）
    ramp_min_total_size: int = 24 * 1024 * 1024
    ramp_warmup_seconds: float = 2.0
    ramp_warmup_bytes: int = 4 * 1024 * 1024
    ramp_probe_seconds: float = 4.0
    #: 放开并发后的吞吐至少要达到单连接的这个倍数，否则判定"加了没用"
    ramp_gain_threshold: float = 1.15

    # ---- 网络 ----
    user_agent: str = f"{APP_NAME}/0.1.0 (+local)"
    network_monitor: bool = False
    network_probe_urls: tuple[str, ...] = (
        "https://www.msftconnecttest.com/connecttest.txt",
        "https://www.baidu.com/",
    )
    network_probe_interval: float = 15.0

    # ---- 本地服务 ----
    host: str = "127.0.0.1"
    port: int = 8720
    #: 访问令牌。留空 = 不鉴权（仅回环地址可访问）；设置后 /api/* 与 /agent/* 必须带
    #: Authorization: Bearer <token>，同时才会放开跨域（浏览器扩展需要）。
    api_token: str = ""
    log_level: str = "INFO"

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                out[f.name] = str(value)
            elif isinstance(value, tuple):
                out[f.name] = list(value)
            else:
                out[f.name] = value
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        defaults = {f.name: getattr(cls(), f.name) for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in defaults or value is None:
                continue
            default = defaults[key]
            if isinstance(default, Path):
                kwargs[key] = Path(value).expanduser()
            elif isinstance(default, tuple):
                kwargs[key] = tuple(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)

    # ------------------------------------------------------------------
    # 磁盘 / 环境变量
    # ------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return self.state_dir / CONFIG_FILE

    def save(self, path: Path | None = None) -> Path:
        target = Path(path) if path else self.config_path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, state_dir: Path | None = None, use_env: bool = True) -> "Config":
        cfg = cls()
        if state_dir is not None:
            cfg.state_dir = Path(state_dir).expanduser()
        else:
            cfg.state_dir = default_state_dir()

        path = cfg.config_path
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cfg = cls.from_dict({**cfg.to_dict(), **data})
                    cfg.state_dir = path.parent
            except (OSError, ValueError):
                # 配置损坏时回退到默认值，不阻塞启动。
                pass

        if use_env:
            cfg.apply_env()
        return cfg

    def apply_env(self, environ: dict[str, str] | None = None) -> "Config":
        env = environ if environ is not None else os.environ
        for f in fields(self):
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None or raw == "":
                continue
            current = getattr(self, f.name)
            try:
                if isinstance(current, bool):
                    low = raw.strip().lower()
                    if low in _TRUE:
                        setattr(self, f.name, True)
                    elif low in _FALSE:
                        setattr(self, f.name, False)
                elif isinstance(current, Path):
                    setattr(self, f.name, Path(raw).expanduser())
                elif isinstance(current, tuple):
                    setattr(self, f.name, tuple(p.strip() for p in raw.split(",") if p.strip()))
                elif isinstance(current, int):
                    setattr(self, f.name, int(raw))
                elif isinstance(current, float):
                    setattr(self, f.name, float(raw))
                elif isinstance(current, str):
                    setattr(self, f.name, raw)
            except (TypeError, ValueError):
                continue
        return self

    def ensure_dirs(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
