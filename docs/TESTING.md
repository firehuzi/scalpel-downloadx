# 测试说明

```powershell
cd outputs\DownloadX
python -m pytest              # 全量：100 个测试，约 80 秒
python -m pytest tests/test_units.py          # 纯单元，1 秒
python -m pytest tests/test_e2e_download.py   # 下载行为端到端
python -m pytest tests/test_e2e_control.py    # 暂停 / 续传 / 崩溃恢复
python -m pytest tests/test_api.py            # REST + Agent API
python -m pytest tests/test_cli.py            # 命令行
python scripts/smoke_live.py                  # 拉起真实服务进程联调
```

测试不依赖外网：所有下载都指向本机 `tests/support/server.py` 起的可控 HTTP 服务。

---

## 1. 可控 HTTP 服务覆盖的恶劣情况

| 端点 | 模拟的真实情况 | 验证点 |
| --- | --- | --- |
| `/file/<name>` | 标准服务器：HEAD + Range + 206 + ETag | 多连接分片、断点续传 |
| `/norange/<name>` | 忽略 Range，永远 200 | 自动降级为单连接，结果仍正确 |
| `/unknown/<name>` | 不给 `Content-Length` | 流式下载，能正确收尾 |
| `/flaky/<name>?fails=N` | 前 N 次返回 503 | 重试（含探测阶段重试）后成功 |
| `/truncate/<name>` | 报全长却只发一半就断流 | 判定中断 → 从偏移续传 → 内容正确 |
| `/slow/<name>` | 分块限速输出 | 留出时间窗口做暂停 / 取消 |
| `/disposition/<name>` | `Content-Disposition: filename*=UTF-8''…` | 中文文件名正确落盘 |
| `/missing` | 404 | 任务失败、错误信息可读、不重试 404 |

服务还会记录每次请求的 `Range` 头，测试据此断言**续传是从偏移继续而不是重下**
（`tests/support/server.py: range_starts()`）。

---

## 2. 各测试文件覆盖什么

**`tests/test_units.py`（47 个）** —— 不需要网络的纯逻辑

- 状态机：合法流转、5 组非法流转、监听器、已结算状态集合
- 分片规划：区间连续不重叠、总长守恒、连接数钳制、三种模式判定
- Range 解析：`Content-Range` 解析、大小写不敏感的头、Windows 文件名清洗
  （非法字符 / 保留名 `CON` / 超长名截断）
- 重试：退避序列、可重试判定（503 可重试，404/416 不可）、耗尽与永久错误
- 存储：元数据往返、索引丢失后自愈、v0 → v1 迁移、损坏文件跳过、历史倒序
- 文件写入：随机偏移写、追加写、模式守卫、`.dxpart` 原子改名
- 校验：`sha256_file` / `verify_file` / 增量哈希
- 队列：优先级顺序、同优先级 FIFO、运行时改优先级、移除、去重
- 限速：不限速零开销、限速真的等、单任务与全局叠加
- 检查点：只刷脏任务、后台周期刷盘
- 单实例锁：互斥、崩溃残留接管
- 任务模型：序列化往返、显式文件名不被服务端覆盖、时间戳与流转校验
- 配置：文件往返、环境变量覆盖、配置文件损坏时回退默认

**`tests/test_e2e_download.py`（9 个）** —— 真下载

分片下载 + SHA-256 校验 + 最终文件就位、小文件单连接、`norange` 降级、
未知大小流式、中文文件名、503 重试、校验不符失败、404 失败、断流续传。

**`tests/test_e2e_control.py`（12 个）** —— 控制、恢复与并发自适应

暂停后从中断偏移继续（断言恢复后的 Range 起始偏移全部 > 0）、取消后重试续传、
`--reset` 从零重下、**崩溃恢复**（手工构造 `downloading` + 半截分片的磁盘现场，
断言恢复后的请求起始偏移 ≥ 已完成字节）、优先级出队顺序、全局限速生效、
**服务端限流自适应**（服务端并发上限 2 时引擎必须自降并发并最终完成且内容正确）、
**无误报**（服务端能扛 8 并发时不允许降连接数）、
**静默限速自校准**（聚合限速端点：加连接没有吞吐提升时必须退回单连接，且配置值不被改写）、
**并行有收益时保留并发**（每连接独立限速时必须判定为有收益）、
**小文件跳过校准**（不为了测量浪费时间）、
**校准窗口里暂停**（不留下任何存活的 `dx-seg-*` 分片任务，也不再有新请求）。

其中聚合限速与每连接限速两个端点，是测试服务器对"静默按连接限速"与"并行真有用"
两种真实服务端行为的确定性建模。

**`tests/test_api.py`（19 个）** —— REST 与 Agent

健康检查 / 配置、创建下载并完成、暂停恢复、改优先级与删除、错误码映射
（404 / 409 / 400）、探测接口、统计与历史、WebSocket 快照、manifest 结构、
工具调用、工具错误、自然语言（下载 / 列表 / 统计 / 暂停 / 只出计划 / 听不懂）、
UI 首页可访问。

**`tests/test_cli.py`（12 个）** —— 命令行

前台下载、JSON 输出、列表 / 详情 / 统计、暂停 → 重试、取消 → 移除（含删文件）、
探测、工具清单、自然语言（进程内会自动等到下完）、配置读写、坏 URL 退出码、
未知任务退出码、`--version`。

---

## 3. 联调脚本

```powershell
python scripts/smoke_live.py
```

它会：

1. 用 `subprocess` 真的拉起 `python -m downloadx serve`（独立进程，无窗口）；
2. 等健康检查通过；
3. 在 4 MiB 文件上跑完整流程：探测 → 创建 → 进度采样 → 完成 → 校验内容 → 统计 → 历史；
4. 打印 Agent 工具数量与自然语言调用结果；
5. 收尾时终止服务进程并回显服务端日志尾部。

最近一次运行结果（本机）：

```
[1] 服务进程 pid=25784  健康检查: DownloadX 0.1.0
[2] Web UI: HTTP 200, 19935 字节, 标题包含 DownloadX: True
[3] 探测: demo-package.bin  4194304 字节  可续传=True
[4] 创建任务 c3b8ff41  mode=segmented
[5] 峰值速度 6.99 MB/s, 最终 completed 100.0%
[6] 落盘文件 4194304 字节, 内容校验=通过
[7] 自然语言指令 -> 规划器=rules  工具=list_downloads  回复: 共有 1 个任务
[8] Agent 工具清单: 15 个
[9] 统计: 完成=1 连接池=0/32 峰值连接=1
[10] 历史记录 1 条, 首条=demo-package.bin completed
[11] 建任务->暂停(paused)->移除(True)
```

---

## 4. 测试环境

- Windows 11 + Python 3.12.3
- 不依赖外网；所有请求都指向 `127.0.0.1`
- 测试用的状态目录与下载目录都在 `tmp_path` 下，互不干扰
- 不使用 `pytest-asyncio`：用 `tests/conftest.py` 的 `run()` 包装 `asyncio.run`，
  少一个依赖，也避免事件循环跨用例污染
