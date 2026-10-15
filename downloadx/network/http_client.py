"""HTTP 客户端：资源探测 + Range 流式下载。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Mapping

import aiohttp

from .connection_pool import ConnectionPool
from .retry import HttpStatusError
from .range_request import (
    ResourceInfo,
    build_range_header,
    content_length,
    guess_filename,
    parse_content_range,
    supports_ranges,
)

log = logging.getLogger("downloadx.http")


class HttpError(Exception):
    pass


class HttpClient:
    """只负责"取字节"，不关心分片、进度、落盘 —— 那些是 core / storage 的事。"""

    def __init__(self, pool: ConnectionPool, *, chunk_size: int = 64 * 1024) -> None:
        self.pool = pool
        self.chunk_size = max(4096, int(chunk_size))

    # ------------------------------------------------------------------
    # 资源探测
    # ------------------------------------------------------------------
    async def probe(self, url: str, headers: Mapping[str, str] | None = None) -> ResourceInfo:
        """探测资源大小与续传能力。

        顺序：HEAD ->（必要时）Range 探测。
        只下 1 个字节来确定服务端是否真的支持 Range，代价极小。
        """
        base_headers = {"Accept-Encoding": "identity"}
        if headers:
            base_headers.update(headers)

        size: int | None = None
        accept_ranges = False
        final_url = url
        content_type = ""
        etag = ""
        last_modified = ""
        filename: str | None = None
        status = 200
        head_ok = False

        #: 这些状态说明服务端不接受 HEAD，但仍可能支持 GET + Range
        head_fallback_status = {403, 405, 501}

        try:
            async with self.pool.acquire():
                async with self.pool.session.head(
                    url, headers=base_headers, allow_redirects=True
                ) as resp:
                    if resp.status < 400:
                        head_ok = True
                        status = resp.status
                        final_url = str(resp.url)
                        size = content_length(resp.headers)
                        accept_ranges = supports_ranges(resp.headers)
                        content_type = resp.headers.get("Content-Type", "")
                        etag = resp.headers.get("ETag", "")
                        last_modified = resp.headers.get("Last-Modified", "")
                        if resp.headers.get("Content-Disposition"):
                            filename = guess_filename(final_url, resp.headers)
                    elif resp.status not in head_fallback_status:
                        # 404 / 500 之类：直接判定资源不可用，绝不能把错误页当文件
                        raise HttpStatusError(resp.status, url, str(resp.reason or ""))
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            log.debug("HEAD 探测失败，将回退到 Range/GET 探测：%s", exc)

        # 大小未知或未声明支持 Range 时，用真实的 1 字节 Range 请求验证
        need_range_probe = (not head_ok) or size is None or not accept_ranges
        if need_range_probe:
            probe_headers = dict(base_headers)
            probe_headers["Range"] = build_range_header(0, 0)
            async with self.pool.acquire():
                async with self.pool.session.get(
                    url, headers=probe_headers, allow_redirects=True
                ) as resp:
                    final_url = str(resp.url)
                    status = resp.status
                    if resp.status >= 400 and resp.status != 416:
                        raise HttpStatusError(resp.status, url, str(resp.reason or ""))
                    content_type = content_type or resp.headers.get("Content-Type", "")
                    etag = etag or resp.headers.get("ETag", "")
                    last_modified = last_modified or resp.headers.get("Last-Modified", "")
                    if filename is None:
                        filename = guess_filename(final_url, resp.headers)
                    parsed = parse_content_range(resp.headers.get("Content-Range"))
                    if resp.status == 416:
                        # 空文件或区间越界：只在确认总长为 0 时按空文件处理
                        if parsed is not None and parsed[2] == 0:
                            size = 0
                            accept_ranges = True
                            return ResourceInfo(
                                url=url,
                                final_url=final_url,
                                size=0,
                                accept_ranges=True,
                                resumable=False,
                                filename=filename or guess_filename(final_url, None),
                                content_type=content_type,
                                etag=etag,
                                last_modified=last_modified,
                                status=resp.status,
                            )
                        raise HttpStatusError(416, url, "资源区间越界")
                    if resp.status == 206 and parsed is not None:
                        accept_ranges = True
                        if size is None:
                            size = parsed[2]
                    else:
                        # 服务端忽略了 Range（返回 200）：不支持续传
                        accept_ranges = False
                        if size is None:
                            size = content_length(resp.headers)

        if not filename:
            filename = guess_filename(final_url, None)

        return ResourceInfo(
            url=url,
            final_url=final_url,
            size=size,
            accept_ranges=accept_ranges,
            resumable=bool(accept_ranges and size and size > 0),
            filename=filename,
            content_type=content_type,
            etag=etag,
            last_modified=last_modified,
            status=status,
        )

    # ------------------------------------------------------------------
    # 流式下载
    # ------------------------------------------------------------------
    @asynccontextmanager
    async def stream(
        self,
        url: str,
        *,
        byte_range: tuple[int, int] | None = None,
        headers: Mapping[str, str] | None = None,
        method: str = "GET",
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        """发起请求并占用一个连接槽位，退出时自动释放。

        - 强制 ``Accept-Encoding: identity``：Range 偏移必须对应原始字节。
        - 206 表示服务端按 Range 返回；200 表示忽略 Range（调用方需处理）。
        """
        request_headers: dict[str, str] = {"Accept-Encoding": "identity"}
        if headers:
            request_headers.update(headers)
        if byte_range is not None:
            request_headers["Range"] = build_range_header(*byte_range)

        async with self.pool.acquire():
            resp = await self.pool.session.request(
                method, url, headers=request_headers, allow_redirects=True
            )
            try:
                yield resp
            finally:
                resp.release()

    async def iter_bytes(
        self, resp: aiohttp.ClientResponse, chunk_size: int | None = None
    ) -> AsyncIterator[bytes]:
        size = chunk_size or self.chunk_size
        async for chunk in resp.content.iter_chunked(size):
            if chunk:
                yield chunk

    async def fetch_text(
        self, url: str, *, headers: Mapping[str, str] | None = None, limit: int = 65536
    ) -> tuple[int, str]:
        async with self.stream(url, headers=headers) as resp:
            data = await resp.content.read(limit)
            return resp.status, data.decode("utf-8", errors="replace")

    @staticmethod
    def validate_range_response(
        resp: aiohttp.ClientResponse, requested: tuple[int, int] | None
    ) -> None:
        """校验服务端是否按 Range 返回；忽略 Range 会导致数据错位，必须早失败。"""
        if requested is None:
            return
        if resp.status == 206:
            parsed = parse_content_range(resp.headers.get("Content-Range"))
            if parsed is None:
                raise HttpError("服务端返回 206 但缺少 Content-Range，无法确认偏移")
            if parsed[0] != requested[0]:
                raise HttpError(
                    f"服务端返回的起始偏移 {parsed[0]} 与请求 {requested[0]} 不一致"
                )
            return
        if resp.status == 200:
            raise HttpError("服务端忽略了 Range 请求，续传不可用")
        raise HttpError(f"意外的响应状态: {resp.status}")
