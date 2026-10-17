# DownloadX 架构

## 0. 一句话

下载引擎是一个**自洽的异步状态机集群**：任务在状态机里流转，调度器决定谁跑，
执行器把字节搬下来，检查点把进度写进磁盘。API、UI、LLM 都只是它的调用方。

---

## 1. 分层与依赖方向

```
        ┌───────────────────────────────────────────────┐
        │  ui/index.html        cli.py                  │   ← 表现层（可替换）
        └───────────────┬───────────────────────────────┘
                        │ HTTP / WebSocket / 直接调用
        ┌───────────────▼───────────────────────────────┐
        │  api/local_api        api/agent_api           │   ← 接口层（薄）
        └───────────────┬───────────────────────────────┘
                        │ 只是函数调用
        ┌───────────────▼───────────────────────────────┐
        │  core/download_manager   ← 唯一门面            │
        │  core/scheduler  core/download_worker         │   ← 引擎层
        │  core/download_task  core/segment             │
        │  core/state_machine  core/events  core/metrics│
        └───┬───────────────┬───────────────┬───────────┘
            │               │               │
   ┌────────▼──────┐ ┌──────▼───────┐ ┌─────▼─────────┐
   │ queue/        │ │ network/     │ │ storage/      │   ← 能力层
   │ task_queue    │ │ http_client  │ │ file_writer   │
   │ priority_queue│ │ connection_  │ │ metadata_store│
   │ bandwidth_    │ │ pool         │ │ checkpoint    │
   │ controller    │ │ range_request│ │ segment_store │
   └───────────────┘ │ retry        │ │ checksum/lock │
                     │ network_     │ └───────────────┘
                     │ monitor      │
                     └──────────────┘
```

依赖只向下：`ui/cli → api → core → (queue | network | storage)`。
反向依赖一处都没有 —— 把 `api/` 整个删掉，引擎和测试照样跑。

---

## 2. 真实数据回路

这不是单向堆叠：系统里跑着 6 条闭环，任何一条断了功能就不完整。

**① 调度回路（决定"现在跑谁"）**

```
   Scheduler._loop ──▶ TaskQueue.pop ──▶ _spawn ──▶ Worker.run
        ▲                                              │
        └────── wake() ◀── on_finished ◀───────────────┘
```

任务结束 → 回调 → 唤醒主循环 → 再出队。这就是"队列永远在推进"的机制。

**② 分片续传回路（可靠性与速度的核心）**

```
   _segment_loop ──▶ _fetch_segment(Range: 未收区间)
        ▲                    │
        │                失败/断流
        │                    ▼
        └── 退避 sleep ◀── mark_failed ◀── 异常分类（可重试？次数够？）
```

每轮重试都用 `segment.next_offset` 重新拼 Range，所以断在哪就从哪继续，
不需要任何"续传协议"。

**③ 探测回路（元信息必须可信）**

```
   probe ──▶ HEAD ──(405/501 或失败)──▶ GET + Range:0-0 ──▶ 判定 size / Accept-Ranges
        ▲                                                          │
        └──────────── retry_async（503/超时 退避重试）◀─────────────┘
```

**④ 检查点回路（崩溃恢复的前提）**

```
   Worker._on_bytes ──▶ checkpointer.touch(task_id) ──▶ 脏集合
   CheckpointManager._loop ──▶ 每 checkpoint_interval 秒 ──▶ MetadataStore.save_tasks
```

写盘走线程池 + tmp 文件 + fsync + `os.replace`，不阻塞下载协程。

**⑤ 事件回路（UI 实时性）**

```
   Worker/Scheduler ──▶ EventBus.publish ──▶ 每个订阅队列 ──▶ WebSocket ──▶ 浏览器
                            └─▶ recent 环形缓冲（新连上的 UI 能立刻补到最近事件）
```

**⑥ 崩溃恢复回路（跨进程）**

```
   进程启动 ──▶ MetadataStore.load_all_tasks ──▶ 状态是 downloading / verifying ？
        ├─ 是 ──▶ 标记"上次被中断" ──▶ QUEUED ──▶ 重新入队 ──▶ 回到回路 ①
        └─ 否 ──▶ 原样保留（paused / failed / completed 不擅自重启）
```

**⑦ 并发自校准回路（对抗静默限速）**

```
   大文件 + 配置并发 > 1
        │
        ▼
   先用 1 连接跑：预热窗口（≤25% 预算）→ 采样窗口 → 单连接速率 R1
        │
        ▼
   放开到 N 连接跑一个对比窗口 → 聚合速率 R2
        │
        ├─ R2 ≥ 1.15 × R1 ──▶ 保留并发（task_ramp_kept）
        └─ 否则 ────────────▶ 收紧到 1（task_ramp_down），只影响未开始的分片
```

