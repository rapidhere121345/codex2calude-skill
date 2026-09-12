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
2. From this skill directory, run `python3 scripts/setup_client.py`; Python 3.11
   or newer is required. Use `--principal owner` only for the server owner; the
   default is `partner`.
3. The script may generate a dedicated passwordless automation key, pin the
   server host key, and append the restricted stdio MCP entry to the user's
   Codex config. Never print, read, transmit, or ask for the private key.
4. Give the emitted `.pub` public key to the server administrator and stop for
   authorization. This out-of-band approval is mandatory; never attempt to
   self-register a key or weaken SSH authentication.
5. After the administrator confirms authorization, rerun the same command with
   `--verify`. Report the sanitized status returned by the MCP handshake.
6. Ask the user to restart Codex if the newly configured MCP or skill is not
   visible. Configuration changes do not alter the current process's tool
   catalog.

The setup script is fail-closed: it must not overwrite an existing key,
different host pin, or conflicting `claude_research` MCP entry. Diagnose such a
conflict and ask before changing it manually.

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
  `opus_quota`, `five_hour` and `seven_day` are shared plan windows that apply
  to Opus, while `seven_day_opus` is an optional additional Opus-specific
  window. These are shared-account values, not separate owner/partner quotas.
  Report used/remaining percentages and reset times as returned. If an optional
  window is `null` or quota is unavailable, say unknown rather than treating it
  as zero usage or unlimited capacity; mention `cached`/`stale` when relevant.

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
- Keep each prompt self-contained enough for Claude to act, but do not send
  credentials, private keys, tokens, or unrelated private context.
- Calls share one Claude subscription and may queue. Avoid redundant retries;
  report a timeout or quota error instead of silently opening a new session.

## Present the result

Attribute delegated findings to Claude and preserve any uncertainty. Verify
important current facts and source links before presenting them as confirmed.
Do not imply that this MCP can control the VPS, browse an interactive session,
or access arbitrary files: its boundary is Claude conversation and optional
`WebSearch`/`WebFetch` only.
