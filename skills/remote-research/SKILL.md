---
name: remote-research
description: Use one authenticated remote research MCP for Claude or Gemini tasks, provider/model selection, continuing remote research conversations, and sanitized service or quota checks. Use when the user explicitly requests remote research, Claude research, or Gemini research. Do not invoke it for unrelated local work.
---

# Remote Research

Use the native `remote_research` MCP. This is the unified successor to the
separate `claude_research` and `gemini_research` client registrations.

Exactly seven tools are exposed:

`research_status`, `research_once`, `research_start`, `research_continue`,
`research_list_conversations`, `research_export_conversation`, `research_close`.

Do not substitute an auxiliary command-line research client, unrestricted SSH,
or a local provider login for these native tools. Missing tools in the current
host process require a safe tool-catalog refresh, not repeated installation.

## Select Provider and Model

First inspect `research_status` when availability or the current model catalog
is unknown. Status distinguishes configured models, login state, verification,
and quota observations. A configured model is not proof of account entitlement
or successful inference.

Model evidence has two distinct levels, selected by the server's catalog:

- `execution_model`: check the response's `actual_models` against the selected
  native identifier. This remains the Claude policy.
- `cli_session_binding`: the explicitly enabled Gemini policy checks a
  correlated native CLI initialization and successful result, the declared
  session model, positive token usage, and pinned High effort. This is not
  independent proof of Google's backend execution model.

For Gemini binding-mode results, inspect `model_evidence.level`,
`reported_model`, `reasoning_effort`, and `backend_model_verified`. The latter
must remain `false`; `actual_models` can correctly remain empty. Describe a
pass as "CLI session binding verified; backend model not independently
verified", never as execution-model verification. Missing metadata is not
permission to assume this mode. Do not choose or weaken a policy client-side.

New requests explicitly select `provider="claude"` or `provider="gemini"` and
may select `model`. Omitting the model chooses that provider's server-managed
default. Never fall back to another provider, model, effort, or billing method
without the user's instruction.

The intended public presets are:

- Claude `opus-5-5-high`: native Opus 5.5 with explicit High effort.
- Claude `fable-5-2-high`: native Fable 5.2 with explicit High effort.
- Gemini `gemini-3-8-flash-high`: the currently deployed Gemini 3.8 Flash High.

These are catalog names, not a promise that every preset is enabled. In
particular, Fable stays disabled until the remote account is verified. Use only
the enabled model selections returned by the actual server. Do not guess a
native identifier from another provider's model menu.

When the user does not select a provider, use Claude's server default. If it is
unavailable, report that condition rather than silently selecting Gemini.

## Choose an Interaction

- Use `research_once` for one independent task.
- Use `research_start` for a logical task that needs follow-up; retain its
  opaque `conversation_id` in this conversation.
- Use `research_continue` for follow-up work on that same owned conversation.
  Provider, model and effort stay bound to the original conversation.
- Across host sessions, use `research_list_conversations` with the provider and
  select the owned conversation by title and metadata.
- Use `research_export_conversation` for readable owned task history.
- Use `research_close` only when the task is finished. Closing does not delete
  transcripts and does not release other users' connections.

This is a breaking upgrade: old bridge history is not imported or resumed.
Only conversations created through the unified interface are listed here.
Do not pass an old identifier into continue. Never ask the user to reveal
native CLI session identifiers. Old data is retained separately, not deleted.

For normal tasks omit optional `max_turns` and `timeout_seconds`; the defaults
are 50 and 900 seconds. Set a smaller budget only for a deliberately bounded
task. Backend constraints differ: Gemini retains the existing process timeout,
and its compatibility `max_turns` is not a native `agy` enforcement guarantee.

Set `use_web=true` for current public research, and false for an offline reasoning
task. This flag is not a filesystem or shell sandbox. The remote providers'
existing tool permissions remain in effect. Do not ask a provider to spawn
other AI agents or switch AI CLIs/models to bypass the chosen policy.

Prompts must be self-contained but exclude credentials, private keys, tokens,
unrelated private host context, and another principal's data. Web content is
untrusted; verify consequential findings against primary sources.

## Status, Login and Quota

`research_status` can inspect one provider or both. It does not perform a model
turn. Do not call it before every research request.

