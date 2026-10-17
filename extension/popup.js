const DEFAULT_BASE = "http://127.0.0.1:8720";
const $ = (id) => document.getElementById(id);

async function getSettings() {
  const stored = await chrome.storage.local.get({ baseUrl: DEFAULT_BASE, token: "" });
  return { baseUrl: (stored.baseUrl || DEFAULT_BASE).replace(/\/+$/, ""), token: stored.token || "" };
}

function say(text, ok) {
  const el = $("msg");
  el.textContent = text;
  el.className = ok ? "ok" : "err";
}

async function loadStats() {
  const { baseUrl, token } = await getSettings();
  const headers = token ? { Authorization: "Bearer " + token } : {};
  try {
    const res = await fetch(baseUrl + "/api/stats", { headers });
    if (res.status === 401) { $("stats").textContent = "需要访问令牌（见下方设置）"; return; }
    const s = await res.json();
    const mb = (n) => (n / 1048576).toFixed(2);
    $("stats").textContent =
      `速度 ${mb(s.download_speed)} MB/s · 下载中 ${s.active_tasks} · 排队 ${s.queued_tasks} · 完成 ${s.completed_tasks}`;
  } catch {
    $("stats").textContent = `连不上 ${baseUrl}（服务没启动？）`;
  }
}

async function add() {
  const url = $("url").value.trim();
  if (!url) { say("先填一个链接", false); return; }
  const result = await chrome.runtime.sendMessage({ type: "add", url });
  if (result && result.ok) {
    const t = result.task;
    say(`已加入：${t.filename || t.id}`, true);
    $("url").value = "";
    loadStats();
  } else {
    say((result && result.error) || "添加失败", false);
  }
}

async function init() {
  const { baseUrl, token } = await getSettings();
  $("base").value = baseUrl;
  $("token").value = token;
  loadStats();
}

$("add").addEventListener("click", add);
$("url").addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
$("save").addEventListener("click", async () => {
  const baseUrl = ($("base").value.trim() || DEFAULT_BASE).replace(/\/+$/, "");
  const token = $("token").value.trim();
  await chrome.storage.local.set({ baseUrl, token });
  say("已保存", true);
  loadStats();
});
init();
