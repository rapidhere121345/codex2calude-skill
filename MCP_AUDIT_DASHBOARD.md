# MCP 审计与健康面板部署说明

## 安全模型

面板是严格只读的 Python 标准库 HTTP 服务，只提供本地静态资源和 `GET /api/dashboard`、`GET /healthz`。它没有 Shell、代理、任意 URL 抓取、凭据读取、账户变更或刷新探测端点；所有非 GET/HEAD 方法返回 `405`。健康信息只读取 collector 预先写好的 JSON 文件。刷新页面只会重新聚合缓存，不会触发 Claude/Gemini 探测。

生产模式只绑定 `/run/mcp-audit/http.sock`：

- 运行目录必须为 `0700`，socket 必须为 `0600`；
- 每个 UNIX 连接还必须通过 Linux `SO_PEERCRED` 验证对端 UID 为 `0`；
- 不提供生产 TCP 监听。`--listen` 只接受显式传入的 `127.0.0.1` 或 `::1`；
- 设置 `MCP_AUDIT_OWNER_LOGIN` 后，所有 HTML、CSS、JS、API、健康检查和 404 路由都要求 `Tailscale-User-Login` 精确相等；
- `MCP_AUDIT_LOCAL_TEST_NO_AUTH=1` 是唯一显式免认证测试开关，且不能与 owner login 同时设置；
- 请求 Host 必须精确匹配 `MCP_AUDIT_ALLOWED_HOSTS`；默认仅允许本机开发主机；
- CSP 为 `default-src 'none'`，所有脚本和样式来自同一本地 origin，不使用 CDN、analytics 或内联脚本；
- API 不设置 Cookie，因此没有 Cookie 会话；仍显式声明只读边界并拒绝跨站写方法。

这个设计针对的是：Gemini partner 进程可在同一 VPS 上执行 shell。即使该进程伪造 `Tailscale-User-Login`，它也没有 socket DAC 权限；即使获得 socket 访问，非 root UID 仍会被 `SO_PEERCRED` 拒绝。Tailscale Serve 以 root 连接 UNIX socket，并负责剥离外部伪造的同名身份头、注入已认证用户身份。

## 健康快照 JSON Schema

父级健康 collector 每 15 分钟写一次 `/var/lib/mcp-audit/health.json`，文件建议 `root:mcp-audit`、模式 `0640`。页面请求只读缓存，绝不触发新的状态探测。面板只允许 schema 中的安全字段；缺失字段显示“未知”，不会推断。

```json
{
  "observed_at": "RFC3339 timestamp",
  "bridges": {
    "claude": {
      "probe_ok": true,
      "account_ready": true,
      "checked_at": "RFC3339 timestamp",
      "latency_ms": 1234,
      "account": {
        "logged_in": true,
        "status": "ok",
        "default_model": "opus",
        "model_selection_ready": true,
        "cli_version": "safe short label",
        "reasoning_effort": "safe short label",
        "availability": { "status": "eligible", "eligible": true },
        "quota": {
          "available": true,
          "reason": "safe short label",
          "source": "safe short label",
          "observed_at": "RFC3339 timestamp",
          "used_percent": 25,
          "resets_at": "RFC3339 timestamp"
        },
        "models": {
          "opus": {
            "cli_supported": true,
            "selection_ready": true,
            "reasoning_effort": "safe short label"
          }
        }
      },
      "transport": {
        "probe_kind": "local_forced_command",
        "initialize_ok": true,
        "tools_count": 7,
        "server_version": "safe short label",
        "ssh_banner_ok": true
      }
    },
    "gemini": { "...": "same shape as claude" }
  },
  "system": {
    "memory_total_bytes": 1,
    "memory_available_bytes": 1,
    "swap_total_bytes": 1,
    "swap_used_bytes": 1,
    "workers": { "claude": 1, "gemini": 1 },
    "idle_timeout_seconds": 1800
  }
}
```

约定：

- `transport.probe_kind` 必须如实渲染，当前 collector 为 `local_forced_command`；
- `ssh_banner_ok` 只表示 SSH banner 形式有效，不验证公钥认证；
- quota 可以包含 `five_hour`、`seven_day` 两个同类嵌套对象；
- 面板将 20 分钟内视为 fresh，30 分钟内为 warning，更旧为 stale；无有效 `observed_at` 为 unknown。

## 审计数据与 token 覆盖

生产默认读取父级生成的脱敏、被动刷新快照（建议每 60 秒一次）：

