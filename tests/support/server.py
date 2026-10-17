"""本地可控 HTTP 服务，用来真实地测试下载引擎。

覆盖的边界：

* ``/file/<name>``        支持 Range / 206 / HEAD
* ``/norange/<name>``     忽略 Range（永远 200），不支持续传
* ``/unknown/<name>``     不给 Content-Length，只能流式下
* ``/flaky/<name>``       前 N 次请求返回 503，测试重试
* ``/truncate/<name>``    Content-Length 报全长但提前断流，测试续传
* ``/slow/<name>``        限速输出，测试暂停 / 取消
* ``/disposition/<name>`` 带 UTF-8 的 Content-Disposition
* ``/missing``            404
"""

from __future__ import annotations

import hashlib
import mimetypes
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

_CACHE: dict[tuple[str, int], bytes] = {}
_CACHE_LOCK = threading.Lock()
_COUNTS: dict[str, int] = {}
_COUNTS_LOCK = threading.Lock()
_REQUESTS: list[dict] = []
_REQUESTS_LOCK = threading.Lock()
_INFLIGHT: dict[str, int] = {}
_INFLIGHT_LOCK = threading.Lock()
_SHARED_PACING: dict[str, float] = {}
_SHARED_LOCK = threading.Lock()
_MOUNTS: dict[str, Path] = {}
_MOUNT_LOCK = threading.Lock()
_MOUNT_SEQ = 0


def register_mount(directory: Path) -> str:
    """把一个真实目录挂到 /disk/<token>/ 下（测试真实 HLS 产物用）。"""
    global _MOUNT_SEQ
    with _MOUNT_LOCK:
        _MOUNT_SEQ += 1
        token = f"m{_MOUNT_SEQ}"
        _MOUNTS[token] = Path(directory).resolve()
        return token


def _shared_wait(key: str, nbytes: int, rate: float) -> None:
    """跨连接的共享发送节奏：不管开多少条连接，聚合速率都被锁在 rate。

    这正是"服务端按连接限速/静默限速"的行为模型 —— 加连接不会变快。
    """
    with _SHARED_LOCK:
        now = time.monotonic()
        start = max(now, _SHARED_PACING.get(key, now))
        _SHARED_PACING[key] = start + nbytes / max(1.0, rate)
        wait = start - now
    if wait > 0:
        time.sleep(wait)


def _enter(path: str) -> int:
    with _INFLIGHT_LOCK:
        _INFLIGHT[path] = _INFLIGHT.get(path, 0) + 1
        return _INFLIGHT[path]


def _leave(path: str) -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT[path] = max(0, _INFLIGHT.get(path, 0) - 1)


def max_inflight(path_contains: str) -> int:
    """观测到的并发峰值（测试用）。"""
    with _INFLIGHT_LOCK:
        return max((v for k, v in _INFLIGHT.items() if path_contains in k), default=0)


def _record_request(method: str, path: str, range_header: str | None) -> None:
    with _REQUESTS_LOCK:
        _REQUESTS.append({"method": method, "path": path, "range": range_header})


def recorded_requests(path_contains: str | None = None) -> list[dict]:
    with _REQUESTS_LOCK:
        items = list(_REQUESTS)
    if path_contains:
        items = [r for r in items if path_contains in r["path"]]
    return items


def clear_requests() -> None:
    with _REQUESTS_LOCK:
        _REQUESTS.clear()


def range_starts(path_contains: str) -> list[int]:
    """所有 Range 请求的起始偏移（升序无关）。"""
    starts = []
    for item in recorded_requests(path_contains):
        header = item.get("range")
        if not header or not header.startswith("bytes="):
            continue
        spec = header[6:].split("-", 1)[0]
        try:
            starts.append(int(spec))
        except ValueError:
            continue
    return starts


def make_payload(name: str, size: int) -> bytes:
    """确定性生成的测试数据（同 name+size 永远一致）。"""
    key = (name, size)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            seed = int.from_bytes(hashlib.sha256(name.encode("utf-8")).digest()[:8], "big")
            rng = random.Random(seed)
            # randbytes 一次性生成超大字节串会撞 CPython 的 C int 上限，
            # 所以按块拼接；块大小固定，保证同名同大小仍然完全可复现。
            block = 8 * 1024 * 1024
            if size <= block:
                cached = rng.randbytes(size)
            else:
                buffer = bytearray()
                while len(buffer) < size:
                    take = min(block, size - len(buffer))
                    buffer += rng.randbytes(take)
                cached = bytes(buffer)
            _CACHE[key] = cached
        return cached


