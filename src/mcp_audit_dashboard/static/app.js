"use strict";

const controls = {
  bridge: document.getElementById("bridge"),
  principal: document.getElementById("principal"),
  period: document.getElementById("period"),
  eventStatus: document.getElementById("event-status"),
  eventKind: document.getElementById("event-kind"),
  pageSize: document.getElementById("page-size"),
  previous: document.getElementById("prev-page"),
  next: document.getElementById("next-page"),
  refresh: document.getElementById("manual-refresh"),
};

const elements = {
  status: document.getElementById("snapshot-status"),
  quality: document.getElementById("data-quality"),
  providers: document.getElementById("provider-summaries"),
  health: document.getElementById("health-grid"),
  system: document.getElementById("system-health"),
  recent: document.getElementById("recent-events"),
  eventCards: document.getElementById("event-cards"),
  pageIndicator: document.getElementById("page-indicator"),
};

const principalLabels = new Map([
  ["owner", "我"],
  ["partner", "伙伴"],
  ["total", "合计"],
]);

const tokenLabels = new Map([
  ["input_tokens", "输入"],
  ["output_tokens", "输出"],
  ["thinking_tokens", "思考"],
  ["cache_creation_tokens", "缓存写"],
  ["cache_read_tokens", "缓存读"],
  ["total_tokens", "上报合计"],
]);

const countFormatter = new Intl.NumberFormat("zh-CN");
const compactFormatter = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });
const decimalFormatter = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 });
const relativeFormatter = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" });

let currentPage = 1;
let requestSequence = 0;
let activeRequest = null;
let lastPayload = null;

function node(parent, name, className, content) {
  const element = document.createElement(name);
  if (className) element.className = className;
  if (content !== undefined) element.textContent = content;
  parent.appendChild(element);
  return element;
}

function clear(parent) {
  while (parent.firstChild) parent.removeChild(parent.firstChild);
}

function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function count(value) {
  return isNumber(value) ? countFormatter.format(value) : "—";
}

function compactCount(value) {
  return isNumber(value) ? compactFormatter.format(value) : "—";
}

function ratioPercent(value) {
  return isNumber(value) ? `${decimalFormatter.format(value * 100)}%` : "—";
}

function directPercent(value) {
  return isNumber(value) ? `${decimalFormatter.format(value)}%` : "—";
}

function duration(value) {
  if (!isNumber(value)) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  const seconds = value / 1000;
  if (seconds < 60) return `${decimalFormatter.format(seconds)} 秒`;
  if (seconds < 3600) return `${decimalFormatter.format(seconds / 60)} 分钟`;
  return `${decimalFormatter.format(seconds / 3600)} 小时`;
}

