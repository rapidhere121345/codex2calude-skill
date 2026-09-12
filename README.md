# claude-research-mcp

Private, restricted stdio MCP bridge for running Claude Code research jobs on a
remote server. Codex starts it through SSH; there is no HTTP listener and no
Tailscale dependency.

The bridge exposes exactly five tools:

- `claude_status()` — sanitized CLI, login, subscription, and model-policy status.
- `claude_once(...)` — one independent query.
- `claude_start(...)` — start a logical task and return an opaque
  `conversation_id`.
- `claude_continue(conversation_id, ...)` — resume that task with its original
  model and web policy.
- `claude_close(conversation_id)` — close the bridge mapping. It does not erase
  the Claude-side transcript.

## Conversation and model policy

Use one Claude session per logical task. `claude_start` stores the native Claude
session ID only on the server and returns a separate UUID. A continuation must
come from the same SSH principal (`owner` or `partner`).

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
- SQLite enforces principal ownership and rolling per-principal quotas. A
  process-wide flock limits shared-account concurrency.
- Responses and diagnostics are bounded. Raw Claude stderr is not returned.
- The shared Claude login remains in `/home/ubuntu`; neither user receives its
  OAuth material. The state directory must stay mode `0700`.

Web content is still untrusted. Prompt injection can influence research output,
so important findings should be verified.

## Install on the VPS

The expected production paths are:

```text
/opt/claude-research-mcp
/home/ubuntu/.local/share/claude-research-mcp
/etc/ssh/authorized_keys/claude-mcp
/etc/ssh/sshd_config.claude-mcp
```

After copying this project to `/opt/claude-research-mcp`:

```sh
sudo chown -R root:root /opt/claude-research-mcp
sudo chmod 0755 /opt/claude-research-mcp/scripts/*.sh
sudo /opt/claude-research-mcp/scripts/install.sh
sudo install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.local/share/claude-research-mcp
```

Install the dedicated SSH daemon configuration only after inserting real public
keys into `examples/authorized_keys`:

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

Each person generates their own key locally and sends only the `.pub` file to
the server administrator:

```sh
ssh-keygen -t ed25519 -a 100 -f ~/.ssh/claude_mcp
```

Use a trusted copy of the VPS ED25519 host public key to create a pinned
`known_hosts` file; do not approve a changing fingerprint. Copy
`examples/codex-config.toml` into the relevant Codex config and replace the key
and known-host paths. The same stdio MCP configuration works in Codex clients
that share `~/.codex/config.toml`.

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
the production defaults. Large package caches remain inside the project.
