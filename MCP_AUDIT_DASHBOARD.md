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

## 审计数据与令牌覆盖

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

所有 token 和 `reported_models_json` 字段均 nullable，历史未知值保持 NULL。字段只在 CLI 成功结果明确给出数值/模型名时写入；token 来源固定为 `claude_cli_result` 或 `gemini_cli_result`。API 的每类令牌都会返回 known calls；没有覆盖时总量为 `null`，不会补零或估算。

用量卡片把 `once/start/continue` 归为推理调用，把 `status/list/export/close` 等归为状态/管理调用；token 覆盖率分母只使用推理调用，避免 15 分钟健康监控稀释或夸大实际用量。最近记录中 `model` 是配置/逻辑模型，`reported_models_json` 是 CLI 明确上报的模型；后者缺失时显示“未上报/未知”，不会把配置值伪装成实际值。

如无法给 `mcp-audit` 只读 DB 权限，优先由父级生成同 schema 的脱敏审计快照；不要放宽 DB 写权限。

## 本地测试

```bash
cd /data/repo/codex2calude-skill
.venv/bin/python -m pytest tests/test_audit_token_recording.py \
  tests/test_mcp_audit_dashboard.py -q

MCP_AUDIT_LOCAL_TEST_NO_AUTH=1 \
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
- **令牌覆盖不完整**：旧历史和未暴露 usage 的 CLI 结果保持 NULL；覆盖率是唯一诚实指标，不能把面板总量当作账户计费总量。

采集器读取原始 WAL 数据库时暂用对应 bridge 账号创建 SQLite 锁文件，防止 root 所有的锁阻碍业务写入。快照 unit 仅为这两个 state 目录及输出目录允许文件系统写入；源连接始终 `mode=ro` / `query_only`。网页 unit 不具备这些写权限。