- `/var/lib/mcp-audit/claude.sqlite3`
- `/var/lib/mcp-audit/gemini.sqlite3`

连接使用 SQLite `mode=ro` 和 `PRAGMA query_only=ON`。SQL 只访问 `audit_log` 元数据和 token 列；快照不得包含 `conversations` 或 `conversation_history`。面板不读取 prompt、回复、OAuth、私钥、原生 session ID 或 request ID。数据库错误只显示 `unavailable`，不回显 SQL、路径或底层异常。

每个快照只需要一张表：

```sql
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  principal TEXT NOT NULL,
  tool TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL,
  duration_ms INTEGER,
  created_at TEXT NOT NULL,
  error_category TEXT,
  http_status INTEGER,
  exit_code INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  thinking_tokens INTEGER,
  cache_creation_tokens INTEGER,
  cache_read_tokens INTEGER,
  total_tokens INTEGER,
  token_usage_source TEXT,
  reported_models_json TEXT
);
```

所有 token 和 `reported_models_json` 字段均 nullable，历史未知值保持 NULL。字段只在 CLI 成功结果明确给出数值/模型名时写入；token 来源固定为 `claude_cli_result` 或 `gemini_cli_result`。API 的每类 token 都会返回 known calls；没有覆盖时总量为 `null`，不会补零或估算。

用量卡片把 `once/start/continue` 归为推理调用，把 `status/list/export/close` 等归为状态/管理调用；token 覆盖率分母只使用推理调用，避免 15 分钟健康监控稀释或夸大实际用量。最近记录中 `model` 是配置/逻辑模型，`reported_models_json` 是 CLI 明确上报的模型；后者缺失时显示“未上报/未知”，不会把配置值伪装成实际值。

如无法给 `mcp-audit` 只读 DB 权限，优先由父级生成同 schema 的脱敏审计快照；不要放宽 DB 写权限。

## 订阅到期与续费日期

两个账号卡片分别显示订阅日期和剩余天数，7 天内提醒。`expires` 表示已确认的到期日，`renews` 表示下次续费日；过了续费日期只提示核对账单，不推断账号已停用。日期按北京时间的日历天计算，不使用浏览器或 VPS 的默认时区。

当前 CLI 的状态输出未提供可靠的订阅结束日期：Claude `auth status --json` 返回订阅类型；agy `/usage` 返回额度重置时间，`/credits` 返回余额。订阅开通时间、OAuth 登录有效期、额度窗口都不能代替订阅日期。不要据此每月自动推算。

面板只读 `/var/lib/mcp-audit/subscriptions.json`，配置名为 `MCP_AUDIT_SUBSCRIPTIONS_FILE`，也可用 `--subscriptions-file` 指定。该文件应由管理员维护为 `root:mcp-audit 0640`，位于现有的 `root:mcp-audit 0750` 目录内，通过临时文件原子替换。只保存经账单页面确认的日期；修改后刷新页面即可，无需重启服务。下面是**格式示例，不是真实账号日期**：

```json
{
  "claude": {"date": "2026-12-01", "kind": "renews"},
  "gemini": null
}
```

字段来源固定显示为“手动记录”；不存在的文件或 `null` 表示暂未取得，非法日期、其他日期类型和无法读取的文件不产生倒计时。响应只包含经过校验的日期、固定标签和剩余天数，不返回文件路径或任意嵌套内容。这份资料独立于健康快照，即使状态采集暂时失败，也能显示已确认的订阅日期。

