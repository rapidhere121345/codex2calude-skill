"use strict";

const controls = {
  bridge: document.getElementById("bridge"),
  principal: document.getElementById("principal"),
  period: document.getElementById("period"),
};
const elements = {
  snapshot: document.getElementById("snapshot-status"),
  cards: document.getElementById("summary-cards"),
  policy: document.getElementById("token-policy"),
  health: document.getElementById("health-grid"),
  system: document.getElementById("system-health"),
  recent: document.getElementById("recent-events"),
};

function text(value, unknown = "未知") {
  return value === null || value === undefined || value === "" ? unknown : String(value);
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? new Intl.NumberFormat("zh-CN").format(value)
    : "未知";
}

function percent(value) {
  return typeof value === "number" && Number.isFinite(value)
    ? `${(value * 100).toFixed(1)}%`
    : "未知";
}

function duration(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value < 10000 ? 2 : 0)} s`;
}

function bytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "未知";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function tag(parent, name, className) {
  const element = document.createElement(name);
  if (className) element.className = className;
  parent.appendChild(element);
  return element;
}

function definition(parent, label, value) {
  const item = tag(parent, "div");
  const term = tag(item, "dt");
  term.textContent = label;
  const data = tag(item, "dd");
  data.textContent = value;
}

function metricCard(principal, metrics) {
  const card = tag(elements.cards, "article", "card");
  const title = tag(card, "strong");
  title.textContent = { owner: "Owner", partner: "Partner", total: "总计" }[principal];
  const list = tag(card, "dl");
  definition(list, "调用总数", number(metrics.calls));
  definition(list, "推理调用", number(metrics.inference_calls));
  definition(list, "状态/管理调用", number(metrics.admin_calls));
  definition(list, "成功", number(metrics.successes));
  definition(list, "错误", number(metrics.errors));
  definition(list, "成功率", percent(metrics.success_rate));
  definition(list, "平均耗时", duration(metrics.duration_ms_average));
  definition(list, "推理令牌覆盖", percent(metrics.token_known_coverage));
  definition(list, "输入令牌", number(metrics.tokens.input_tokens.total));
  definition(list, "输出令牌", number(metrics.tokens.output_tokens.total));
  definition(list, "缓存读取令牌", number(metrics.tokens.cache_read_tokens.total));
  definition(list, "缓存写入令牌", number(metrics.tokens.cache_creation_tokens.total));
  definition(list, "上报总令牌", number(metrics.tokens.total_tokens.total));
}

function addStatus(parent, value) {
  const element = tag(parent, "span", `status status-${text(value).toLowerCase()}`);
  element.textContent = text(value);
}

function renderMetrics(payload) {
  clear(elements.cards);
  for (const principal of ["owner", "partner", "total"]) {
    metricCard(principal, payload.metrics[`${payload.filters.bridge}:${principal}`]);
  }
  clear(elements.policy);
  elements.policy.textContent =
    "令牌字段只累计真实 CLI 结果中明确出现的数值；历史未知值保持 NULL，不推断、不补零。";
  const unavailable = Object.entries(payload.database_availability || {}).filter(([, state]) => state !== "ok");
  if (unavailable.length) elements.policy.textContent += ` 数据源不可用，统计不完整：${unavailable.map(([name]) => name).join("、")}。`;
}

function healthCard(name, bridge) {
  const card = tag(elements.health, "article", "health-card");
  const title = tag(card, "h3");
  title.textContent = name;
  const list = tag(card, "dl");
  definition(list, "探针", bridge.probe_ok === true ? "正常" : bridge.probe_ok === false ? "失败" : "未知");
  definition(list, "账号就绪", bridge.account_ready === true ? "是" : bridge.account_ready === false ? "否" : "未知");
  definition(list, "检查时间", text(bridge.checked_at));
  definition(list, "延迟", duration(bridge.latency_ms));
  const account = bridge.account || {};
  definition(list, "登录", text(account.logged_in === true ? "已登录" : account.logged_in === false ? "未登录" : "未知"));
  definition(list, "状态", text(account.status));
  definition(list, "默认模型", text(account.default_model));
  definition(list, "模型选择", text(account.model_selection_ready === true ? "可用" : account.model_selection_ready === false ? "不可用" : "未知"));
  definition(list, "可用性", text(account.availability && account.availability.status));
  const quota = account.quota || {};
  definition(list, "配额可用", text(quota.available === true ? "是" : quota.available === false ? "否" : "未知"));
  definition(list, "配额原因", text(quota.reason));
  definition(list, "配额来源", text(quota.source));
  for (const [key, label] of [["five_hour", "5 小时"], ["seven_day", "7 天"]]) {
    const window = quota[key] || {};
    definition(list, `${label}已用`, typeof window.used_percent === "number" ? `${number(window.used_percent)}%` : "未知");
    definition(list, `${label}剩余`, typeof window.remaining_percent === "number" ? `${number(window.remaining_percent)}%` : "未知");
    definition(list, `${label}重置`, text(window.resets_at));
  }
  definition(list, "配额观测时间", text(quota.observed_at));
  definition(list, "配额观测过期", quota.stale === true ? "是" : quota.stale === false ? "否" : "未知");
  const transport = bridge.transport || {};
  definition(list, "探测方式", text(transport.probe_kind, "未提供"));
  definition(list, "初始化", text(transport.initialize_ok === true ? "成功" : transport.initialize_ok === false ? "失败" : "未知"));
  definition(list, "工具数", number(transport.tools_count));
  definition(list, "SSH Banner", text(transport.ssh_banner_ok === true ? "有效" : transport.ssh_banner_ok === false ? "无效" : "未知"));
}

function renderHealth(payload) {
  const health = payload.health || {};
  elements.snapshot.textContent = `健康快照：${text(health.staleness)}；观测时间：${text(health.observed_at)}`;
  clear(elements.health);
  const bridges = health.bridges || {};
  if (bridges.claude) healthCard("Claude", bridges.claude);
  if (bridges.gemini) healthCard("Gemini", bridges.gemini);
  if (!bridges.claude && !bridges.gemini) {
    const empty = tag(elements.health, "p", "muted");
    empty.textContent = "健康快照缺失或无效；面板不会触发探测。";
  }
  const system = health.system || {};
  clear(elements.system);
  const items = [
    ["内存总量", bytes(system.memory_total_bytes)],
    ["内存可用", bytes(system.memory_available_bytes)],
    ["Swap 已用", bytes(system.swap_used_bytes)],
    ["Claude 进程", number(system.workers && system.workers.claude)],
    ["Gemini 进程", number(system.workers && system.workers.gemini)],
    ["空闲超时", duration(typeof system.idle_timeout_seconds === "number" ? system.idle_timeout_seconds * 1000 : null)],
  ];
  for (const [label, value] of items) {
    const item = tag(elements.system, "div");
    const term = tag(item, "dt");
    term.textContent = label;
    const data = tag(item, "dd");
    data.textContent = value;
  }
}

function recentRow(event) {
  const row = tag(elements.recent, "tr");
  for (const value of [
    event.created_at,
    event.bridge,
    event.principal,
    event.tool,
    event.configured_model,
  ]) {
    tag(row, "td").textContent = text(value);
  }
  tag(row, "td").textContent = event.reported_models
    ? event.reported_models.join(", ")
    : "未上报/未知";
  const status = tag(row, "td");
  addStatus(status, event.status);
  tag(row, "td").textContent = duration(event.duration_ms);
  tag(row, "td").textContent = text(event.error_category);
  tag(row, "td").textContent = text(event.token_usage_source, "未记录");
}

function renderRecent(payload) {
  clear(elements.recent);
  for (const event of payload.recent_events) recentRow(event);
  if (!payload.recent_events.length) {
    const row = tag(elements.recent, "tr");
    const cell = tag(row, "td", "muted");
    cell.colSpan = 10;
    cell.textContent = "所选时间范围没有审计记录。";
  }
}

async function load() {
  const params = new URLSearchParams({
    bridge: controls.bridge.value,
    principal: controls.principal.value,
    period: controls.period.value,
  });
  const response = await fetch(`/api/dashboard?${params}`, {
    method: "GET",
    credentials: "omit",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const payload = await response.json();
  renderMetrics(payload);
  renderHealth(payload);
  renderRecent(payload);
}

async function refresh() {
  try { await load(); }
  catch (error) { elements.snapshot.textContent = `读取失败：${error.message}。请检查 Tailscale 身份及服务状态。`; }
}
document.getElementById("filters").addEventListener("submit", async (event) => {
  event.preventDefault();
  await refresh();
});
await refresh();
