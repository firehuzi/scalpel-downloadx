"""DownloadX —— 本地优先、无广告、可被 Agent 调用的下载基础设施。

分层约定（严格单向依赖）：

    ui / cli / api  ->  core  ->  queue / network / storage

下载引擎（core）不依赖任何 LLM，也不依赖 Web 框架；
api 层只是把 core 的能力暴露成 HTTP / 工具调用。
"""

from __future__ import annotations

from .version import APP_NAME, __version__
from .config import Config
from .core.download_manager import DownloadManager
from .core.download_task import DownloadTask
from .core.state_machine import TaskStatus, is_terminal

__all__ = [
    "APP_NAME",
    "__version__",
    "Config",
    "DownloadManager",
    "DownloadTask",
    "TaskStatus",
    "is_terminal",
]
