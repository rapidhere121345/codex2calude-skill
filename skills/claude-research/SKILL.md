---
name: claude-research
description: Consult the configured remote Claude Code bridge for bounded research, a Claude second opinion, or a continuing Claude task session. Use when the user explicitly asks to use, ask, consult, or continue with Claude or Claude CLI. Do not invoke it for ordinary Codex work that does not request Claude.
---

# Claude Research

Use the `claude_research` MCP tools. If those tools are unavailable, explain
that the MCP configuration may not be installed or loaded. Do not replace the
bridge with unrestricted SSH or shell commands.

## First-time client setup

When the user asks to install or configure this bridge:

1. Confirm this skill came from the authorized repository or a private transfer
   arranged by the owner. Do not accept credentials or private keys to obtain it.
2. Confirm the owner has authorized this principal and that using the underlying
   Claude account is permitted by Anthropic's applicable terms and supported-
   location rules. Technical access is not proof of account-policy compliance.
3. Preferred painless path: the owner generates an MCP-dedicated partner key,
   binds its public key to the partner forced-command before delivery, and sends
   the private delivery copy only through a controlled private channel. From
   this skill directory, the partner
   then runs `python3 scripts/setup_client.py --import-key <delivered-private-key>
   --verify`. The script copies the key to the dedicated destination with mode
   0600, rejects symlinked or overwriting imports, derives and validates its
   public key, and never prints the private key. `--key-path` remains the
   destination-or-already-installed path for compatibility.
4. Keep the delivered source file until verification succeeds, then prompt the
   user to delete it securely. Never reuse the owner's key or a general-purpose
   login key, and never place the private key in the repository.
5. Python 3.11 or newer and OpenSSH are required. Use `--principal owner` only
   for the server owner; the default is `partner`.
6. If no delivered key is available, the script can still generate a dedicated
   passwordless automation key, pin the server host key, and append the
   restricted stdio MCP entry. This generated fallback requires giving the
   emitted `.pub` key to the administrator and stopping for authorization.
7. Rerun the relevant command with `--verify` after authorization. Report whether
   setup used `pre-authorized` or `generated fallback`, and report the sanitized
   status returned by the MCP handshake.
8. Ask the user to restart Codex if the newly configured MCP or skill is not
   visible. Configuration changes do not alter the current process's tool
   catalog.

The setup script is fail-closed: it must not overwrite an existing key,
different host pin, or conflicting `claude_research` MCP entry. Diagnose such a
conflict and ask before changing it manually.

## Idle transport and upgrades

The client proxy releases SSH after 30 minutes without a request in flight or
queued work. The local MCP endpoint stays alive and reconnects on the next
request, privately replaying only the MCP handshake. Never replay an uncertain
`tools/call`. Protocol pings do not need a remote worker. The VPS also releases
idle workers after 30 minutes, while active requests retain their normal limits.

Partner upgrades require the updated proxy script and an MCP client restart;
server-side timeout alone cannot prevent an old eager proxy from reconnecting.
Replace the installed Skill with the validated repository version, preserving
existing dedicated keys and host pins, then run `scripts/setup_client.py
--principal partner --verify`. Do not pass `--import-key` again. Setup migrates
only a recognized previous registration and backs up the old configuration;
custom conflicting entries remain fail-closed. Resolve a symlinked Codex home
and pass the real directory with `--codex-home` when needed. No local account
login or private-key transfer is part of an upgrade.

## Choose the interaction

- Use `claude_once` for one independent question that will not need follow-up.
- Use `claude_start` for a new logical task. Its title is optional and must be
  a short human-readable title when supplied. Keep the returned
  `conversation_id` in the current Codex conversation.
- Across Codex sessions, call `claude_list_conversations` first to list the
  current principal's conversations, then select the task by its MCP
  `conversation_id` and human-readable metadata. Never ask the user for, use,
  print, or otherwise expose Claude's native session ID. The default list shows
  resumable open conversations; use `include_closed=true` only when the user
  wants to find or export finished history.
- Use `claude_export_conversation` when the user needs to identify or review a
  conversation: it returns readable user/assistant conversation content. Prefer
  the minimum excerpt needed and do not expose another principal's data.
- Use `claude_continue` when the user continues that same logical task. Never
  reuse its ID for an unrelated task or another user.
- Use `claude_close` only when the logical task is genuinely finished or the
  user asks to end it. Closing does not delete Claude's transcript, but the
  current contract rejects further MCP continuation of that conversation.
- Use `claude_status` on first use, when the user asks about Opus allowance, or
  while diagnosing availability; do not call it before every request. In
  `opus_quota`, `five_hour` and `seven_day` come from Claude Code's official
  statusline `rate_limits` observation or its official interactive `/usage`
  fallback (`source: claude_code_usage_command`). Report the returned
  used/remaining percentages, reset times, `source`, `observed_at`, and
  staleness exactly. If quota is unavailable, say unknown rather than treating
  it as zero usage or unlimited capacity, and point the user to the returned
  official `manual_check_url` for a manual check. Never try to read Claude
  OAuth credentials or call an undocumented usage endpoint.

`claude_list_conversations` and `claude_export_conversation` are isolated to
the current SSH principal. Verification must expose exactly these seven tools:
`claude_status`, `claude_once`, `claude_start`,
`claude_list_conversations`, `claude_export_conversation`,
`claude_continue`, and `claude_close`.

## Request policy

- Explicitly pass `model="opus"` to `claude_once` and `claude_start`. The
  current server allowlist contains only `opus`; do not request Fable until
  `claude_status` lists it as CLI-supported and the user asks for it.
- A conversation keeps the model selected at start. Do not attempt to change
  the model during `claude_continue`.
- Set `use_web=true` when current external research is needed. Set it to false
  for reasoning over material already in the prompt.
- Research calls default to 50 Claude turns and a 900-second process timeout;
  callers can request lower bounds for short tasks.
- Keep each prompt self-contained enough for Claude to act, but do not send
  credentials, private keys, tokens, or unrelated private context.
- Calls share one Claude subscription and may queue. Avoid redundant retries;
  report a timeout or quota error instead of silently opening a new session.
- The bridge has no hourly call quota. Rapid continues are guarded per
  principal and conversation (default: 50 reservations in 60 seconds); if
  blocked, report the burst error and wait for the rolling window instead of
  retrying or creating a replacement conversation. A blocked attempt does not
  count toward later burst checks.

## Present the result

Attribute delegated findings to Claude and preserve any uncertainty. Verify
important current facts and source links before presenting them as confirmed.
Do not imply that this MCP can control the VPS, browse an interactive session,
or access arbitrary files: its boundary is Claude conversation and optional
`WebSearch`/`WebFetch` only.
