"""版本与全局常量。

命名约定（改名前想清楚）：

* ``APP_DISPLAY_NAME`` 是给人看的品牌名，可以随便换；
* ``APP_NAME`` 是**运行时标识**——状态目录名、User-Agent、API 的 ``app`` 字段都用它。
  改它会让老的状态目录（``%LOCALAPPDATA%\\DownloadX``）失联、已安装的 skill 与
  中台应用行对不上，所以除非同时做迁移，否则不要动。
"""

from __future__ import annotations

APP_DISPLAY_NAME = "刃研社-DownloadX"
APP_NAME = "DownloadX"  # 运行时标识，见上方说明
__version__ = "0.1.0"

# 磁盘元数据 schema 版本。字段结构变化时递增，并在 metadata_store 中做迁移。
SCHEMA_VERSION = 1

# 未完成文件的临时后缀。下载完成后用 os.replace 原子改名。
PART_SUFFIX = ".dxpart"

# 状态目录里的锁文件与配置文件名。
LOCK_FILE = "downloadx.lock"
CONFIG_FILE = "config.json"
