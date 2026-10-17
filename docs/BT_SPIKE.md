# P3 spike：libtorrent 能不能用、暂停恢复靠不靠得住

**结论先行：能，但要按"独立进程 + 不承诺字节级续传"来设计。**

实测环境：Windows 11 / Python 3.12.3 / `libtorrent 2.1.1`（cp312 win_amd64 wheel）。
全程离线：脚本自己生成 8 MB 种子（16 KB 分片、512 块），同一进程里跑"做种会话 + 下载会话"，
结果可复现，不依赖公网 swarm。

---

## 1. 安装：site-packages 装不上，`--target` 可以

```text
pip install libtorrent
  → ERROR: Could not install packages due to an OSError:
    [Errno 2] No such file or directory: '...\libtorrent\__init__.cp312-win_amd64.pyd'
    （写二进制模块时被拦，机器上装着腾讯电脑管家）

pip install --no-cache-dir --target <项目目录>/libtorrent libtorrent
  → Successfully installed libtorrent-2.1.1
```

推论：集成时**不要依赖全局 site-packages**，把 wheel 装进应用自己的目录（或做成独立可执行），
既绕开权限/杀软问题，也符合"引擎自带依赖"的定位。

## 2. 暂停 / 恢复：同会话完美，跨会话只恢复"已验证"的块

8 MB 文件、限速 2 MB/s、在 ~50% 处暂停：

| 场景 | 暂停时 | 恢复后起点 | 最终文件 |
| --- | --- | --- | --- |
| **同 session** `pause()` → `resume()` | 261 块 / 4.08 MB | **261 块 / 4.08 MB** | — |
| **新 session** + resume data | 261 块 / 4.08 MB | **129 块 / 2.02 MB** | 校验通过 |
| 新 session + resume data（另一轮） | 281 块 / 4.39 MB | 193 块 / 3.05 MB | 校验通过 |

结论：

- 进程内 `pause()/resume()` 完全无损，可以直接用来实现"暂停"。
- **跨进程恢复只认 hash 已验证的分片**：暂停瞬间还在校验队列里的块会被丢弃重下。
  实测大约一半的已完成块被重下（261 → 129）。
- 这是 BitTorrent 的固有语义（恢复单位是"已验证的 piece"），不是 bug。
  所以 UI 上**不能照抄 HTTP 引擎那种"从断点精确续传"的措辞**。

## 3. 必须 `flush_disk_cache`，否则会产出损坏文件

第一版直接 `handle.save_resume_data()`：

```text
暂停时 done=4.27MB → resume_data 声称已完成 → 新 session 从 3.02MB 起
最终文件校验：**不通过**（SHA-256 与源文件不一致）
```

原因：磁盘缓存里还有没落盘的块，resume data 把它们记成已完成，换 session 后不再重下。

修法（已验证）：

```python
handle.pause()
handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
```

改完之后文件校验通过。**这条是集成时的硬性要求**，不是可选优化。

## 4. 其它 API 事实（踩过的坑）

| 想做的事 | 正确写法 |
| --- | --- |
| 做种 | `atp.flags \|= lt.torrent_flags.seed_mode`（dict 形式不支持 `seed_mode`） |
| 指定 peer | `atp.peers = [("127.0.0.1", port)]` |
| 取 resume 字节 | **`lt.write_resume_data_buf(alert.params)`** —— `alert.params.resume_data` 在这个构建里恒为空 |
| 读 resume | `lt.read_resume_data(buf)`；不要给返回的 params 再赋 `ti`（会把状态冲掉，起点直接归零） |
| 限速 | `handle.set_download_limit(bytes_per_sec)`（会话级 settings 键在这个构建里没生效） |

## 5. 建议的集成形态（若继续）

1. **独立进程**：`downloadx-bt` 子进程持有 libtorrent 会话，与主引擎通过本地 socket/HTTP 通信。
   理由：libtorrent 是 GPL，本项目是 MIT，进程边界最干净；也隔离了 C++ 崩溃。
2. **状态映射**：`queued / downloading / paused / completed / failed` 复用现有状态机；
   进度用 `progress` + `num_pieces/total_pieces`；速度用 `download_rate`。
3. **恢复语义**：写清楚"恢复到最后一次已验证的分片，可能重下少量数据"。
4. **暂停**：`pause()` + `save_resume_data(flush_disk_cache)`，收到 alert 才算暂停完成。
5. **做种**：默认下载完即停（`torrent_flags.upload_mode` 反例），提供开关。
6. **依赖**：wheel 内置到安装包，`pip install --target`；不污染全局环境。

## 6. 成本与风险

| 项 | 评估 |
| --- | --- |
| 代码量 | 子进程 + 协议 + 状态映射 ≈ 600–800 行，外加打包 |
| 依赖 | libtorrent 5 MB wheel（GPL），需随包分发并写清楚许可 |
| 兼容 | Windows 上杀软可能拦 .pyd（本次 site-packages 安装即被拦） |
| 用户体验 | 冷门种子可能长时间 0 速度，需要在 UI 上区分"在连 peer"与"没 peer" |
| 与现有引擎的关系 | 独立的 `mode="torrent"`，复用队列/状态机/事件/API，不复用分片与 Range 逻辑 |

## 7. 一句话建议

技术可行性已验证（能装、能下、能暂停、文件正确）。但它和 HTTP 引擎是**两种不同的东西**：
没有"字节级断点续传"、依赖是 GPL、还要面对 peer 发现与做种策略。
建议按"独立子进程 + 明确的能力边界"立项，而不是塞进现有 worker。
