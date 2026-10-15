"""pytest 公共夹具。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from downloadx.config import Config  # noqa: E402
from tests.support.server import TestServer  # noqa: E402


def run(coro):
    """在同步测试里跑协程，避免依赖 pytest-asyncio。"""
    return asyncio.run(coro)


@pytest.fixture()
def server():
    with TestServer() as srv:
        srv.reset()
        yield srv


@pytest.fixture()
def config(tmp_path) -> Config:
    cfg = Config(
        download_dir=tmp_path / "downloads",
        state_dir=tmp_path / "state",
    )
    cfg.max_concurrent_tasks = 2
    cfg.max_connections_per_task = 4
    cfg.min_segment_size = 32 * 1024
    cfg.chunk_size = 16 * 1024
    cfg.checkpoint_interval = 0.2
    cfg.max_retries = 3
    cfg.retry_base_delay = 0.05
    cfg.retry_max_delay = 0.2
    cfg.verify_sha256 = True
    cfg.network_monitor = False
    cfg.ensure_dirs()
    return cfg
