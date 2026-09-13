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