为什么必须在同任务内相邻几秒对比：同一配置的吞吐在一次会话里漂移过 40 倍，
绝对阈值没有意义；"刚刚单连接是多少、现在多连接是多少"才免疫漂移。
预热窗口专门用来排除 DNS/TLS/TCP 慢启动——否则基线会被低估好几倍，
真机上曾把 5.1 MB/s 的单连接量成 31 KB/s。

可选的第八条：`network_monitor`（默认关闭）周期探测网络，离线时暂停调度、
恢复后自动重新入队。

---

## 3. 状态机（唯一的合法流转表）

```
                ┌───────────── 重新下载 ─────────────┐
                ▼                                    │
   [queued] ──▶ [downloading] ──▶ [verifying] ──▶ [completed]
      │  ▲           │  ▲              │
      │  │           ▼  │              ▼
      │  │        [paused]          [failed] ──▶ [queued]（重试）
      │  └────────────┘                 │
      └──────────▶ [canceled] ◀─────────┘
```

| 当前状态 | 允许流转到 |
| --- | --- |
| `queued` | downloading, paused, failed, canceled |
| `downloading` | paused, verifying, failed, canceled, queued |
| `paused` | queued, canceled, failed |
| `verifying` | completed, failed, canceled, queued |
| `completed` | queued（重新下载） |
| `failed` | queued, paused, canceled |
| `canceled` | queued（复活重下） |

要点：

- 流转只能通过 `DownloadTask.transition()` / `StateMachine.to()`，非法流转抛
  `InvalidTransitionError`，不允许"随手赋值状态"。
- `completed / failed / canceled` 是**已结算**状态（`is_terminal()` 为真）：不占资源、
  `wait_for` 立刻返回、写入历史；但它们仍可被显式重新入队。
- `verifying → queued` 专门留给崩溃恢复：校验阶段被中断，重启后可以重跑。

---

## 4. 一个任务的完整生命周期

```
DownloadManager.add(url)
  ├─ 生成 id / 文件名清洗（Windows 非法字符、保留名、超长名截断）
  ├─ 落盘 task.json（状态 queued）
  └─ Scheduler.submit → TaskQueue.push（同优先级 FIFO）

Scheduler._spawn（拿到并发名额时）
  └─ QUEUED → DOWNLOADING
     DownloadWorker.run()
       ├─ _prepare()
       │    ├─ 首次：probe（HEAD → 必要时 Range 探测，带退避重试）
       │    ├─ 恢复：probe 比对 size / ETag / Last-Modified → 变了就废弃旧分片重下
       │    └─ build_plan：segmented（多连接）/ single（不支持 Range）/ streaming（大小未知）
       ├─ _open_writer()：预分配 .dxpart
       ├─ _run_segmented()
       │    └─ N 个分片协程（信号量 = min(任务连接数, 全局并发上限)），各自带重试回路
       │         └─ 每 chunk：控制检查 → 令牌桶 → write_at(绝对偏移) → 进度 → 事件
       └─ _finalize()
            ├─ DOWNLOADING → VERIFYING
            ├─ flush + fsync + 关文件
            ├─ SHA-256（与期望值比对，不一致 → FAILED + checksum_mismatch）
            └─ os.replace(.dxpart → 最终文件) → COMPLETED
```

暂停 / 取消都是**协作式**：设置 `asyncio.Event`，分片协程在 chunk 边界检查后抛
`PauseRequested / CancelRequested`，worker 结算进度、关文件、返回。
已下载的字节全部保留，恢复时从偏移继续。

---

## 5. 并发模型

| 层级 | 限制 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 同时运行的任务数 | `TaskQueue.max_concurrent` | 3 | 调度器只在有空位时出队 |
| 单任务连接数 | `task.connections` | 8 | 分片协程的信号量 |
| 全局连接数 | `ConnectionPool.limit` | 32 | 所有请求都要先拿槽位 |
| 单主机连接数 | `limit_per_host` | 16 | 不把单个站点打满 |
| 全局带宽 | `TokenBucket` | 0（不限） | 初始余量 0，从第一个字节就生效 |
| 单任务带宽 | `TokenBucket` | 0（不限） | 与全局同时生效，取更严格者 |
| 单任务实际并发 | `AdaptiveLimiter` | 上限 = `task.connections` | 被 429/503/504 限流时自动收紧，下限 1 |
| 并发自校准 | 见「⑦ 并发自校准回路」 | 大文件才触发 | 静默限速（不报 429）时的唯一发现手段 |

I/O 全部是 asyncio；磁盘写入放在线程池（`asyncio.to_thread`），同一文件用一把锁
串行化，避免多连接乱序写造成的碎片与竞争。

`AdaptiveLimiter` 的两个细节：

