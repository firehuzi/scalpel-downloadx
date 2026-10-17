"""BT / Magnet / DHT 支持（P3）。

这一层是**独立子进程**：libtorrent 是 GPL，主程序是 MIT，所以边界画在进程上，
子进程只做会话管理与进度上报，不含任何 HTTP 下载逻辑。
"""

from .client import BtClient, BtError, libtorrent_available

__all__ = ["BtClient", "BtError", "libtorrent_available"]
