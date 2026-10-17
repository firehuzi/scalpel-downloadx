// 右键菜单 -> 把链接交给本机 DownloadX 引擎
const DEFAULT_BASE = "http://127.0.0.1:8720";

async function settings() {
  const stored = await chrome.storage.local.get({ baseUrl: DEFAULT_BASE, token: "" });
  return { baseUrl: (stored.baseUrl || DEFAULT_BASE).replace(/\/+$/, ""), token: stored.token || "" };
}

async function addTask(url) {
  const { baseUrl, token } = await settings();
  const headers = { "Content-Type": "application/json" };
  if (token) headers["Authorization"] = "Bearer " + token;
  try {
    const res = await fetch(baseUrl + "/api/tasks", {
      method: "POST",
      headers,
      body: JSON.stringify({ url }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      return { ok: false, error: "访问令牌不对：点扩展图标 → 设置里填 downloadx token 的值" };
    }
    if (!res.ok) {
      return { ok: false, error: data.detail || `HTTP ${res.status}` };
    }
    return { ok: true, task: data };
  } catch (err) {
    return { ok: false, error: `连不上 ${baseUrl}（服务没启动？）` };
  }
}

function flashBadge(text, color) {
  chrome.action.setBadgeText({ text });
  chrome.action.setBadgeBackgroundColor({ color });
  setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
}

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: "dx-link",
    title: "用 DownloadX 下载此链接",
    contexts: ["link"],
  });
  chrome.contextMenus.create({
    id: "dx-page",
    title: "用 DownloadX 下载当前页面",
    contexts: ["page"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info) => {
  const url = info.linkUrl || info.pageUrl;
  if (!url || !/^https?:/i.test(url)) {
    flashBadge("✗", "#b62324");
    return;
  }
  const result = await addTask(url);
  flashBadge(result.ok ? "✓" : "✗", result.ok ? "#238636" : "#b62324");
  if (!result.ok) {
    console.warn("[DownloadX]", result.error);
  }
});

// popup 复用同一套逻辑
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message && message.type === "add") {
    addTask(message.url).then(sendResponse);
    return true; // 异步响应
  }
  return false;
});