* 名额**按每次尝试申请**，退避等待期间不占用名额，其他分片可以跑满带宽；
* 收紧只影响"还没开始的分片"，已经在传的连接让它自然结束 —— 绝不为了降并发
  去掐断一个已经收了一半的分片（那等于白下载）。

---

## 6. 可靠性机制清单

| 风险 | 应对 |
| --- | --- |
| 断电 / 崩溃 | 元数据原子写（tmp + fsync + `os.replace`）+ 每 N 秒检查点 |
| 半截文件被当成品 | 全程写 `.dxpart`，校验通过才 `os.replace` |
| 服务端忽略 Range | 探测阶段实测识别（校验 `Content-Range`），降级为单连接 |
| 假装支持 Range 却返回 200 | `validate_range_response` 直接报错，绝不写错位置 |
| 把错误页当成文件 | 探测阶段 4xx/5xx 直接抛错（开发中真实踩过的坑） |
| 服务端多给字节 | 按分片剩余长度截断，绝不越界污染相邻分片 |
| 连接提前断开 | 校验分片是否收满，没收满按可重试错误处理，下一轮从偏移继续 |
| 远端文件被替换 | 恢复前比对 size / ETag / Last-Modified，不一致就重下 |
| 校验失败 | 删除 `.dxpart` 并标记 `checksum_mismatch`（坏数据不留盘） |
| 两个进程写同一状态目录 | 心跳式单实例锁，过期可接管 |
| 慢订阅者拖死引擎 | 事件队列满时丢最旧事件，绝不阻塞下载 |
| 域名解析延迟 | 连接池 DNS 缓存 300s |
| 镜像站按并发限流（429/503/504） | 自动收紧该任务并发（下限 1），抛 `task_throttled` 事件 |
| 静默按连接限速（不报错，只是变慢） | 大文件自校准：单连接 vs N 连接的相邻窗口对比，没提升就退回 1 |

---

## 7. 与任务书模块的对应关系

任务书点名的模块全部落地；额外增加的 4 个模块是为了不把职责硬塞进已有文件：

| 任务书模块 | 实现文件 | 备注 |
| --- | --- | --- |
| core/download_manager | `core/download_manager.py` | 对外唯一门面 |
| core/download_task | `core/download_task.py` | 数据模型 |
| core/segment | `core/segment.py` | 分片模型 + 规划 |
| core/scheduler | `core/scheduler.py` | 调度 |
| core/state_machine | `core/state_machine.py` | 状态机 |
| — | `core/download_worker.py` | **新增**：单次下载执行（否则 scheduler 要变成 600 行） |
| — | `core/events.py` | **新增**：进程内事件总线 |
| — | `core/metrics.py` | **新增**：速度计量与格式化 |
| network/http_client | `network/http_client.py` | |
| network/connection_pool | `network/connection_pool.py` | |
| network/range_request | `network/range_request.py` | |
| network/retry | `network/retry.py` | |
| network/network_monitor | `network/network_monitor.py` | 默认关闭 |
| — | `network/hls.py` | **新增**：HLS 主/媒体播放列表解析（P2） |
| — | `network/dash.py` | **新增**：MPD 解析（SegmentTemplate / SegmentList / SegmentBase，P2） |
| storage/file_writer | `storage/file_writer.py` | |
| storage/segment_store | `storage/segment_store.py` | 分片账本 + 进度 |
| storage/metadata_store | `storage/metadata_store.py` | |
| storage/checkpoint | `storage/checkpoint.py` | |
| storage/checksum | `storage/checksum.py` | |
| — | `storage/lock.py` | **新增**：单实例锁 |
| queue/task_queue | `queue/task_queue.py` | |
| queue/priority_queue | `queue/priority_queue.py` | |
| queue/bandwidth_controller | `queue/bandwidth_controller.py` | |
| api/local_api | `api/local_api.py` | REST + WebSocket + 静态 UI |
| api/agent_api | `api/agent_api.py` | 工具清单 + 调用 + 自然语言 |
| ui | `ui/index.html` | 单文件，无构建、无 CDN |
| tests / docs | `tests/` `docs/` | 129 个测试 |

统计口径：60 个 Python 文件、约 12210 行（含测试与脚本），UI 单文件 479 行。

---

## 8. 扩展点（后续协议怎么接）

要加 BT / Magnet 等协议，只需要三件事，**不用动 core 的调度与存储**：

1. 在 `core/segment.py` 的 `build_plan` 里增加一种 `mode`（例如 `torrent`）。
2. 在 `core/download_worker.py` 里加一个 `_run_<mode>()` 分支 —— `_run_segmented` 就是样板：
   拿连接 → 收数据 → 写偏移 → 报进度 → 出错重试。
3. 在 `network/` 下加对应客户端（BT 用 peer 而不是 Range）。

存储、检查点、状态机、事件、API、UI 全部复用。