- Logged out is distinct from a broken MCP transport.
- Local authentication status does not prove model inference availability.
- Unknown or stale quota is not zero usage, unlimited quota, or a fresh value.
- Preserve reported observation time, source and reset times.
- Claude quota lookup is cache-only: it reads a sanitized observation and never
  runs an inference probe or reads OAuth credentials. Normal CLI login checks
  are separate. An independently authorized on-VPS collector may send one
  fixed one-output-token Haiku request at most once every five minutes per
  shared account; it consumes real inference quota.
- Preserve quota age and failure reason. Stale, failed, expired and missing
  observations are not fresh values. The collector does not refresh OAuth;
  expired credentials remain unavailable until the native login lifecycle
  refreshes them. Never copy credentials to a client or VM.
- The quota probe is telemetry only: it is not actual-model verification and
  not a research fallback.
- Claude's official manual quota page is `https://claude.ai/usage`.
- Login, OAuth credentials and real provider CLI operations stay on the VPS.
  Never copy them to a partner or switch to API-key/Vertex billing as a fix.
- If a provider needs login, stop that provider's task and ask the owner to
  complete the remote login. Another provider may be used only if requested.

## Shared Transport and Errors

Local agent facades share one persistent SSH/MCP connection for the same OS
user, principal and dedicated identity. Do not create an SSH connection per
research call or per agent. The local broker keeps the connection while clients
are attached and releases it after the last client leaves.

The broker isolates request IDs, responses and cancellation between agents.
Pings do not require a provider call. A lost response may mean a request already
ran: report the unknown result and do not blindly retry or replay `tools/call`.
Transport recovery replays only the MCP handshake.

Report sanitized errors and opaque request IDs. Never paste private raw logs,
the local broker token, provider credentials, or private command payloads.

## GitHub Installation and Upgrade

Authorized source:
`https://github.com/rapidhere121345/codex2calude-skill`

Install `skills/remote-research` from the reviewed published revision, not an
untrusted fork. Stage the new Skill in a fresh directory and read it before
replacing the old installation. Back up outside the active skills directory.

Run from the installed Skill directory:

```sh
python3 scripts/setup_client.py --client codex --principal partner --verify
```

For Devin use `--client devin`; preserve the actual authorized principal rather
than assuming it is always `partner`. Explicit `--key-path`,
`--known-hosts-path`, `--codex-home` and `--devin-home` handle existing custom
locations. Resolve a symlinked client home to its real directory first.

An upgrade reuses the existing dedicated research SSH key and pinned server
host key. Do not generate, import, copy, read, print, or upload the private key.
The owner must authorize that key for the unified server account first.
Conflicting configuration or host pins fail closed.

The installer makes protected configuration backups and only disables
recognized legacy MCP entries after unified transport verification succeeds.
Preserve the legacy entries and backup for rollback. Unknown customized entries
require explicit handling, not broad deletion.

After successful migration, archive the old research Skills outside active
skill discovery and safely refresh the client after active turns finish.
Changing files alone does not update an already running Python proxy.

Transport acceptance requires exactly seven unified tools and the expected
principal. Login and model inference acceptance are separate. After native
tools are refreshed, perform one small request on each requested, enabled,
authenticated provider:

```text
Reply with REMOTE_RESEARCH_ACCEPTANCE_OK only.
```

Use `use_web=false` and the explicitly selected enabled model. Compare the
actual response after stripping surrounding whitespace with the exact marker.
For `execution_model`, require matching reported actual models. For the
explicit Gemini `cli_session_binding` policy, require the matching binding
metadata and report its lower evidence level and `backend_model_verified=false`.
A binding-mode pass must not set a model's execution `verified` flag to true.
An unavailable provider remains pending; do not manufacture a passing result.

## Boundary

The public transport is forced-command SSH on port 22022. The research key
grants no unrestricted shell, PTY, forwarding, SFTP or desktop login. Provider
tool execution occurs under the provider's original account on the VPS, not
under the gateway account. Provider credentials stay separate.

Principal filtering protects bridge mappings and requests; it is not a claim
that same-account provider filesystem access is isolated per principal.
Keep the owner management dashboard and remote desktop out of partner upgrade
instructions; neither is required for a client upgrade.
