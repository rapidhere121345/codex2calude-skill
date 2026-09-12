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

1. From this skill directory, run `python3 scripts/setup_client.py`. Use
   `--principal owner` only for the server owner; the default is `partner`.
2. The script may generate a dedicated passwordless automation key, pin the
   server host key, and append the restricted stdio MCP entry to the user's
   Codex config. Never print, read, transmit, or ask for the private key.
3. Give the emitted `.pub` public key to the server administrator and stop for
   authorization. This out-of-band approval is mandatory; never attempt to
   self-register a key or weaken SSH authentication.
4. After the administrator confirms authorization, rerun the same command with
   `--verify`. Report the sanitized status returned by the MCP handshake.
5. Ask the user to restart Codex if the newly configured MCP or skill is not
   visible. Configuration changes do not alter the current process's tool
   catalog.

The setup script is fail-closed: it must not overwrite an existing key,
different host pin, or conflicting `claude_research` MCP entry. Diagnose such a
conflict and ask before changing it manually.

## Choose the interaction

- Use `claude_once` for one independent question that will not need follow-up.
- Use `claude_start` for a new logical task. Keep the returned
  `conversation_id` in the current Codex conversation.
- Use `claude_continue` when the user continues that same logical task. Never
  reuse its ID for an unrelated task or another user.
- Use `claude_close` when the logical task is finished or the user asks to end
  it. Closing the bridge mapping does not delete Claude's transcript.
- Use `claude_status` on first use or while diagnosing availability; do not call
  it before every request.

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
