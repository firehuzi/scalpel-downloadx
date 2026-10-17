# DownloadX 浏览器扩展（Chrome / Edge，MV3）

右键任意链接 → **用 DownloadX 下载此链接**，交给本机的 DownloadX 引擎（多连接 + 断点续传 + SHA-256）。

## 安装（开发者模式，不需要上架商店）

1. 先启动服务：`downloadx serve`
2. Chrome / Edge 打开 `chrome://extensions`（Edge 是 `edge://extensions`）
3. 打开右上角「开发者模式」→「加载已解压的扩展程序」→ 选这个 `extension/` 目录
4. 右键任意链接测试；点工具栏图标可看速度/队列，也能粘贴链接

## 令牌

服务默认不鉴权（只监听回环地址）。如果开了令牌：

```powershell
downloadx token          # 查看
downloadx token --rotate # 生成新的（旧的立即失效，服务需重启）
downloadx token --clear  # 关闭鉴权
```

把输出填进扩展的「设置 → 访问令牌」。

## 限制

- 只监听本机（`127.0.0.1` / `localhost`），扩展不能跨机器用。
- 改过端口时，在扩展设置里改服务地址即可（host 权限已覆盖所有本地端口）。
- 不带图标：Chrome 会显示默认拼图图标；要自定义就放 16/48/128 PNG 并在 manifest 里声明 `icons`。
