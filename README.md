# claude-research-mcp

Private, restricted stdio MCP bridge for running Claude Code research jobs on a
remote server. Codex starts it through SSH; there is no HTTP listener and no
Tailscale dependency.

The bridge exposes exactly seven tools:

- `claude_status()` — sanitized CLI, login, subscription, model-policy, and
  best-effort Opus quota status.
- `claude_once(...)` — one independent query.
- `claude_start(...)` — start a logical task with an optional human title and
  return an opaque `conversation_id`.
- `claude_list_conversations()` — list the current principal's conversations.
- `claude_export_conversation(conversation_id)` — return readable user and
  assistant content for identifying or reviewing one of those conversations.
- `claude_continue(conversation_id, ...)` — resume that task with its original
  model and web policy.
- `claude_close(conversation_id)` — close the bridge mapping. It does not erase
  the Claude-side transcript, but further MCP continuation is rejected.

## Gemini Research companion

This repository also contains the `gemini-research` Skill and `gemini_research`
MCP server. It keeps the Claude bridge's seven-tool interface, with a separate
VPS service account, state directory, SSH principal, and owner/partner keys. The
backend is the official **Antigravity CLI (`agy`) 1.2.14 or newer**. It uses the
owner's Google OAuth subscription on the VPS; clients never receive Google
credentials. The deployment does not select API-key or Vertex billing.

- `gemini_status()` — CLI version, authentication availability, and model policy;
  it uses the CLI's read-only `/usage` command without a model turn. Quota remains
  unavailable when the bridge cannot safely normalize the CLI's quota schema.
- `gemini_once(...)` — one independent query.
- `gemini_start(...)` — start a titled task and return an opaque bridge UUID.
- `gemini_list_conversations()` — list the current principal's tasks.
- `gemini_export_conversation(conversation_id)` — export the bridge's stored
  user/assistant turns for an owned task.
- `gemini_continue(conversation_id, ...)` — resume the owned native conversation.
- `gemini_close(conversation_id)` — reject further continuation while retaining
  readable history.

Install `skills/gemini-research` from the repository's `main` branch:
[GitHub](https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/gemini-research).
The canonical partner flow fetches the Skill there and does not require a Skill
ZIP; the owner sends only the dedicated private partner-access ZIP separately
through a controlled channel. The handoff is in
[`examples/PARTNER_SETUP_GEMINI.zh-CN.md`](examples/PARTNER_SETUP_GEMINI.zh-CN.md).
The installed Skill includes `scripts/call_tool.py`, a restricted client for
this same MCP endpoint. It lets Codex verify and use the bridge in the
installation conversation before its native tool catalog is reloaded.
The production paths are:

```text
/opt/gemini-research-mcp/releases/<release>
/opt/gemini-research-mcp/current -> releases/<release>
/opt/antigravity/current/bin/agy
/var/lib/gemini-mcp/research
/var/lib/gemini-mcp/.gemini/config/agents/
/var/lib/gemini-mcp/.gemini/antigravity-cli/settings.json
/etc/ssh/gemini-mcp-authorized_keys
/etc/ssh/sshd_config.claude-mcp  # shared TCP 22022 listener
```

`deploy/gemini-agents/` contains the two task-mode definitions and independent
VPS CLI permission settings. The bridge validates CLI init format and required
web capabilities (`search_web` and `read_url_content` for `use_web=true`), not
a tool-catalogue subset. CLI tool execution uses VPS `always-proceed` settings
without an ordinary-tool allowlist. Trusted owner and partner may request
remote files, commands, and other ordinary tools for explicit tasks.
`use_web=false` requests no web research; it is not technical isolation from
web, files, or shell. The initial catalogue proves neither effective tool
permissions nor tool-context pruning. Do not let `agy` spawn or delegate other
AI agents or tasks, or use commands to switch AI CLIs/models to bypass fixed
Pro; collaboration remains with the outer Codex native subagents. Ordinary
prompts use official NDJSON stdin with slash expansion disabled. Timeout and
output bounds are enforced by the bridge; `max_turns` is retained for interface
compatibility and is not a native `agy` limit. Native conversation IDs stay on
the VPS. Successful task turns are stored privately for export, separate from
the text-free audit records.

