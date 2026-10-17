"""下载执行器：把一个任务从"排队"跑到"完成/暂停/失败"。

关键设计：

* **续传靠"偏移"而不是"续传 API"** —— 每个分片失败后重新用
  ``Range: bytes=<已收字节>-<结束>`` 发起请求，天然就是断点续传。
* **暂停/取消用事件 + 协作式检查**，在每个 chunk 边界检查，不丢已写数据。
* **校验在改名之前** —— 只有校验通过，``.dxpart`` 才会变成最终文件。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from ..config import Config
from ..network import dash, hls
from ..network.http_client import HttpClient, HttpError
from ..network.retry import HttpStatusError, RetryExhausted, RetryPolicy, is_retryable, retry_async
from ..storage.checksum import sha256_file
from ..storage.file_writer import FileWriter, finalize_part
from ..storage.segment_store import SegmentStore
from .download_task import DownloadTask
from .events import EventBus
from .metrics import SpeedMeter
from .segment import Segment
from .state_machine import TaskStatus

log = logging.getLogger("downloadx.worker")


class PauseRequested(Exception):
    """用户暂停（可续传）。"""


class CancelRequested(Exception):
    """用户取消（保留 .dxpart，可续传）。"""


class ChecksumMismatch(Exception):
    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"SHA-256 校验失败：期望 {expected[:16]}… 实际 {actual[:16]}…")
        self.expected = expected
        self.actual = actual


class AdaptiveLimiter:
    """可动态收紧的并发闸门。

    服务端（尤其是国内镜像 / CDN）会按并发连接数限流：连接开得越多越容易被
    429/503 打回。这里的做法是**让运行中的分片自然结束，把新分片的并发上限降下来**，
    而不是粗暴取消正在传的连接（那会丢掉已经收了一半的数据）。
    """

    __slots__ = ("_limit", "_min", "_in_use", "_cond")

    def __init__(self, limit: int, minimum: int = 1) -> None:
        self._limit = max(1, int(limit))
        self._min = max(1, min(int(minimum), self._limit))
        self._in_use = 0
        self._cond = asyncio.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def in_use(self) -> int:
        return self._in_use

    async def acquire(self) -> None:
        async with self._cond:
            await self._cond.wait_for(lambda: self._in_use < self._limit)
            self._in_use += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_use = max(0, self._in_use - 1)
            self._cond.notify_all()

    async def reduce(self) -> int:
        """收紧一个名额，返回新的上限（已经到下限则不变）。"""
        async with self._cond:
            if self._limit > self._min:
                self._limit -= 1
            self._cond.notify_all()
            return self._limit

    async def weaken_to(self, value: int) -> int:
        async with self._cond:
            self._limit = max(self._min, min(self._limit, int(value)))
            self._cond.notify_all()
            return self._limit


@dataclass
class WorkerControl:
    """外部控制本轮下载的信号。"""

    pause: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def request_pause(self) -> None:
        self.pause.set()

    def request_cancel(self) -> None:
        self.cancel.set()

    def reset(self) -> None:
        self.pause.clear()
        self.cancel.clear()

    def check(self) -> None:
        if self.cancel.is_set():
            raise CancelRequested
        if self.pause.is_set():
            raise PauseRequested


class DownloadWorker:
    """一次"执行轮次"。同一个任务可以反复执行（暂停后恢复、失败后重试）。"""

    def __init__(
        self,
        task: DownloadTask,
        *,
        config: Config,
        http: HttpClient,
        bus: EventBus,
        control: WorkerControl,
        segment_store: SegmentStore | None = None,
        bandwidth=None,
        checkpointer=None,
        on_progress=None,
        speed_window: float = 5.0,
    ) -> None:
        self.task = task
        self.config = config
        self.http = http
        self.bus = bus
        self.control = control
        self.segment_store = segment_store or SegmentStore(config)
        self.bandwidth = bandwidth
        self.checkpointer = checkpointer
        self.on_progress = on_progress
        self.meter = SpeedMeter(window=speed_window)
        self.retry_policy = RetryPolicy.from_config(config)
        self.writer: FileWriter | None = None
        self._last_emit = 0.0
        self._emit_interval = 0.3
        self._run_started = 0.0
        self._segment_sem: AdaptiveLimiter | None = None

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------
    async def run(self) -> TaskStatus:
        task = self.task
        self._run_started = time.monotonic()
        self.meter.reset()
        self._last_emit = 0.0
        try:
            await self._prepare()

            if task.mode in ("hls", "dash"):
                await self._run_stream()
            else:
                await self._open_writer()
                if task.mode == "streaming":
                    await self._run_streaming()
                else:
                    await self._run_segmented()

            await self._finalize()
            return task.status

        except PauseRequested:
            task.sync_progress()
            await self._close_writer()
            self._emit("task_paused", percent=task.percent, reason="用户暂停")
            return task.status

        except CancelRequested:
            task.sync_progress()
            await self._close_writer()
            self._emit("task_canceled", percent=task.percent, reason="用户取消")
            return task.status

        except asyncio.CancelledError:
            task.sync_progress()
            await self._close_writer()
            raise

        except ChecksumMismatch as exc:
            await self._close_writer()
            task.error = str(exc)
            task.error_code = "checksum_mismatch"
            # 校验失败说明数据不可信，删掉重来才能正确
            self._discard_part()
            self._emit("task_failed", error=str(exc), code="checksum_mismatch")
            raise

        except Exception as exc:
            task.sync_progress()
            await self._close_writer()
            task.error = self.describe_error(exc)
            task.error_code = type(exc).__name__
            self._emit("task_failed", error=task.error, code=task.error_code)
            raise

        finally:
            task.elapsed += max(0.0, time.monotonic() - self._run_started)
            task.sync_progress()
            task.updated_at = time.time()
            if self.checkpointer is not None:
                self.checkpointer.touch(task.id)

    # ------------------------------------------------------------------
    # 准备：探测 + 分片规划（含远端变化检测）
    # ------------------------------------------------------------------
    async def _prepare(self) -> None:
        task = self.task
        if hls.looks_like_hls(task.url, task.content_type):
            await self._prepare_hls()
            return
        if dash.looks_like_dash(task.url, task.content_type):
            await self._prepare_dash()
            return
        known_plan = bool(task.segments) and task.mode != "streaming"

        if not known_plan:
            await self._probe_and_plan()
            return

        # 恢复路径：先确认远端资源没有变化，否则续传会拼出坏文件
        if task.resumable:
            try:
                info = await self.http.probe(task.url, task.headers)
            except Exception as exc:
                log.debug("恢复前探测失败，按原计划继续：%s", exc)
                return
            changed_reason = self._detect_remote_change(info)
            if changed_reason:
                self._emit("task_invalidated", reason=changed_reason)
                self._discard_part()
                task.segments = []
                task.total_size = None
                task.downloaded_size = 0
                task.sha256 = None
                task.verified = None
                await self._probe_and_plan(info=info)

    def _detect_remote_change(self, info) -> str | None:
        task = self.task
        if info.size is not None and task.total_size is not None and info.size != task.total_size:
            return f"远端文件大小变化：{task.total_size} -> {info.size}"
        if task.etag and info.etag and task.etag != info.etag:
            return "远端 ETag 变化，文件已被替换"
        if task.last_modified and info.last_modified and task.last_modified != info.last_modified:
            return "远端 Last-Modified 变化，文件已被替换"
        if not info.resumable:
            return "服务端不再支持 Range，需重新下载"
        return None

    async def _probe_and_plan(self, info=None) -> None:
        task = self.task
        if info is None:
            info = await self._probe_with_retry()

        task.final_url = info.final_url
        task.etag = info.etag or ""
        task.last_modified = info.last_modified or ""
        task.content_type = info.content_type or ""
        task.accept_ranges = info.accept_ranges
        task.resumable = info.resumable

        # 只在文件名还是"猜测值"时才跟随服务端，用户显式指定的名字要尊重
        if info.filename and not task.filename_locked:
            if Path(task.save_path).name != info.filename:
                task.filename = info.filename
                task.save_path = str(Path(task.save_path).parent / info.filename)

        plan = self.segment_store.plan(
            total_size=info.size,
            connections=task.connections,
            resumable=info.resumable,
        )
        task.mode = plan.mode
        task.total_size = info.size
        task.segments = plan.segments
        task.plan_reason = plan.reason
        task.downloaded_size = 0

        # 记录远端元信息，供恢复时比对
        self._emit(
            "task_probed",
            size=info.size,
            resumable=info.resumable,
            mode=plan.mode,
            plan_reason=plan.reason,
            final_url=info.final_url,
            content_type=info.content_type,
        )

    async def _probe_with_retry(self):
        """探测也要重试：503/超时这类临时故障不该直接判任务失败。"""

        async def call():
            self.control.check()
            return await self.http.probe(self.task.url, self.task.headers)

        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            self._emit(
                "probe_retry",
                attempt=attempt,
                delay=round(delay, 2),
                error=self.describe_error(exc),
            )

        try:
            return await retry_async(call, self.retry_policy, on_retry=on_retry)
        except RetryExhausted as exc:
            raise exc.last_error from exc

    # ------------------------------------------------------------------
    # 写入器
    # ------------------------------------------------------------------
    async def _open_writer(self) -> None:
        task = self.task
        if task.mode == "streaming":
            self.writer = FileWriter(task.part_path, mode="append")
        else:
            if not task.total_size:
                raise HttpError("分段模式缺少总大小")
            self.writer = FileWriter(task.part_path, mode="ranged", size=task.total_size)
        self.writer.open()

    async def _close_writer(self) -> None:
        writer, self.writer = self.writer, None
        if writer is not None:
            try:
                await writer.close()
            except Exception as exc:
                log.debug("关闭写入器失败：%s", exc)

    def _discard_part(self) -> None:
        try:
            Path(self.task.part_path).unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # HLS（.m3u8）：分片下载 + 断点续传 + 合并
    # ------------------------------------------------------------------
    async def _prepare_hls(self) -> None:
        task = self.task
        text, final_url = await self._fetch_playlist(task.url)
        if hls.is_master(text):
            variants = hls.parse_master(text, final_url)
            variant = hls.pick_variant(variants, self.config.stream_max_height or None)
            text, final_url = await self._fetch_playlist(variant.uri)
            self._emit("hls_variant", variant=variant.label, uri=variant.uri)

        playlist = hls.parse_media(text, final_url)
        if playlist.is_live:
            raise hls.HlsError("直播流（没有 EXT-X-ENDLIST）不在支持范围")

        task.mode = "hls"
        task.final_url = final_url
        task.resumable = True
        task.stream_tracks = [
            {
                "kind": "media",
                "init": playlist.init_uri or "",
                "urls": [s.uri for s in playlist.segments],
                "done": [],
            }
        ]
        task.stream_count = len(playlist.segments)
        task.total_size = None
        minutes = f"，约 {playlist.duration / 60:.1f} 分钟" if playlist.duration else ""
        task.plan_reason = f"HLS：{task.stream_count} 个分片{minutes}"

        # 输出名：.m3u8 -> .mp4（有 ffmpeg）或 .ts
        if not task.filename_locked:
            use_mp4 = bool(self.config.stream_remux and shutil.which("ffmpeg"))
            stem = Path(task.filename).stem or "video"
            new_name = stem + (".mp4" if use_mp4 else ".ts")
            if Path(task.save_path).name != new_name:
                task.filename = new_name
                task.save_path = str(Path(task.save_path).parent / new_name)

        self._emit(
            "task_probed",
            mode="hls",
            segments=task.stream_count,
            final_url=final_url,
            resumable=True,
            plan_reason=task.plan_reason,
        )

    async def _prepare_dash(self) -> None:
        task = self.task
        text, final_url = await self._fetch_playlist(task.url)
        tracks = dash.parse(text, final_url, self.config.stream_max_height or None)

        task.mode = "dash"
        task.final_url = final_url
        task.resumable = True
        task.stream_tracks = [
            {"kind": track.kind, "init": track.init, "urls": list(track.urls), "done": []}
            for track in tracks
        ]
        task.stream_count = sum(len(track.urls) for track in tracks)
        task.total_size = None
        labels = " + ".join(track.label for track in tracks)
        task.plan_reason = f"DASH：{labels}，共 {task.stream_count} 个分片"

        if not task.filename_locked:
            stem = Path(task.filename).stem or "video"
            new_name = stem + ".mp4"
            if Path(task.save_path).name != new_name:
                task.filename = new_name
                task.save_path = str(Path(task.save_path).parent / new_name)

        self._emit(
            "task_probed",
            mode="dash",
            segments=task.stream_count,
            final_url=final_url,
            resumable=True,
            plan_reason=task.plan_reason,
        )

    async def _fetch_playlist(self, url: str) -> tuple[str, str]:
        async def call() -> tuple[str, str]:
            self.control.check()
            async with self.http.stream(url, headers=self.task.headers) as resp:
                if resp.status >= 400:
                    raise HttpStatusError(resp.status, url, str(resp.reason or ""))
                data = await resp.content.read()
            if not data:
                raise HttpError("播放列表为空")
            return data.decode("utf-8", "replace"), str(resp.url)

        try:
            return await retry_async(call, self.retry_policy)
        except RetryExhausted as exc:
            raise exc.last_error from exc

    async def _run_stream(self) -> None:
        """HLS / DASH 共用的分片下载循环：按轨展开、并发拉取、可续传。"""
        task = self.task
        tracks = task.stream_tracks
        if not tracks:
            raise HttpError("没有可下载的流轨")

        seg_dir = Path(task.part_path + ".segs")
        seg_dir.mkdir(parents=True, exist_ok=True)
        total = sum(len(track["urls"]) for track in tracks)
        task.stream_count = total

        # 以磁盘实物为准：已有分片文件就视为完成（元数据落后也不重下）
        for track in tracks:
            kind = track.get("kind") or "media"
            done = {i for i in (track.get("done") or []) if 0 <= i < len(track["urls"])}
            for i in range(len(track["urls"])):
                if self._segment_path(seg_dir, kind, i).exists():
                    done.add(i)
            track["done"] = sorted(done)

        limit = max(1, min(int(task.connections), 8))
        self._emit(
            "task_started",
            mode=task.mode,
            connections=limit,
            total_size=None,
            segments=total,
            done=task.stream_done_count,
        )

        for track in tracks:
            init = track.get("init") or ""
            if init:
                path = self._init_path(seg_dir, track)
                if not path.exists():
                    data = await self._fetch_stream_bytes(init)
                    await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
                    await asyncio.to_thread(path.write_bytes, data)
                    self._on_bytes(len(data))

        pending = [
            (track_index, index)
            for track_index, track in enumerate(tracks)
            for index in range(len(track["urls"]))
            if index not in set(track["done"])
        ]
        queue: asyncio.Queue[tuple[int, int]] = asyncio.Queue()
        for item in pending:
            queue.put_nowait(item)

        async def pool_worker() -> None:
            while True:
                try:
                    track_index, index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                self.control.check()
                recorded = await self._fetch_stream_segment(track_index, index, seg_dir)
                track = task.stream_tracks[track_index]
                if recorded:
                    track["done"] = sorted(set(track.get("done") or []) | {index})
                self._touch()
                self._emit(
                    "stream_segment_done",
                    track=track.get("kind"),
                    index=index,
                    done=task.stream_done_count,
                    total=task.stream_count,
                    percent=task.percent,
                )

        workers = [
            asyncio.create_task(pool_worker())
            for _ in range(min(limit, max(1, len(pending))))
        ]
        try:
            await asyncio.gather(*workers)
        except BaseException:
            for task_obj in workers:
                task_obj.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            raise

        await self._assemble_stream(seg_dir)

    @staticmethod
    def _track_dir(seg_dir: Path, kind: str) -> Path:
        return seg_dir / (kind or "media")

    def _segment_path(self, seg_dir: Path, kind: str, index: int) -> Path:
        return self._track_dir(seg_dir, kind) / f"{index:05d}.seg"

    def _init_path(self, seg_dir: Path, track: dict) -> Path:
        return self._track_dir(seg_dir, track.get("kind") or "media") / "init.seg"

    async def _fetch_stream_bytes(self, url: str) -> bytes:
        """单个分片/初始化段：带重试地整段读取（分片通常几 MB，不适合 Range 续传）。"""
        attempts = 0
        while True:
            attempts += 1
            try:
                async with self.http.stream(url, headers=self.task.headers) as resp:
                    if resp.status >= 400:
                        raise HttpStatusError(resp.status, url, str(resp.reason or ""))
                    data = await resp.content.read()
                if not data:
                    raise HttpError("分片为空")
                return data
            except (PauseRequested, CancelRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                if attempts >= self.retry_policy.max_attempts or not is_retryable(exc):
                    raise
                await asyncio.sleep(self.retry_policy.delay_for(attempts))

    async def _fetch_stream_segment(self, track_index: int, index: int, seg_dir: Path) -> bool:
        """下载一个分片；返回 False 表示"尾部分片不存在"，已从计划里摘掉。"""
        task = self.task
        track = task.stream_tracks[track_index]
        url = track["urls"][index]
        try:
            data = await self._fetch_stream_bytes(url)
        except HttpStatusError as exc:
            if exc.status in (404, 416) and index == len(track["urls"]) - 1:
                # 清单按"时长/分段时长"推算出的尾段经常并不存在（VOD 常见），
                # 把它从计划里摘掉即可，不该让整个任务失败。
                track["urls"].pop()
                track["done"] = [i for i in (track.get("done") or []) if i < len(track["urls"])]
                task.stream_count = sum(len(t["urls"]) for t in task.stream_tracks)
                self._emit("stream_segment_missing", track=track.get("kind"), index=index)
                return False
            raise
        if self.bandwidth is not None:
            await self.bandwidth.acquire(len(data), task.id)
        target = self._segment_path(seg_dir, track.get("kind") or "media", index)
        await asyncio.to_thread(target.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(target.write_bytes, data)
        self._on_bytes(len(data))
        return True

    async def _assemble_stream(self, seg_dir: Path) -> None:
        task = self.task
        target = Path(task.part_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # 注意：target 是 .dxpart 临时文件，容器要按**最终文件名**判断
        final_is_mp4 = task.save_path.lower().endswith(".mp4")
        tracks = task.stream_tracks

        def build(track: dict) -> Path:
            kind = track.get("kind") or "media"
            out = seg_dir / f"track-{kind}.bin"
            with out.open("wb") as fh:
                init = self._init_path(seg_dir, track)
                if init.exists():
                    with init.open("rb") as src:
                        shutil.copyfileobj(src, fh, 1024 * 1024)
                for i in range(len(track["urls"])):
                    with self._segment_path(seg_dir, kind, i).open("rb") as src:
                        shutil.copyfileobj(src, fh, 1024 * 1024)
            return out

        built: dict[str, Path] = {}
        for track in tracks:
            built[track.get("kind") or "media"] = await asyncio.to_thread(build, track)

        ffmpeg = shutil.which("ffmpeg")
        if len(built) > 1:
            if ffmpeg is None:
                raise HttpError("音视频是分开的流，合并需要 ffmpeg")
            video = built.get("video") or next(iter(built.values()))
            audio = built.get("audio")
            command = [ffmpeg, "-y", "-loglevel", "error", "-i", str(video)]
            if audio is not None:
                command += ["-i", str(audio)]
            command += ["-c", "copy", "-f", "mp4", "-movflags", "+faststart", str(target)]
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await proc.wait()
            if code != 0 or not target.exists():
                raise HttpError(f"ffmpeg 合并失败（退出码 {code}）")
        elif final_is_mp4 and self.config.stream_remux:
            if ffmpeg is None:
                raise HttpError("合并成 mp4 需要 ffmpeg；装 ffmpeg 或把输出名改成 .ts")
            single = next(iter(built.values()))
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, "-y", "-loglevel", "error", "-i", str(single),
                "-c", "copy", "-f", "mp4", "-movflags", "+faststart", str(target),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            code = await proc.wait()
            if code != 0 or not target.exists():
                raise HttpError(f"ffmpeg 合并失败（退出码 {code}）")
        else:
            os.replace(next(iter(built.values())), target)

        shutil.rmtree(seg_dir, ignore_errors=True)
        self._emit("stream_assembled", segments=task.stream_count, size=target.stat().st_size)

    # ------------------------------------------------------------------
    # 多连接分片下载
    # ------------------------------------------------------------------
    async def _run_segmented(self) -> None:
        task = self.task
        pending = [s for s in task.segments if not s.is_done]
        if not pending:
            return

        limit = max(1, min(int(task.connections), len(pending), self.config.max_connections_per_task))
        self._segment_sem = AdaptiveLimiter(limit)
        task.effective_connections = limit
        self._emit(
            "task_started",
            mode=task.mode,
            connections=limit,
            total_size=task.total_size,
            resumable=task.resumable,
        )

        try:
            if not self._should_calibrate(pending, limit):
                await self._gather_segments([self._segment_task(s) for s in pending])
                return
            await self._run_with_calibration(pending, limit)
        finally:
            self._segment_sem = None

    # ------------------------------------------------------------------
    # 并发自校准：先用 1 连接量一段，再放开量一段，没提升就退回单连接
    # ------------------------------------------------------------------
    def _should_calibrate(self, pending: list[Segment], limit: int) -> bool:
        cfg = self.config
        if not cfg.ramp_enabled or limit < 2 or len(pending) < 2:
            return False
        # 小文件不值得为测量浪费时间（大文件才吃得住这 2~6 秒）
        return sum(s.remaining for s in pending) >= cfg.ramp_min_total_size

    async def _run_with_calibration(self, pending: list[Segment], limit: int) -> None:
        """自校准的并发加压。

        为什么必须在**同一个任务内、相邻几秒**做这个对比：同一台机器上同一份配置的吞吐
        会在一次会话里漂移几十倍（实测 conn=1 从 2.89s 漂到 112s 传同一个文件），
        任何绝对阈值都不可靠；只有"刚刚单连接是多少、现在多连接是多少"才有意义。
        """
        cfg = self.config
        first, rest = pending[0], pending[1:]
        tasks: list[asyncio.Task] = []
        try:
            self._emit(
                "task_ramp_started",
                connections=limit,
                warmup_seconds=cfg.ramp_warmup_seconds,
                probe_seconds=cfg.ramp_probe_seconds,
            )

            tasks.append(self._segment_task(first))
            single_rate = await self._measure_single_rate(first, tasks[0])
            tasks.extend(self._segment_task(s) for s in rest)
            if single_rate is None:
                # 单连接就在测量窗口内把这片下完了：没有可比较的信号，不做任何降级
                await self._gather_segments(tasks)
                return

            ramped_rate = await self._measure_ramped_rate(tasks)
            if ramped_rate is not None:
                await self._apply_calibration(single_rate, ramped_rate, limit)
            await self._gather_segments(tasks)
        except BaseException:
            # 测量阶段被打断（暂停/取消/异常）时，已经启动的分片必须收干净再往外抛。
            # 实测：暂停标志本身也会让分片在下一个 chunk 边界自行退出，所以这不是"修一个
            # 观察得到的 bug"，而是消除一个结构性隐患 —— worker 绝不能在子任务还活着时返回，
            # 否则下一轮运行会和残留任务争抢同一段偏移。
            for item in tasks:
                item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _measure_single_rate(self, segment: Segment, task_obj: asyncio.Task) -> float | None:
        """单连接阶段的实测吞吐（bytes/s）。返回 None 表示测不出来。

        先"预热"再采样：冷连接的前几百毫秒全花在 DNS/TLS/慢启动上，直接把它算进速率
        会把基线压低好几倍（真机实测第一版把基线量成了 31 KB/s，而放开后是 3.6 MB/s，
        纯属把冷启动当成了"单连接很慢"）。所以开头的一段只用来热身，不进分子分母。
        """
        cfg = self.config
        budget_bytes = max(1, cfg.ramp_warmup_bytes)
        budget_seconds = max(0.2, cfg.ramp_warmup_seconds)
        prime_bytes = max(1, budget_bytes // 4)
        prime_seconds = max(0.2, budget_seconds * 0.25)

        warm_start = time.monotonic()
        start_mark = self.task.downloaded_size
        while True:
            self.control.check()
            if task_obj.done() or segment.is_done:
                return None
            moved = self.task.downloaded_size - start_mark
            elapsed = time.monotonic() - warm_start
            if moved >= prime_bytes or elapsed >= prime_seconds:
                break
            await asyncio.sleep(0.05)

        mark = self.task.downloaded_size
        measured_from = time.monotonic()
        # 再短的窗口也可能"达标"（比如字节阈值很小），但几十毫秒的样本在 Windows 上
        # 抖动足以翻转判定，所以给测量窗口一个下限。
        min_window = max(0.3, budget_seconds * 0.25)
        while True:
            self.control.check()
            if task_obj.done() or segment.is_done:
                break
            moved_total = self.task.downloaded_size - start_mark
            window = time.monotonic() - measured_from
            reached = moved_total >= budget_bytes or (time.monotonic() - warm_start) >= budget_seconds
            if reached and window >= min_window:
                break
            await asyncio.sleep(0.05)

        window = time.monotonic() - measured_from
        moved = self.task.downloaded_size - mark
        if moved <= 0 or window <= 0:
            return None
        return moved / window

    async def _measure_ramped_rate(self, tasks: list[asyncio.Task]) -> float | None:
        """放开并发后的聚合吞吐（bytes/s）。"""
        cfg = self.config
        mark = self.task.downloaded_size
        started = time.monotonic()
        await asyncio.wait(
            tasks, timeout=cfg.ramp_probe_seconds, return_when=asyncio.FIRST_EXCEPTION
        )
        elapsed = time.monotonic() - started
        moved = self.task.downloaded_size - mark
        # 窗口被打断（提前出错/提前结束）时不拿短样本下结论
        if moved <= 0 or elapsed < cfg.ramp_probe_seconds * 0.6:
            return None
        return moved / elapsed

    async def _apply_calibration(self, single_rate: float, ramped_rate: float, limit: int) -> None:
        limiter = self._segment_sem
        if limiter is None or single_rate <= 0:
            return
        gain = ramped_rate / single_rate
        if gain >= self.config.ramp_gain_threshold:
            self._emit(
                "task_ramp_kept",
                connections=limit,
                baseline=round(single_rate),
                ramped=round(ramped_rate),
                gain=round(gain, 3),
            )
            return
        after = await limiter.weaken_to(1)
        self.task.effective_connections = after
        self.task.plan_reason = (
            f"{self.task.plan_reason}；并发自校准 {limit}→{after}（增益 {gain:.2f}x）"
        ).strip("；")
        self._emit(
            "task_ramp_down",
            connections=after,
            previous=limit,
            baseline=round(single_rate),
            ramped=round(ramped_rate),
            gain=round(gain, 3),
            reason="加连接没有换来吞吐，退回单连接",
        )

    # ------------------------------------------------------------------
    # 分片任务编排
    # ------------------------------------------------------------------
    def _segment_task(self, segment: Segment) -> asyncio.Task:
        return asyncio.create_task(
            self._segment_loop(segment), name=f"dx-seg-{self.task.id}-{segment.index}"
        )

    @staticmethod
    async def _gather_segments(tasks: list[asyncio.Task]) -> None:
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for item in tasks:
                item.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    async def _segment_loop(self, segment: Segment) -> None:
        assert self._segment_sem is not None
        while not segment.is_done:
            self.control.check()
            segment.mark_active()
            self.task.attempts += 1
            # 名额按"每次尝试"申请：退避等待期间不占连接，让其他分片跑满带宽
            await self._segment_sem.acquire()
            try:
                await self._fetch_segment(segment)
            except (PauseRequested, CancelRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                segment.mark_failed(self.describe_error(exc))
                self._touch()
                if self._is_throttle(exc):
                    await self._back_off_connections(segment, exc)
                if not is_retryable(exc) or segment.attempts >= self.retry_policy.max_attempts:
                    raise
                delay = self.retry_policy.delay_for(segment.attempts)
                self._emit(
                    "segment_retry",
                    index=segment.index,
                    attempt=segment.attempts,
                    delay=round(delay, 2),
                    error=segment.error,
                )
                await asyncio.sleep(delay)
            else:
                segment.mark_done()
                self._touch()
                self._emit(
                    "segment_done",
                    index=segment.index,
                    received=segment.received,
                    percent=self.task.percent,
                )
            finally:
                await self._segment_sem.release()

    #: 服务端明确在"嫌我们连接太多"的状态码
    THROTTLE_STATUS = frozenset({429, 503, 504})

    @classmethod
    def _is_throttle(cls, exc: BaseException) -> bool:
        return isinstance(exc, HttpStatusError) and exc.status in cls.THROTTLE_STATUS

    async def _back_off_connections(self, segment: Segment, exc: BaseException) -> None:
        """被限流：收紧并发上限，并把这件事报出去（UI 能看到 8 -> 2）。"""
        limiter = self._segment_sem
        if limiter is None:
            return
        before = limiter.limit
        after = await limiter.reduce()
        self.task.effective_connections = after
        if after != before:
            self._emit(
                "task_throttled",
                index=segment.index,
                connections=after,
                previous=before,
                reason=self.describe_error(exc),
            )

    async def _fetch_segment(self, segment: Segment) -> None:
        task = self.task
        writer = self.writer
        if writer is None:
            raise RuntimeError("写入器已关闭")

        # 服务端不支持 Range 时，任何"已收到一半"的进度都不可信：整段重来。
        resumable = bool(task.resumable and task.mode == "segmented")
        if not resumable:
            segment.reset()
        start = segment.next_offset
        requested = (start, segment.end) if resumable else None
        async with self.http.stream(
            task.url, byte_range=requested, headers=task.headers
        ) as resp:
            if resp.status == 416:
                if segment.is_done:
                    return
                raise HttpStatusError(416, task.url, "请求区间超出文件范围")
            if resp.status >= 400:
                raise HttpStatusError(resp.status, task.url, str(resp.reason or ""))
            if resp.status == 206 and not resumable:
                raise HttpError("服务端返回了未请求的分段响应")
            if resp.status == 200 and start > 0:
                raise HttpError("服务端忽略了 Range 请求，无法续传")
            self.http.validate_range_response(resp, requested)

            offset = start
            async for chunk in self.http.iter_bytes(resp):
                self.control.check()
                want = segment.remaining
                if want <= 0:
                    break
                if len(chunk) > want:
                    # 服务端多给了字节：截断，绝不越过本分片边界污染邻居
                    chunk = chunk[:want]
                if self.bandwidth is not None:
                    await self.bandwidth.acquire(len(chunk), task.id)
                await writer.write_at(offset, chunk)
                offset += len(chunk)
                segment.received += len(chunk)
                self._on_bytes(len(chunk))

        if not segment.is_done:
            raise HttpError(
                f"连接提前结束：分片 {segment.index} 只收到 "
                f"{segment.received}/{segment.size} 字节"
            )

    # ------------------------------------------------------------------
    # 流式下载（服务端不支持 Range / 大小未知）
    # ------------------------------------------------------------------
    async def _run_streaming(self) -> None:
        task = self.task
        if not task.segments:
            from .segment import Segment as _Segment

            task.segments = [_Segment(index=0, start=0, end=0)]
        segment = task.segments[0]
        writer = self.writer
        assert writer is not None

        self._emit("task_started", mode="streaming", connections=1, total_size=task.total_size)
        attempt = 0
        while True:
            self.control.check()
            attempt += 1
            task.attempts += 1
            try:
                if attempt > 1:
                    # 流式不支持续传：每次重试都从头来
                    await writer.truncate_to_pos(0)
                    segment.received = 0
                    task.downloaded_size = 0
                async with self.http.stream(task.url, headers=task.headers) as resp:
                    if resp.status >= 400:
                        raise HttpStatusError(resp.status, task.url, str(resp.reason or ""))
                    async for chunk in self.http.iter_bytes(resp):
                        self.control.check()
                        if self.bandwidth is not None:
                            await self.bandwidth.acquire(len(chunk), task.id)
                        await writer.append(chunk)
                        segment.received += len(chunk)
                        self._on_bytes(len(chunk))
                segment.mark_complete_unknown()
                self._touch()
                return
            except (PauseRequested, CancelRequested, asyncio.CancelledError):
                raise
            except Exception as exc:
                if not is_retryable(exc) or attempt >= self.retry_policy.max_attempts:
                    raise
                delay = self.retry_policy.delay_for(attempt)
                self._emit(
                    "segment_retry",
                    index=0,
                    attempt=attempt,
                    delay=round(delay, 2),
                    error=self.describe_error(exc),
                )
                await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # 收尾：校验 + 原子改名
    # ------------------------------------------------------------------
    async def _finalize(self) -> None:
        task = self.task
        task.transition(TaskStatus.VERIFYING)
        await self._close_writer()

        part = Path(task.part_path)
        if not part.exists():
            raise HttpError("临时文件缺失，无法完成")

        try:
            self._emit("task_verifying", sha256_expected=task.expected_sha256)
            if self.config.verify_sha256 or task.expected_sha256:
                digest = await asyncio.to_thread(sha256_file, part)
                task.sha256 = digest
                if task.expected_sha256:
                    expected = task.expected_sha256.strip().lower()
                    ok = hmac.compare_digest(digest, expected)
                    task.verified = ok
                    if not ok:
                        raise ChecksumMismatch(expected, digest)
                else:
                    task.verified = None

            await asyncio.to_thread(finalize_part, part, task.save_path)
            task.downloaded_size = task.total_size or task.downloaded_size
            task.transition(TaskStatus.COMPLETED)
            self._emit(
                "task_completed",
                save_path=task.save_path,
                size=task.total_size,
                sha256=task.sha256,
                elapsed=round(task.elapsed, 3),
            )
        except ChecksumMismatch:
            raise
        except Exception as exc:
            raise HttpError(f"收尾失败：{exc}") from exc

    # ------------------------------------------------------------------
    # 进度与事件
    # ------------------------------------------------------------------
    def _on_bytes(self, nbytes: int) -> None:
        task = self.task
        self.meter.add(nbytes)
        task.downloaded_size += nbytes
        if self.checkpointer is not None:
            self.checkpointer.touch(task.id)
        now = time.monotonic()
        if now - self._last_emit >= self._emit_interval:
            self._last_emit = now
            speed = self.meter.speed()
            self._emit(
                "task_progress",
                percent=round(task.percent, 2),
                downloaded_size=task.downloaded_size,
                total_size=task.total_size,
                speed=round(speed, 2),
                eta=task.eta(speed),
            )
            if self.on_progress is not None:
                self.on_progress(task, speed)

    def _touch(self) -> None:
        if self.checkpointer is not None:
            self.checkpointer.touch(self.task.id)

    def _emit(self, event_type: str, **data) -> None:
        self.bus.publish(event_type, self.task.id, **data)

    @property
    def speed(self) -> float:
        return self.meter.speed()

    @staticmethod
    def describe_error(exc: BaseException) -> str:
        if isinstance(exc, HttpStatusError):
            return f"HTTP {exc.status} {exc.reason}".strip()
        if isinstance(exc, aiohttp.ClientConnectorError):
            return f"连接失败：{exc}"
        if isinstance(exc, aiohttp.ServerTimeoutError):
            return "服务器响应超时"
        if isinstance(exc, aiohttp.ClientPayloadError):
            return f"传输中断：{exc}"
        if isinstance(exc, asyncio.TimeoutError):
            return "网络超时"
        return f"{type(exc).__name__}: {exc}"