function bytes(value) {
  if (!isNumber(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${decimalFormatter.format(size)} ${units[unit]}`;
}

function localDate(value) {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(parsed);
}

function localRelative(value) {
  if (typeof value !== "string" || !value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  const differenceSeconds = Math.round((parsed.getTime() - Date.now()) / 1000);
  if (Math.abs(differenceSeconds) < 60) return "刚刚";
  const thresholds = [
    [60, "minute"],
    [3600, "hour"],
    [86400, "day"],
    [2592000, "month"],
  ];
  for (let index = thresholds.length - 1; index >= 0; index -= 1) {
    const [seconds, unit] = thresholds[index];
    if (Math.abs(differenceSeconds) >= seconds) {
      return relativeFormatter.format(Math.round(differenceSeconds / seconds), unit);
    }
  }
  return "刚刚";
}

function timeLabel(value) {
  const readable = localDate(value);
  return readable === null ? "时间未记录" : readable;
}

function statusClass(value) {
  if (value === true || value === "success" || value === "ok" || value === "fresh") return "status-ok";
  if (value === "warning" || value === "running" || value === "timeout" || value === "cancelled") return "status-warning";
  if (value === false || value === "error" || value === "failed" || value === "stale" || value === "unavailable") return "status-error";
  return "status-neutral";
}

function addStatus(parent, value, readyLabel, unavailableLabel) {
  const element = node(parent, "span", `status ${statusClass(value)}`);
  const stateLabels = new Map([
    ["success", "成功"],
    ["error", "失败"],
    ["failed", "失败"],
    ["running", "进行中"],
    ["timeout", "超时"],
    ["cancelled", "已取消"],
    ["burst_limited", "限流"],
  ]);
  if (value === true) element.textContent = readyLabel;
  else if (value === false) element.textContent = unavailableLabel;
  else if (stateLabels.has(value)) element.textContent = stateLabels.get(value);
  else if (typeof value === "string" && value) element.textContent = value;
  else element.textContent = "未记录";
}

function definition(parent, label, value) {
  if (value === null || value === undefined || value === "") return;
  const item = node(parent, "div");
  node(item, "dt", "", label);
  node(item, "dd", "", String(value));
}

function metricTile(parent, label, value, secondary) {
  const item = node(parent, "div");
  node(item, "span", "metric-label", label);
  node(item, "p", "metric-value", value);
  if (secondary) node(item, "span", "metric-secondary", secondary);
}

function progress(parent, percentValue, caption, secondary) {
  if (!isNumber(percentValue) || percentValue < 0 || percentValue > 100) {
    node(parent, "p", "token-empty", "暂未取得有效的使用比例");
    return;
  }
  const block = node(parent, "div", "quota-item");
  const line = node(block, "div", "quota-line");
  node(line, "span", "", caption);
  node(line, "strong", "", directPercent(percentValue));
  if (secondary) node(block, "span", "metric-secondary", secondary);
  const bar = node(block, "progress");
  bar.setAttribute("aria-label", caption);
  bar.max = 100;
  bar.value = Math.max(0, Math.min(100, percentValue));
  if (percentValue >= 90) bar.className = "level-error";
  else if (percentValue >= 75) bar.className = "level-warning";
}

function renderPrincipalCard(parent, principal, metrics) {
  const card = node(parent, "article", `principal-metrics ${principal === "total" ? "is-total" : ""}`);
  node(card, "h4", "principal-title", principalLabels.get(principal) || principal);
  metricTile(card, "推理调用", count(metrics?.inference_calls));
  const facts = node(card, "div", "principal-facts");
  node(facts, "span", "", `成功 ${count(metrics?.inference_successes)} 次`);
  node(facts, "strong", "", ratioPercent(metrics?.inference_success_rate));
  node(card, "p", "metric-secondary", `平均 ${duration(metrics?.inference_duration_ms_average)}`);
  const tokens = metrics?.tokens || {};
  const known = [...tokenLabels].filter(([field]) => isNumber(tokens[field]?.total));
  if (known.length) {
    const detail = node(card, "details", "token-group");
    node(detail, "summary", "", "已记录 token");
    node(detail, "span", "metric-secondary", `${count(metrics.token_known_calls)} / ${count(metrics.inference_calls)} 次有上报`);
    const chips = node(detail, "ul", "token-chips");
    for (const [field, label] of known) {
      const chip = node(chips, "li", "token-chip");
      node(chip, "span", "", label);
      node(chip, "strong", "", compactCount(tokens[field].total));
    }
  }
}

function renderProvider(provider, payload) {
  const card = node(elements.providers, "article", `provider-card ${provider}`);
  const head = node(card, "div", "provider-head");
  const name = node(head, "div", "provider-name");
  node(name, "span", "provider-glyph", provider === "claude" ? "C" : "G");
  const title = node(name, "h3", "", provider === "claude" ? "Claude" : "Gemini");
  title.id = `${provider}-usage-title`;
  card.setAttribute("aria-labelledby", title.id);
  const selectedBridge = payload.filters && payload.filters.bridge;
  if (selectedBridge !== "total" && selectedBridge !== provider) {
    card.classList.add("is-excluded");
    node(head, "span", "filter-pill", "当前服务筛选未包含");
    node(card, "p", "token-empty", "调整服务筛选后显示这项用量。");
    return;
  }
  if (payload.database_availability?.[provider] !== "ok") {
    node(head, "span", "status status-warning", "暂时不可用");
    node(card, "p", "token-empty", "暂时无法读取此服务的用量，合计可能不完整。");
    return;
  }
  const grid = node(card, "div", `principal-grid ${payload.filters.principal !== "total" ? "two-people" : ""}`);
  for (const principal of ["owner", "partner", "total"]) {
    if (principal !== "total" && payload.filters.principal !== "total" && principal !== payload.filters.principal) continue;
    renderPrincipalCard(grid, principal, payload.metrics[`${provider}:${principal}`]);
  }
  const overall = payload.metrics[`${provider}:total`];
  const footer = node(card, "div", "provider-foot");
  node(footer, "span", "", `状态与管理 ${count(overall?.admin_calls)} 次`);
  node(footer, "span", "", `token 已记录 ${count(overall?.token_known_calls)} / ${count(overall?.inference_calls)} 次`);
}

function renderMetrics(payload) {
  const overall = payload.metrics["total:total"];
  const label = payload.filters.bridge === "total" ? "全部服务" : payload.filters.bridge === "claude" ? "Claude" : "Gemini";
  const partial = Object.values(payload.database_availability || {}).some(value => value !== "ok");
  document.querySelector("#usage-section .section-caption").textContent = `${label}合计 ${count(overall?.inference_calls)} 次推理${partial ? " · 数据不完整" : ""}`;
  clear(elements.providers);
  renderProvider("claude", payload);
  renderProvider("gemini", payload);
}

function quotaWindow(parent, title, windowData) {
  if (!windowData || typeof windowData !== "object") return false;
  const used = isNumber(windowData.used_percent) ? windowData.used_percent : null;
  const remaining = isNumber(windowData.remaining_percent) ? windowData.remaining_percent : null;
  if (used === null && remaining === null) return false;
  const block = node(parent, "div", "quota-block");
  node(block, "h4", "", title);
  const reset = localDate(windowData.resets_at || windowData.reset_at);
  if (used !== null) progress(block, used, "已用", reset ? `重置：${reset}` : null);
  else progress(block, remaining, "剩余", reset ? `重置：${reset}` : null);
  return true;
}

function technicalDetails(parent, entries) {
  const visibleEntries = entries.filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!visibleEntries.length) return null;
  const details = node(parent, "details", "technical");
  node(details, "summary", "", "查看详情");
  const body = node(details, "div", "technical-body");
  const list = node(body, "dl", "technical-list");
  for (const [label, value] of visibleEntries) {
    definition(list, label, value);
  }
  return list;
}

function healthTechnical(parent, bridge, account) {
  const transport = bridge.transport || {};
  technicalDetails(parent, [
    ["配置默认模型", account.default_model],
    ["模型选择", account.model_selection_ready === true ? "可用" : account.model_selection_ready === false ? "不可用" : null],
    ["CLI 状态", account.status],
    ["CLI 版本", account.cli_version || account.claude_version],
    ["探测方式", transport.probe_kind],
    ["初始化", transport.initialize_ok === true ? "成功" : transport.initialize_ok === false ? "失败" : null],
    ["工具数量", isNumber(transport.tools_count) ? count(transport.tools_count) : null],
    ["服务版本", transport.server_version],
    ["SSH Banner", transport.ssh_banner_ok === true ? "有效" : transport.ssh_banner_ok === false ? "无效" : null],
  ]);
}

function healthCard(provider, bridge) {
  const card = node(elements.health, "article", `health-card ${provider}`);
  const name = node(card, "div", "provider-name");
  node(name, "span", "provider-glyph", provider === "claude" ? "C" : "G");
  node(name, "h3", "", provider === "claude" ? "Claude" : "Gemini");

  const readiness = node(card, "div", "readiness-row");
  addStatus(readiness, bridge.account_ready, "账号可用", "账号不可用");
  addStatus(readiness, bridge.probe_ok, "连接正常", "连接失败");
  if (bridge.error_category) node(readiness, "span", "status status-warning", failureReason({error_category: bridge.error_category, status: "error"}));
  if (bridge.checked_at) node(card, "p", "metric-secondary", `检查：${timeLabel(bridge.checked_at)}（${localRelative(bridge.checked_at)}）`);
  if (isNumber(bridge.latency_ms)) node(card, "p", "metric-secondary", `响应：${duration(bridge.latency_ms)}`);

  const account = bridge.account || {};
  const quota = account.quota || {};
  let renderedQuota = false;
  for (const [field, title] of [["five_hour", "5 小时窗口"], ["seven_day", "7 天窗口"]]) {
    renderedQuota = quotaWindow(card, title, quota[field]) || renderedQuota;
  }
  if (!renderedQuota) renderedQuota = quotaWindow(card, "配额", quota);
  if (quota.stale === true) node(card, "p", "notice notice-warning", "额度数据较旧，请以官方账户页面为准。");
  if (provider === "gemini" && !renderedQuota) {
    node(card, "p", "token-empty", "暂未取得 Gemini 额度，请等待下一次监测更新。");
  }

  healthTechnical(card, bridge, account);
}

function renderHealth(payload) {
  clear(elements.health);
  const bridges = payload.health && payload.health.bridges ? payload.health.bridges : {};
  if (bridges.claude) healthCard("claude", bridges.claude);
  if (bridges.gemini) healthCard("gemini", bridges.gemini);
  if (!bridges.claude || !bridges.gemini) {
    node(elements.health, "p", "empty-message", "部分服务尚未取得状态，请稍后查看。");
  }

  clear(elements.system);
  const system = payload.health && payload.health.system ? payload.health.system : {};
  const memoryCard = node(elements.system, "article", "system-card");
  node(memoryCard, "strong", "", "内存");
  if (isNumber(system.memory_total_bytes) && system.memory_total_bytes > 0 && isNumber(system.memory_available_bytes) && system.memory_available_bytes >= 0) {
    const usedPercent = ((system.memory_total_bytes - system.memory_available_bytes) / system.memory_total_bytes) * 100;
    progress(memoryCard, usedPercent, "已用", `容量 ${bytes(system.memory_total_bytes)}`);
  } else {
    node(memoryCard, "p", "token-empty", "内存容量未记录。");
  }

  const swapCard = node(elements.system, "article", "system-card");
  node(swapCard, "strong", "", "缓冲内存 · Swap");
  if (isNumber(system.swap_total_bytes) && system.swap_total_bytes > 0 && isNumber(system.swap_used_bytes) && system.swap_used_bytes >= 0) {
    const usedPercent = (system.swap_used_bytes / system.swap_total_bytes) * 100;
    progress(swapCard, usedPercent, "已用", `容量 ${bytes(system.swap_total_bytes)}`);
  } else if (system.swap_total_bytes === 0) {
    node(swapCard, "p", "token-empty", "未启用 Swap。");
  } else {
    node(swapCard, "p", "token-empty", "Swap 容量未记录。");
  }

  const runtimeCard = node(elements.system, "article", "system-card");
  node(runtimeCard, "strong", "", "运行状态");
  const workers = node(runtimeCard, "div", "worker-row");
  const workerData = system.workers || {};
  node(workers, "span", "worker-chip", `Claude 进程 ${count(workerData.claude)}`);
  node(workers, "span", "worker-chip", `Gemini 进程 ${count(workerData.gemini)}`);
  node(runtimeCard, "p", "metric-secondary", isNumber(system.idle_timeout_seconds) ? `空闲超时 ${duration(system.idle_timeout_seconds * 1000)}` : "空闲超时未记录");
}

function appendTokenDetails(list, event) {
  const tokens = event.tokens && typeof event.tokens === "object" ? event.tokens : null;
  if (!tokens) return;
  for (const [field, label] of tokenLabels) {
    if (isNumber(tokens[field])) definition(list, `${label}token`, compactCount(tokens[field]));
  }
}

function eventDetails(parent, event) {
  const reported = Array.isArray(event.reported_models) ? event.reported_models.filter(Boolean).join("、") : null;
  const list = technicalDetails(parent, [
    ["配置模型", event.configured_model],
    ["CLI 实际上报模型", reported],
    ["工具", event.tool],
    ["原始错误分类", event.error_category],
    ["token来源", event.token_usage_source],
    ["HTTP 状态", isNumber(event.http_status) ? String(event.http_status) : null],
    ["退出码", isNumber(event.exit_code) ? String(event.exit_code) : null],
  ]);
  if (list) appendTokenDetails(list, event);
}

function operation(event) {
  const name = String(event.tool || "").replace(/^(claude|gemini)_/, "");
  return ({once:"单次调用",start:"开启会话",continue:"继续会话",status:"状态检查",list_conversations:"查看会话",export_conversation:"导出会话",close:"结束会话"})[name] || "其他操作";
}

function failureReason(event) {
  if (event.status === "success" || event.status === "running") return "—";
  const code = event.error_category || event.status;
  return ({timeout:"响应超时",TimeoutError:"响应超时",permission:"账号资格或权限需检查",runtime:"服务执行失败",auth:"登录需要检查",authentication:"登录需要检查",unauthorized:"登录需要检查",rate_limit:"调用过于频繁",rate_limited:"调用过于频繁",burst_limited:"调用过于频繁",cancelled:"调用已取消",unknown:"原因未记录",error:"服务执行失败"})[code] || "服务暂不可用";
}

function recentRow(event) {
  const row = node(elements.recent, "tr");
  node(row, "td", "time-cell", timeLabel(event.created_at));
  const providerCell = node(row, "td");
  const provider = node(providerCell, "span", `provider-chip ${event.bridge === "gemini" ? "gemini" : "claude"}`);
  provider.textContent = event.bridge === "gemini" ? "Gemini" : "Claude";
  node(row, "td", "", principalLabels.get(event.principal) || "其他");
  node(row, "td", "operation-cell", operation(event));
  const stateCell = node(row, "td");
  addStatus(stateCell, event.status, "成功", event.status === "error" ? "失败" : "未完成");
  node(row, "td", "", duration(event.duration_ms));
  node(row, "td", "", failureReason(event));
  eventDetails(node(row, "td"), event);
}

function recentCard(event) {
  const card = node(elements.eventCards, "article", "event-card");
  const top = node(card, "div", "event-top");
  node(top, "span", "event-time", timeLabel(event.created_at));
  addStatus(top, event.status, "成功", event.status === "error" ? "失败" : "未完成");
  const meta = node(card, "div", "event-meta");
  const provider = node(meta, "span", `provider-chip ${event.bridge === "gemini" ? "gemini" : "claude"}`);
  provider.textContent = event.bridge === "gemini" ? "Gemini" : "Claude";
  node(meta, "span", "plain-chip", principalLabels.get(event.principal) || "其他");
  node(meta, "span", "plain-chip", duration(event.duration_ms));
  node(card, "p", "event-operation", operation(event));
  if (!["success", "running"].includes(event.status)) node(card, "p", "failure-note", failureReason(event));
  eventDetails(card, event);
}

function fallbackPagination(events) {
  const rows = Array.isArray(events) ? events.length : 0;
  return {
    page: currentPage,
    page_size: Number(controls.pageSize.value),
    total_rows: rows,
    total_pages: rows ? currentPage : 0,
    has_prev: currentPage > 1,
    has_next: false,
  };
}

function renderPagination(payload) {
  const pagination = payload.pagination && typeof payload.pagination === "object" ? payload.pagination : fallbackPagination(payload.recent_events);
  currentPage = Math.max(1, Number(pagination.page) || 1);
  const totalPages = Math.max(0, Number(pagination.total_pages) || 0);
  const totalRows = Math.max(0, Number(pagination.total_rows) || 0);
  elements.pageIndicator.textContent = totalPages ? `第 ${currentPage} / ${totalPages} 页 · ${totalRows} 条` : "第 1 页 · 0 条";
  controls.previous.disabled = !pagination.has_prev;
  controls.next.disabled = !pagination.has_next;
}

function renderRecent(payload) {
  clear(elements.recent);
  clear(elements.eventCards);
  const events = Array.isArray(payload.recent_events) ? payload.recent_events : [];
  for (const event of events) {
    recentRow(event);
    recentCard(event);
  }
  if (!events.length) {
    const row = node(elements.recent, "tr");
    const cell = node(row, "td", "empty-message", "当前筛选没有审计记录。");
    cell.colSpan = 8;
    node(elements.eventCards, "p", "empty-message", "当前筛选没有审计记录。");
  }
  renderPagination(payload);
}

function qualityMessages(payload) {
  const messages = [];
  const unavailable = Object.entries(payload.database_availability || {}).filter(([, state]) => state !== "ok");
  if (unavailable.length) messages.push(`部分数据源不可用（${unavailable.map(([name]) => name).join("、")}），统计可能不完整。`);
  const health = payload.health || {};
  if (health.staleness === "stale") messages.push("健康快照已过期，监控状态可能不代表当前状态。");
  else if (health.staleness === "warning") messages.push("健康快照接近过期，下一次监测后自动恢复。");
  else if (health.staleness === "unknown") messages.push("健康快照时间未知，无法判断新鲜度。");
  if (!messages.length && health.observed_at) messages.push(`健康快照：${timeLabel(health.observed_at)}（${localRelative(health.observed_at)}）。`);
  return messages;
}

function renderQuality(payload) {
  const messages = qualityMessages(payload);
  const warning = Object.values(payload.database_availability || {}).some(state => state !== "ok") || ["stale", "warning", "unknown"].includes(payload.health?.staleness);
  elements.quality.className = warning ? "notice notice-warning" : "notice notice-info";
  elements.quality.textContent = messages.join(" ");
}

function render(payload) {
  renderMetrics(payload);
  renderHealth(payload);
  renderRecent(payload);
  renderQuality(payload);
}

function queryParameters() {
  return new URLSearchParams({
    bridge: controls.bridge.value,
    principal: controls.principal.value,
    period: controls.period.value,
    page: String(currentPage),
    page_size: controls.pageSize.value,
    event_status: controls.eventStatus.value,
    event_kind: controls.eventKind.value,
  }).toString();
}

async function loadDashboard() {
  const sequence = requestSequence + 1;
  requestSequence = sequence;
  if (activeRequest) activeRequest.abort();
  activeRequest = new AbortController();
  controls.refresh.disabled = true;
  controls.previous.disabled = true;
  controls.next.disabled = true;
  elements.status.textContent = "正在更新缓存数据…";
  try {
    const response = await fetch(`/api/dashboard?${queryParameters()}`, {
      method: "GET",
      credentials: "omit",
      cache: "no-store",
      headers: { Accept: "application/json" },
      signal: activeRequest.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (sequence !== requestSequence) return;
    lastPayload = payload;
    render(payload);
    elements.status.textContent = `已更新 · ${timeLabel(new Date().toISOString())}`;
  } catch (error) {
    if (error.name === "AbortError" || sequence !== requestSequence) return;
    elements.status.textContent = lastPayload ? "更新失败，正在显示上次成功数据。" : "数据读取失败，尚无可显示内容。";
    elements.quality.className = "notice notice-error";
    elements.quality.textContent = "读取失败：缓存服务暂不可用。请稍后重试。";
  } finally {
    if (sequence === requestSequence) {
      activeRequest = null;
      controls.refresh.disabled = false;
      renderPagination(lastPayload || { recent_events: [], pagination: fallbackPagination([]) });
    }
  }
}

function resetPageAndLoad() {
  currentPage = 1;
  void loadDashboard();
}

for (const control of [controls.bridge, controls.principal, controls.period, controls.eventStatus, controls.eventKind, controls.pageSize]) {
  control.addEventListener("change", resetPageAndLoad);
}

controls.previous.addEventListener("click", () => {
  if (currentPage > 1) {
    currentPage -= 1;
    void loadDashboard();
  }
});

controls.next.addEventListener("click", () => {
  currentPage += 1;
  void loadDashboard();
});

document.getElementById("dashboard-controls").addEventListener("submit", (event) => {
  event.preventDefault();
  void loadDashboard();
});

controls.refresh.addEventListener("click", () => {
  void loadDashboard();
});

await loadDashboard();