Authenticate only on the VPS by running `agy` as `geminimcp`, selecting Google
OAuth, and completing the [official remote SSH authorization
flow](https://antigravity.google/docs/cli/install/#remote-ssh-oauth-flow).
A personal login to the retired `gemini` CLI cannot serve this deployment.
Owner and partner share the account's plan quota; their SSH keys are separate,
and bridge session mappings and queries are principal-filtered. With file/shell tools,
the shared VPS service account does not provide filesystem isolation between
them. Google credentials, tokens, and keys remain on the VPS and are not
delivered to partners; no local provider login is required. Public clients
connect on the existing TCP 22022 SSH listener; a user-specific
forced command routes the `geminimcp` account to the separate Gemini deployment.
The partner key grants no direct SSH shell or arbitrary SSH command execution;
Gemini's authorized CLI tools can still execute remote commands.
The retired 22023 listener is disabled and its VPS firewall rules removed.

Production exposes only logical model `pro`, bound to the authenticated
account's confirmed highest Pro entry `gemini-3.1-pro-high` with `--effort high`.
The operator verifies these with `agy models` on the VPS; do not infer a CLI
identifier from an API name. A root-owned `/etc/gemini-mcp-model-policy.conf`
sets `GEMINI_PRO_CLI_VALUE` and `GEMINI_PRO_EFFORT`. Until bound, its `auto`
placeholder is blocked by `GEMINI_MCP_REQUIRE_EXPLICIT_MODEL=1`, and
`gemini_status.model_selection_ready` is false. Owner and partner use the same
policy; started tasks retain their concrete model and effort for continuation.
The bridge never automatically falls back to Flash, another provider, API-key,
or Vertex billing.

`gemini_status.availability` distinguishes an eligible account from an
official region restriction, missing authentication, or an unknown diagnostic.
Model binding and a successful OAuth login are separate from service eligibility
and a successful inference test. The installer refuses to declare verification
complete while availability is blocked or unknown.

Acceptance completed on 2026-10-02 with official `agy` 1.2.14 personal OAuth.
Owner and a freshly installed partner-principal client both passed real MCP
queries, native continuation, list/export/close, and cross-principal rejection.
Remote command execution and file writing were verified on the VPS; web mode
read an official source successfully. The production policy binds `pro` to
`gemini-3.1-pro-high` with `high` effort; do not infer a currently billed model
when usage metadata is empty. The shared TCP 22022 listener was unchanged. CLI
init lists a global catalogue, including names unavailable to the execution
registry; task definitions include the supported ordinary tools. The partner
fetches the Skill from GitHub `main`; the owner sends the dedicated access-key
ZIP separately.

The deployment sets `useG1Credits=false` (also the default). Account AI Credit
Overages `Never` has not yet been confirmed, so baseline-plan-only usage is not
established: personal OAuth does not by itself disable overage credits.
Quota exhaustion stops the task.
See the [official quota and overage policy](https://antigravity.google/docs/plans/).

## Hand this repository to another Codex

Before sending the repository URL, the owner must grant the partner Read access
to the private repository and pre-authorize a dedicated partner key. Deliver
the passphrase-less private key only through a controlled one-to-one channel;
it is a bearer credential for this MCP endpoint. Then send this prompt together
with the key file:

```text
请使用 $skill-installer 从
https://github.com/rapidhere121345/codex2calude-skill/tree/main/skills/claude-research
安装 claude-research Skill。不要读取、展示、上传或转发我附带的私钥内容。
安装后在 Skill 目录运行 python3 scripts/setup_client.py --principal partner
--import-key <我附带的私钥绝对路径> --verify。脚本必须显示 pre-authorized
import 且验证恰好七个 claude 工具；成功后通过系统安全删除机制删除交付私钥
和压缩包，并提醒我重启 Codex。遇到已有密钥、host pin 或 MCP 配置冲突时停止，
不要覆盖或绕过校验。
```

The setup is otherwise local and does not require Tailscale. `--import-key`
copies the key to the dedicated destination with mode 0600, rejects symlinks
and overwrites, derives and validates its public key, and never prints the
private key. The delivery copy is intentionally retained until verification
succeeds, after which it should be securely deleted. There is no public
self-enrollment endpoint.

Prefer the `$skill-installer` path shown above. If it is unavailable, a partner
with Git repository access may clone this repository and copy only
`skills/claude-research` into `$CODEX_HOME/skills/claude-research`. If the
partner has neither GitHub CLI nor `$skill-installer`, the owner may send that
skill directory alone through a private channel. Do not include credentials or
unrelated repository files. Without a pre-authorized delivered key, the
generated-key fallback prints only a `.pub` key and deliberately stops for
administrator authorization.

## Conversation and model policy

Use one Claude session per logical task. `claude_start` stores the native Claude
session ID only on the server and returns a separate UUID. In a later Codex
session, list the current principal's conversations first and select the MCP
`conversation_id`; never ask for or expose the native Claude session ID. List
and export are principal-isolated, and export can be used to identify or review
a conversation from its readable user/assistant content. A continuation must
come from the same SSH principal (`owner` or `partner`). Close only a genuinely
finished conversation: closing retains the transcript but rejects further MCP
continuation.

The production wrapper currently enables only logical model `opus`, mapped to
Claude Code's `opus` alias. Every successful answer must report an actual Opus
model in Claude's usage metadata; otherwise the call fails instead of silently
accepting a fallback. The model is fixed when a conversation starts.

Fable 5.1 can be enabled later, after both the account and Claude Code support
it, by adding this entry to `CLAUDE_MCP_MODELS`:

```json
{"opus":{"cli_value":"opus","family":"opus","min_cli_version":"2.1.236"},"fable":{"cli_value":"fable","family":"fable","min_cli_version":"2.1.257"}}
```

Do not add Fable to the allowlist merely to probe entitlement: non-interactive
Claude Code calls can consume usage before reporting an account-level problem.

`claude_status.opus_quota` reports only an observation emitted through Claude
Code's documented [statusline `rate_limits`](https://code.claude.com/docs/en/statusline)
field. The capture helper discards
the rest of the statusline payload and stores only `five_hour`, `seven_day`, and
an observation time. The bridge never reads `~/.claude/.credentials.json`,
never calls an undocumented usage endpoint, and never sends a model request to
obtain quota data. Each official window is independently optional. A valid
window contains used/remaining percentages and its reset time; `source`,
`observed_at`, `stale`, and `age_seconds` make provenance explicit.

Claude Code may not invoke a configured statusline during non-interactive `-p`
runs. Therefore `available: false` is an expected safe result until a recent
interactive statusline observation exists. It means unknown, not zero usage or
unlimited capacity; use the returned `https://claude.ai/usage` URL for the
manual check.

## Security boundary

- SSH on a dedicated public port is transport only. Each public key is forced
  into this MCP process and cannot obtain a shell, PTY, forwarding, SFTP, or an
  arbitrary command.
- The existing administrative SSH port can remain restricted to Tailscale.
- The MCP client cannot choose an executable, cwd, shell command, file path, or
  Claude tool allowlist.
- Prompts are sent on stdin and never placed in process argv or audit records.
- Claude runs with `--permission-mode dontAsk`, an empty MCP config, and no
  browser integration. `use_web=true` enables exactly `WebSearch,WebFetch`;
  `false` enables no Claude tools.
- Calls run in new process groups. Timeouts and cancellation stop Claude and
  its descendants.
- SQLite enforces principal ownership. A per-principal, per-conversation
  rolling burst guard protects rapid `claude_continue` calls; the guard check
  and execution reservation happen in one SQLite write transaction. A
  process-wide flock limits shared-account concurrency.
- Responses and diagnostics are bounded. Raw Claude stderr is not returned.
- CLI failures are classified from bounded error envelopes on stdout and from
  stderr into fixed, sanitized categories. Only allowlisted error categories
  and explicitly structured HTTP status codes may cross the bridge; raw error
  messages, prompts, account identifiers, paths, and credentials do not cross
  the bridge or enter the structured audit table. Failed CLI output can be
  retained separately in private, bounded server-side diagnostic files; see
  the operator-only diagnostics section below.
  A successful `claude_status` checks CLI version and reported login state, not
  inference availability or model entitlement. Use one minimal `claude_once`
  request to verify the inference path; do not repeatedly retry an unchanged
  failure or interpret missing quota observations as exhausted quota.
- Quota status never reads OAuth credentials and never calls an undocumented
  usage endpoint. It only reads the bounded, owner-controlled sanitized
  statusline observation, and reports unavailable rather than guessing.
- The Claude login and state live under the dedicated, password-locked
  `claudemcp` service account in `/var/lib/claude-mcp`; neither SSH principal
  receives its OAuth material or a server shell.
- The root-owned runtime is pinned, and Claude children receive an exact
  environment allowlist with SSH, proxy, API-key, desktop-session, and client
  variables removed. Claude Code's documented
  [privacy environment controls](https://code.claude.com/docs/en/env-vars)
  disable nonessential telemetry, feedback, crash reporting, marketplace
  auto-install, all CLI updates, and Claude.ai MCP connector loading.
- Only MCP/Claude traffic originates from the VPS. A partner's source address
  is visible to the VPS SSH daemon and its logs, but is not forwarded in the
  Claude child environment. Prompts, pasted identifiers, and URLs can still
  disclose information by content, so callers must keep them scoped.

Web content is still untrusted. Prompt injection can influence research output,
so important findings should be verified.

## Operator-only error diagnostics

Tool invocations receive a server-generated, opaque `request_id`. The same
identifier is returned to the caller and stored in `audit_log`, together with
allowlisted `error_category`, `http_status`, and `exit_code` when available.
The identifier is not a Claude-native session ID. Older audit rows retain NULL
diagnostic fields: the upgrade cannot reconstruct historical error details.
Requests rejected by the MCP protocol or argument schema before entering a tool
handler may not have an application audit record.

The structured audit table never stores prompts, answers, raw exception text,
stdout, stderr, account identifiers, or client IP addresses. `duration_ms`
remains total tool time, including queueing, not pure model latency. Unknown
errors remain `unknown`; a successful status check does not establish that a
model call or a particular resumed conversation will succeed.

At the owner's explicit request, captured **failed** CLI stdout/stderr is also
retained under:

```text
/var/lib/claude-mcp/research/state/error-diagnostics/<principal>/<request_id>.json
```

These files are sensitive: unredacted error output can contain private content
or credentials echoed by an upstream component. Directories are mode `0700`,
files are mode `0600`, and they are not exposed through MCP, Git, or the partner
SSH key. No successful response, explicit prompt, environment dump, or OAuth
credential file is deliberately copied into these diagnostics. Each output
stream is bounded to 64 KiB and records whether truncation occurred. Each
principal's store is limited to 100 files and 20 MiB; older files are removed
when these limits are reached. Retention cleanup removes files older than seven
days on writes and through the deployed daily maintenance timer (so an idle
store can retain a file until the next daily sweep). Only regular
UUID-named diagnostic files inside the private store are eligible for cleanup.

On a failure, the partner should send the **request ID and sanitized error** to
the operator, not a key or a transcript. The operator first correlates the audit
record, then inspects the matching private file only if necessary. Do not paste
raw diagnostic files into chat or research reports. A failure before CLI output
exists, a storage failure, or truncation can leave incomplete raw evidence.
The bridge still exposes exactly seven tools and grants no new partner access.

The retention units are `claude-mcp-error-retention.service` and
`claude-mcp-error-retention.timer`; the one-shot service has no network access
and can write only the private diagnostic store. A rollback to a release that
predates this helper must also stop/disable this timer. Retained evidence must
not be deleted merely to roll back executable code. Existing MCP connections
can keep an already-loaded older server after an atomic release switch; reconnect
the MCP client to receive the new diagnostic fields. Do not interrupt an active
research task just to refresh an idle client's tool connection.

## Account and policy boundary

Technical isolation does not grant permission to share a consumer Claude
account. Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms)
prohibit sharing login details or making an account available to another
person, and use must come from a
[supported location](https://www.anthropic.com/supported-countries). Keep the
partner key disabled unless the account arrangement and every user's location
are covered by an Anthropic-supported organization, team, API, or written
authorization. This repository cannot make consumer-account sharing
policy-compliant or ban-safe.

## Install on the VPS

The expected production paths are:

```text
/opt/claude-research-mcp/releases/<release>
/opt/claude-research-mcp/current -> releases/<release>
/opt/claude-code/2.1.236/claude
/var/lib/claude-mcp/.claude
/var/lib/claude-mcp/research
/etc/ssh/authorized_keys/claude-mcp
/etc/ssh/sshd_config.claude-mcp
```

Create a password-locked service account with no sudo membership. Its shell is
needed only because OpenSSH executes the key's forced command through it:

```sh
sudo useradd --system --create-home --home-dir /var/lib/claude-mcp --shell /bin/sh claudemcp
sudo passwd --lock claudemcp
sudo install -d -m 0700 -o claudemcp -g claudemcp /var/lib/claude-mcp/research
```

Copy each release into a new root-owned directory, build its `.venv`, and move
the `current` symlink only after tests pass. Install Claude Code from its
official distribution at the pinned root-owned path shown above; do not let the
service account replace that binary. Authenticate Claude interactively as
`claudemcp` from a trusted administrative console, never by distributing its
OAuth files.

Production retains Claude transcripts for 365 days. Configure the sanitized
statusline capture at the same time; preserve every unrelated settings key:

```sh
sudo -u claudemcp -H python3 - <<'PY'
import json
import os
import tempfile
from pathlib import Path

path = Path.home() / ".claude" / "settings.json"
path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
if path.is_symlink():
    raise SystemExit(f"refusing symlink: {path}")
data = {}
if path.exists():
    if not path.is_file():
        raise SystemExit(f"not a regular file: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("settings.json must contain a JSON object")
data["cleanupPeriodDays"] = 365
data["statusLine"] = {
    "type": "command",
    "command": "/opt/claude-research-mcp/current/scripts/capture-statusline-rate-limits.py",
}
fd, temporary_name = tempfile.mkstemp(prefix=".settings.json.", dir=path.parent)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary_name, 0o600)
    os.replace(temporary_name, path)
finally:
    try:
        os.unlink(temporary_name)
    except FileNotFoundError:
        pass
print("configured retention and sanitized statusline capture")
PY
```

The statusline helper needs `CLAUDE_MCP_STATUSLINE_OBSERVATION` in the
interactive Claude environment to write a sample. The forced MCP wrapper sets
it automatically, but non-interactive Claude may not render a statusline.

Install the dedicated SSH daemon configuration only after inserting real public
keys into a private copy of `examples/authorized_keys` (never commit real keys):

```sh
sudo install -d -m 0755 -o root -g root /etc/ssh/authorized_keys
sudo install -m 0644 -o root -g root examples/authorized_keys /etc/ssh/authorized_keys/claude-mcp
sudo install -m 0644 -o root -g root deploy/sshd_config.claude-mcp /etc/ssh/sshd_config.claude-mcp
sudo install -m 0644 -o root -g root deploy/claude-mcp-sshd.service /etc/systemd/system/claude-mcp-sshd.service
sudo /usr/sbin/sshd -t -f /etc/ssh/sshd_config.claude-mcp
sudo systemctl daemon-reload
sudo systemctl enable --now claude-mcp-sshd.service
```

Then allow TCP 22022 in UFW and in the Lightsail networking firewall. Do not
open administrative port 22 publicly for this bridge.

## Client setup

For the painless partner path, the owner creates a dedicated passphrase-less
ED25519 pair, binds the public half to the `partner` forced command, and sends
the private delivery copy through a controlled channel. The partner runs:

```sh
python3 scripts/setup_client.py --principal partner \
  --import-key /absolute/path/to/delivered-private-key --verify
```

The script pins the VPS host key and adds the exact stdio entry to the Codex
config. After successful verification, delete the delivery source and restart
Codex. If no key was delivered, running the script without `--import-key`
generates a dedicated local pair and prints only the `.pub` fallback for manual
authorization.

## Revocation

Remove one person's single line from
`/etc/ssh/authorized_keys/claude-mcp`. Their existing connection may finish; to
cut it immediately, terminate that principal's active MCP/Claude process. The
other key and Claude login do not need to change.

## Local development

```sh
./scripts/install.sh
.venv/bin/python -m pytest -q
```

Configuration is via `CLAUDE_MCP_*`; see `scripts/stdio-forced-command.sh` for
the production defaults. There is no hourly bridge call quota.
`CLAUDE_MCP_CONTINUE_BURST_LIMIT` defaults to 50 and is bounded to 1–100;
`CLAUDE_MCP_CONTINUE_BURST_WINDOW_SECONDS` defaults to 60 and is bounded to
1–3600 seconds. The burst guard is isolated by principal and MCP conversation;
blocked attempts are audited but do not enter the rolling count. Large package
caches remain inside the project.