def payload_sha256(name: str, size: int) -> str:
    return hashlib.sha256(make_payload(name, size)).hexdigest()


def _bump(key: str) -> int:
    with _COUNTS_LOCK:
        _COUNTS[key] = _COUNTS.get(key, 0) + 1
        return _COUNTS[key]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "DownloadXTestServer/1.0"

    # 安静一点
    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        pass

    def handle_one_request(self) -> None:
        # 客户端主动断开（下载完成/暂停）时，标准库会抛出 ConnectionError 并打印 traceback
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    # ------------------------------------------------------------------
    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        return path, parse_qs(parsed.query)

    def _parse_range(self, total: int) -> tuple[int, int] | None:
        raw = self.headers.get("Range")
        if not raw or not raw.startswith("bytes="):
            return None
        spec = raw[6:].strip()
        if "," in spec:  # 多区间不支持
            return None
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s == "":
                length = int(end_s)
                if length <= 0:
                    return None
                return max(0, total - length), total - 1
            start = int(start_s)
            end = int(end_s) if end_s else total - 1
        except ValueError:
            return None
        if start >= total:
            return (-1, -1)  # 触发 416
        return start, min(end, total - 1)

    def _send_common(self, extra: dict[str, str] | None = None) -> None:
        self.send_header("Accept-Ranges", "bytes")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)

    def _send_bytes(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self._handle(head_only=True)

    def do_GET(self) -> None:  # noqa: N802
        self._handle(head_only=False)

    def _handle(self, *, head_only: bool) -> None:
        path, query = self._route()
        _record_request(self.command or "GET", path, self.headers.get("Range"))
        parts = [p for p in path.split("/") if p]
        if not parts:
            self.send_error(404)
            return
        kind = parts[0]
        name = parts[1] if len(parts) > 1 else "payload.bin"
        size = int(query.get("size", ["262144"])[0])

        if kind == "missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if kind == "flaky":
            fails = int(query.get("fails", ["2"])[0])
            count = _bump(f"flaky:{name}")
            if count <= fails:
                body = b"temporary failure"
                self.send_response(503)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Retry-After", "1")
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)
                return

        if kind == "throttle" and not head_only:
            # 模拟按并发连接数限流的镜像站：同时超过 max 个下载就 429
            limit = int(query.get("max", ["2"])[0])
            key = f"throttle:{name}"
            current = _enter(key)
            self._throttle_key = key
            if current > limit:
                self._release_inflight()
                self.send_response(429)
                self.send_header("Content-Length", "0")
                self.send_header("Retry-After", "1")
                self.end_headers()
                return

        if kind == "sharedcap":
            # 聚合限速：多开连接不会变快（静默限速的模型），客户端必须自己发现这件事
            self._shared_key = f"sharedcap:{name}"
            self._shared_rate = float(query.get("rate", [str(512 * 1024)])[0])

        if kind == "hls":
            # /hls/<name>/master.m3u8 | playlist.m3u8 | segN.ts
            count = int(query.get("segments", ["6"])[0])
            seg_size = int(query.get("size", ["16384"])[0])
            delay = float(query.get("delay", ["0"])[0])
            base = f"/hls/{name}"
            leaf = parts[2] if len(parts) > 2 else ""
            if leaf == "master.m3u8":
                body = "\n".join([
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720",
                    f"{base}/playlist.m3u8?segments={count}&size={seg_size}&delay={delay}",
                    "",
                ])
                self._send_bytes(body.encode(), "application/vnd.apple.mpegurl")
                return
            if leaf == "playlist.m3u8":
                lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
                         "#EXT-X-MEDIA-SEQUENCE:0"]
                for i in range(count):
                    lines += ["#EXTINF:4.0,", f"{base}/seg{i}.ts?size={seg_size}&delay={delay}"]
                lines += ["#EXT-X-ENDLIST", ""]
                self._send_bytes("\n".join(lines).encode(), "application/vnd.apple.mpegurl")
                return
            if leaf.startswith("seg") and leaf.endswith(".ts"):
                index = int(leaf[3:-3])
                payload = make_payload(f"{name}-{index}", seg_size)
                if delay:
                    time.sleep(delay)
                self._send_bytes(payload, "video/mp2t")
                return
            self.send_error(404)
            return

        if kind == "disk":
            # /disk/<token>/<相对路径> —— 仅测试用：按相对路径读盘
            token = parts[1] if len(parts) > 1 else ""
            with _MOUNT_LOCK:
                root = _MOUNTS.get(token)
            if root is None:
                self.send_error(404)
                return
            rel = "/".join(parts[2:])
            target = (root / rel).resolve()
            if root not in target.parents or not target.is_file():
                self.send_error(404)
                return
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if target.suffix == ".m3u8":
                ctype = "application/vnd.apple.mpegurl"
            self._send_bytes(target.read_bytes(), ctype)
            return

        if kind == "truncate":
            count = _bump(f"truncate:{name}")
            payload = make_payload(name, size)
            if count == 1:
                # 上报全长，但只发一半就断开：客户端必须能从中断处续传
                self.send_response(200)
                self._send_common()
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if not head_only:
                    self.wfile.write(payload[: len(payload) // 2])
                    self.wfile.flush()
                    self.close_connection = True
                return
            # 第二次起按正常的支持 Range 的端点处理

        payload = make_payload(name, size)
        total = len(payload)

        if kind == "norange":
            # 明确忽略 Range：永远返回 200 + 全量
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(total))
            self.end_headers()
            if not head_only:
                self._write_body(payload, query)
            return

        if kind == "unknown":
            # 没有 Content-Length，只能靠连接关闭判断结束
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if not head_only:
                self._write_body(payload, query)
            return

        extra: dict[str, str] = {}
        if kind == "disposition":
            extra["Content-Disposition"] = (
                "attachment; filename=\"fallback.bin\"; "
                "filename*=UTF-8''%E4%B8%AD%E6%96%87%E6%96%87%E4%BB%B6.bin"
            )

        rng = self._parse_range(total)
        if rng == (-1, -1):
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if rng is not None:
            start, end = rng
            chunk = payload[start : end + 1]
            self.send_response(206)
            self._send_common(extra)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("ETag", f'"{hashlib.sha256(payload).hexdigest()[:16]}"')
            self.end_headers()
            if not head_only:
                self._write_body(chunk, query)
            return

        self.send_response(200)
        self._send_common(extra)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(total))
        self.send_header("ETag", f'"{hashlib.sha256(payload).hexdigest()[:16]}"')
        self.end_headers()
        if not head_only:
            self._write_body(payload, query)

    def _write_body(self, data: bytes, query: dict[str, list[str]]) -> None:
        delay = float(query.get("delay", ["0"])[0])
        chunk = int(query.get("chunk", ["32768"])[0])
        shared_key = getattr(self, "_shared_key", None)
        shared_rate = getattr(self, "_shared_rate", 0.0)
        try:
            for i in range(0, len(data), chunk):
                if shared_key:
                    _shared_wait(shared_key, min(chunk, len(data) - i), shared_rate)
                self.wfile.write(data[i : i + chunk])
                self.wfile.flush()
                if delay:
                    time.sleep(delay)
        except ConnectionError:
            # 客户端取消/暂停时主动断开是预期行为（Windows 会抛 ConnectionAborted）
            self.close_connection = True
        finally:
            self._release_inflight()

    def _release_inflight(self) -> None:
        key = getattr(self, "_throttle_key", None)
        if key:
            self._throttle_key = None
            _leave(key)


class TestServer:
    """上下文管理器：在后台线程里跑一个测试服务器。"""

    def __init__(self) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def reset(self) -> None:
        with _COUNTS_LOCK:
            _COUNTS.clear()
        with _INFLIGHT_LOCK:
            _INFLIGHT.clear()
        with _SHARED_LOCK:
            _SHARED_PACING.clear()
        clear_requests()

    def mount(self, directory: str | Path) -> str:
        """注册一个可被 /disk/<token>/ 访问的目录，返回 token。"""
        return register_mount(Path(directory))

    def __enter__(self) -> "TestServer":
        self.thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