日期只能经服务器管理更新；网页保持 owner 专用和只读，不新增账号接口、网络请求、支付权限或写入端点。账单可在 [Claude 官方账单说明](https://support.claude.com/en/articles/8325618)、[Apple 订阅设置](https://support.apple.com/guide/iphone/see-your-purchases-and-subscriptions-iph4e3e7324f/ios) 或 [Google One 会员设置](https://support.google.com/googleone/answer/9003633?hl=en&co=GENIE.Platform%3DDesktop) 对应的订阅平台确认。

## 本地测试

```bash
cd /data/repo/codex2calude-skill
.venv/bin/python -m pytest tests/test_audit_token_recording.py \
  tests/test_mcp_audit_dashboard.py -q

MCP_AUDIT_LOCAL_TEST_NO_AUTH=1 \
MCP_AUDIT_ALLOWED_HOSTS=127.0.0.1:8765 \
MCP_AUDIT_CLAUDE_DB=/tmp/claude.sqlite3 \
MCP_AUDIT_GEMINI_DB=/tmp/gemini.sqlite3 \
MCP_AUDIT_HEALTH_SNAPSHOT=/tmp/health.json \
.venv/bin/python -m mcp_audit_dashboard --listen 127.0.0.1 --port 8765 \
  --local-test-no-auth
```

这个模式只监听 loopback、无 Cookie、只读，并必须显式传入 `--local-test-no-auth`。不要把该环境变量带入生产 unit。

## 生产安装（需父级安全审查后执行）

以下命令仅在父级完成 VPS IP/提供商条款与账户风险审查后执行。

1. 创建专用用户和运行目录：

   ```bash
   sudo useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin mcp-audit
   sudo install -d -o root -g root -m 0755 /opt/mcp-audit-dashboard
   ```

2. 安装本仓库构建产物到 `/opt/mcp-audit-dashboard/current`。至少需要：
   `scripts/run-mcp-audit-dashboard.py`、`src/mcp_audit_dashboard/**`。

3. 生产使用父级生成的脱敏 SQLite 快照：

   ```bash
   /var/lib/mcp-audit/claude.sqlite3  root:mcp-audit 0640
   /var/lib/mcp-audit/gemini.sqlite3  root:mcp-audit 0640
   sudo setfacl -m u:mcp-audit:r /var/lib/mcp-audit/health.json
   ```

   不要授予 `mcp-audit` 原始 bridge home、OAuth、私钥、`conversations` 或 `conversation_history` 访问。审计快照应通过原子替换发布；如必须使用 WAL，也应只授予快照文件及 sidecar 的只读权限。

4. 创建 `/etc/mcp-audit/dashboard.env`（root:root `0600`）：

   ```ini
   MCP_AUDIT_OWNER_LOGIN=<父级注入的精确 owner login>
   MCP_AUDIT_ALLOWED_HOSTS=localhost
   ```

   不要在文件或命令历史中硬编码个人邮箱；由父级配置管理注入。

5. 安装 unit 并启动：

   ```bash
   sudo install -o root -g root -m 0644 \
     deploy/mcp-audit-dashboard.service /etc/systemd/system/mcp-audit-dashboard.service
   sudo systemctl daemon-reload
   sudo systemctl enable --now mcp-audit-dashboard.service
   ```

   实际服务命令：

   ```text
   /usr/bin/python3 /opt/mcp-audit-dashboard/current/scripts/run-mcp-audit-dashboard.py
     --unix-socket /run/mcp-audit/http.sock
     --claude-db /var/lib/mcp-audit/claude.sqlite3
     --gemini-db /var/lib/mcp-audit/gemini.sqlite3
     --health-snapshot /var/lib/mcp-audit/health.json
   ```

6. 配置 Tailscale Serve：

   ```bash
   sudo tailscale serve --bg unix:/run/mcp-audit/http.sock
   sudo tailscale serve status
   ```

   当前 Tailscale Serve 的 Unix target 将后端 Host 改写为 `localhost`，因此白名单配置此精确值。外部入口仍为 Tailnet HTTPS 域名；Unix peer 与 owner 身份校验保持启用。需确认 Tailscale 版本支持 `unix:` target。

7. 强制安全测试：

   ```bash
   sudo runuser -u geminimcp -- curl --max-time 3 \
     --unix-socket /run/mcp-audit/http.sock http://localhost/healthz
   ```

   预期连接失败。再用 root 通过 UNIX socket 且无/错误身份头请求，预期 `401`；带精确 owner login 且 Host 匹配才返回 `200`。通过 Tailscale HTTPS 重复测试 owner 与非 owner。

## 威胁评估与残余风险

- **同机 shell 进程伪造身份头**：由 socket `0600`、目录 `0700` 和 root-only `SO_PEERCRED` 防御。
- **伪造 Host 或 DNS rebinding**：精确 Host allowlist、无 Cookie、无写方法、严格 CSP；UNIX 生产监听也降低浏览器直连风险。
- **XSS/反射注入**：API 不回显查询字符串；客户端只用 `textContent` 和 DOM API，静态资源本地化。
- **数据泄露**：API 不选择 prompt/回复/request ID/native session ID；健康快照在服务端按 allowlist 二次脱敏。
- **凭据访问**：dashboard 用户不读 OAuth 或私钥；systemd 使用 strict protections 且无能力、无网络 address family。
- **SQLite 副作用**：只读 URI、`query_only`、无写路径；错误泛化。
- **账户 suspension/IP 风险**：面板本身不触发 provider 请求或探测；仍需父级审查 VPS IP、服务条款和账户使用形态。这是部署阻断项。
- **Tailscale/TLS 边界**：正确配置 Serve 时它剥离外部身份头并注入认证身份。若 Tailscale daemon 权限、版本或 header 行为不符合前提，必须停止部署。
- **共享数据权限错误**：过宽 ACL 或未来 DB schema 变更可能让只读用户看到更多字段；上线前必须复核实际文件模式，优先改用父级脱敏快照。
- **token 覆盖不完整**：旧历史和未暴露 usage 的 CLI 结果保持 NULL；覆盖率是唯一诚实指标，不能把面板总量当作账户计费总量。

采集器读取原始 WAL 数据库时暂用对应 bridge 账号创建 SQLite 锁文件，防止 root 所有的锁阻碍业务写入。快照 unit 仅为这两个 state 目录及输出目录允许文件系统写入；源连接始终 `mode=ro` / `query_only`。网页 unit 不具备这些写权限。


## 用量与记录分页

- 首页按 Claude、Gemini 分开展示“我 / Partner / 合计”。主要调用数是推理调用，状态/管理请求另列；推理成功率只对已结束的推理请求计算，进行中请求不当作失败。
- `GET /api/dashboard` 的 `bridge`、`principal`、`period` 控制本页范围；`event_status=all|success|failed|running` 和 `event_kind=all|inference|admin` 只筛选审计记录，不改动用量汇总。
- `page` 从 1 起，`page_size` 可选 10、20、50（默认 20）。响应的 `pagination` 含 `page`、`page_size`、`total_rows`、`total_pages`、`has_prev`、`has_next`。空结果为第 1 页 / 0 总页，越界页自动归到末页；非法参数返回 400。
- 历史记录不再仅截取最新 100 条；每页由服务端返回。翻页是对每次读取的最新快照排序，期间新增记录可能使页边界前移，刷新后以新快照为准。
- token 未上报时不补零，界面集中解释数据缺口；技术字段放在可展开的详情里。内存使用率以 `(总量 − 可用) / 总量` 计算，Swap 以 `已用 / 总量` 计算。它们是最近一次监测快照，刷新网页不发起服务商调用。


Gemini 额度来自官方 `agy -p /usage --output-format json` 的 `command.data.groups`，只选择 `Gemini Models` 下的 `5h` / `weekly` 窗口。bridge 将 `remaining_fraction` 转为剩余/已用百分比并规范化 `reset_time`，采集器沿用每 15 分钟读取一次的流程。缺失、重复、越界或非数值的额度不会补零；不会导出原始说明、账号标识或 agy 的其他模型组。此前额度恒为 unavailable 属于适配遗漏，现已修正。


## 界面验收（2026-10-09）

1. PC Chrome，1440px：截图检查后修正过密的嵌套列布局；验证 Claude/Gemini、我/伙伴/合计、记录筛选不改变汇总、翻页、页容量、空结果和快速切换筛选。
2. 移动 Chrome，360/390px：记录切换为卡片，分页按钮并排；无页面横向溢出，暗色模式及 reduced-motion 可用。以独立测试数据验证缺失数据库不显示为 0、账号不可用、过期快照/额度、缺失内存和未启用 Swap 的提示。
3. 生产 HTTPS 真实数据：验证 Gemini 5 小时/每周额度、重置时间、全部历史翻页及 provider/principal/status 筛选；复核 owner 200、无/错误身份 401，以及两个服务账号无法伪造 socket 身份。正式页面使用本地资源，不向供应商发起页面驱动的调用。

所有可见文案统一为 `token`，技术诊断收起在“查看详情”中；浏览器标签图标也由同一身份保护的本地路径提供。为稳定现有测试，CLI 测试替身先消费 stdin，再输出用于验证的结果；tmux 清理测试对异步回收保留 1 秒有界等待，未改动生产 CLI 执行逻辑。

最终验证：`.venv/bin/python -m pytest -q` 为 387 passed；JavaScript 语法及 `git diff --check` 通过。

订阅日期增量验证（2026-10-09）：全套 408 passed；三轮真实 Chrome 检查覆盖 PC、360px 手机暗色和生产 HTTPS。验证了日期类型、7 天边界、北京时间跨日、过期续费、缺失健康数据、无日期与非法日期，页面无横向溢出或控制台错误。生产仍通过精确 owner 身份、root UNIX peer 和两服务账号禁止访问测试。当前两个账号未取得可靠账单日期，因此未向生产填入测试日期。
