"""Range 请求与资源探测的数据结构与工具函数。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import unquote, urlparse

_CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)
_CONTENT_DISP_UTF8_RE = re.compile(r"filename\*\s*=\s*UTF-8''([^;]+)", re.IGNORECASE)
_CONTENT_DISP_RE = re.compile(r'filename\s*=\s*"?([^";]+)"?', re.IGNORECASE)

# Windows 文件名非法字符（POSIX 允许，但跨平台设计按 Windows 收紧）
_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}


@dataclass
class ResourceInfo:
    """一次探测得到的远端资源画像。"""

    url: str
    final_url: str
    size: int | None
    accept_ranges: bool
    resumable: bool
    filename: str
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""
    status: int = 200
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "size": self.size,
            "accept_ranges": self.accept_ranges,
            "resumable": self.resumable,
            "filename": self.filename,
            "content_type": self.content_type,
            "etag": self.etag,
            "last_modified": self.last_modified,
            "status": self.status,
        }


def build_range_header(start: int, end: int) -> str:
    if start < 0 or end < start:
        raise ValueError(f"非法区间: {start}-{end}")
    return f"bytes={start}-{end}"


def header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    """大小写不敏感地取响应头。

    aiohttp 的 CIMultiDict 本来就不区分大小写，但普通 dict（测试、外部调用）会，
    所以这里做一层兜底，避免因为 "Accept-Ranges" / "accept-ranges" 漏判续传能力。
    """
    if not headers:
        return None
    direct = headers.get(name)
    if direct is not None:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if str(key).lower() == lowered:
            return value
    return None


def parse_content_range(value: str | None) -> tuple[int, int, int | None] | None:
    """解析 ``Content-Range: bytes 0-1023/4096`` -> (start, end, total)。"""
    if not value:
        return None
    match = _CONTENT_RANGE_RE.search(value)
    if not match:
        return None
    start, end, total = match.group(1), match.group(2), match.group(3)
    return int(start), int(end), (None if total == "*" else int(total))


def content_range_total(value: str | None) -> int | None:
    parsed = parse_content_range(value)
    return None if parsed is None else parsed[2]


def supports_ranges(headers: Mapping[str, str]) -> bool:
    return (header_value(headers, "Accept-Ranges") or "").strip().lower() == "bytes"


def content_length(headers: Mapping[str, str]) -> int | None:
    raw = header_value(headers, "Content-Length")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def filename_from_headers(headers: Mapping[str, str]) -> str | None:
    disposition = header_value(headers, "Content-Disposition") or ""
    if not disposition:
        return None
    match = _CONTENT_DISP_UTF8_RE.search(disposition)
    if match:
        return sanitize_filename(unquote(match.group(1).strip()))
    match = _CONTENT_DISP_RE.search(disposition)
    if match:
        return sanitize_filename(match.group(1).strip())
    return None


def filename_from_url(url: str) -> str | None:
    path = urlparse(url).path
    if not path or path.endswith("/"):
        return None
    name = unquote(path.rsplit("/", 1)[-1])
    return sanitize_filename(name) if name else None


def sanitize_filename(name: str, fallback: str = "download") -> str:
    """清洗为 Windows / POSIX 都合法的文件名。"""
    name = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    name = name.replace("\r", "").replace("\n", "")
    name = name.strip(" .")
    if not name:
        return fallback
    stem, dot, suffix = name.partition(".")
    if stem.upper() in _RESERVED_NAMES:
        name = f"{stem}_{dot}{suffix}" if dot else f"{stem}_"
    # Windows 路径总长限制（260）下留出余量，超长名截断
    if len(name) > 180:
        stem, dot, suffix = name.rpartition(".")
        if dot and len(suffix) <= 16:
            name = stem[: 180 - len(suffix) - 1] + "." + suffix
        else:
            name = name[:180]
    return name or fallback


def guess_filename(url: str, headers: Mapping[str, str] | None = None) -> str:
    """优先级：Content-Disposition > URL path > 兜底名。"""
    if headers:
        from_header = filename_from_headers(headers)
        if from_header:
            return from_header
    from_url = filename_from_url(url)
    if from_url:
        return from_url
    host = urlparse(url).netloc or "download"
    return sanitize_filename(f"{host}-download")
