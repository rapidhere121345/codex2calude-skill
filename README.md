# Remote Research MCP

One Skill, one MCP, explicit provider and model selection.

## Everyday Use

Ask your assistant to use **Gemini 3.8**, **Opus 5.5**, or **Fable 5.2** for a
task. The assistant selects the matching remote preset; no provider login,
manual model acceptance, or knowledge of internal CLI details is required.
For follow-up questions, ask it to continue the same research task.

The three models remain separate choices. The service does not silently
substitute another model when the selected model refuses or is unavailable.

This is a **breaking upgrade** from the separate Claude/Gemini research clients.
It does not emulate old tool names or import old conversations. Old data is not
deleted. Existing provider accounts and OAuth homes remain separate on the VPS.

## Client

Install only [`skills/remote-research`](skills/remote-research/SKILL.md).
Partner upgrade instructions:
[`docs/PARTNER_REMOTE_RESEARCH_UPGRADE.md`](docs/PARTNER_REMOTE_RESEARCH_UPGRADE.md).

From the installed Skill directory:

```sh
python3 scripts/setup_client.py --client codex --principal partner --verify
# For Devin, select --client devin instead.
```

Use the actual authorized principal and the existing dedicated research key.
The server must authorize that key for the unified account before migration.
No provider login, private-key transfer, API-key billing, or backend Python
dependency installation is required on a partner computer.

The installer preserves unrelated settings, takes protected backups, verifies
the new transport, and then disables recognized obsolete MCP entries. Refresh
the host after active turns finish; editing files does not replace running
proxies. Archive the two old Skills outside active discovery.

## Unified API

MCP name: `remote_research`. Exactly seven tools:

| Tool | Selection |
| --- | --- |
| `research_status` | One provider, or both |
| `research_once` | Provider and optional model |
| `research_start` | Provider and optional model; creates a unified conversation |
| `research_continue` | Owned unified conversation ID; original selection retained |
| `research_list_conversations` | Provider; new unified conversations only |
| `research_export_conversation` | Owned unified conversation ID |
| `research_close` | Owned unified conversation ID; retains transcripts |

Providers are `claude` and `gemini`. A missing model uses that provider's
server-managed default. There is no automatic provider/model fallback.

Intended catalog:

- `claude` / `opus-5-5-high`
- `claude` / `fable-5-2-high`
- `gemini` / `gemini-3-8-flash-high`

`research_status` is authoritative for enabled selections, account login and
verification state. Model activation and acceptance are managed server-side;
a name in configuration is not proof of availability.
Gemini's deployed preset is Flash, not an incorrectly relabeled Pro model.

The catalog distinguishes `execution_model` evidence (Claude) from explicitly
enabled `cli_session_binding` evidence (Gemini). Antigravity's headless result
reports token usage but not an execution-model identifier. Gemini therefore
validates its native CLI session declaration, correlated successful completion,
and pinned model/High effort without inventing `actual_models`. Its response
includes `model_evidence.level="cli_session_binding"` and
`backend_model_verified=false`; catalog `verified` stays false. This is a usable
CLI-binding acceptance level, not independent proof of Google's backend model.
Clients must preserve that distinction and explain it when relevant to a
verification claim, rather than include infrastructure details in every answer.

Normal request defaults are `use_web=true`, `max_turns=50`, and
`timeout_seconds=900`. Provider-specific execution constraints still apply.
Native provider session IDs and credentials never belong in client responses.

## Shared Connection

All local agent facades with the same user, principal, identity and pinned
target share one broker and one persistent SSH/MCP connection. Requests and
cancellation are isolated by client; reused JSON-RPC IDs cannot cross agents.
The connection stays alive while clients are attached and is released after
the final client leaves.

A failed in-flight call is never replayed. Transport recovery privately repeats
only the MCP handshake. Unknown results remain unknown.

The local broker binds loopback only and authenticates clients with a protected
random token. There is no new public HTTP API.

## Server

The unified forced-command account routes calls through a restricted Unix-socket
dispatcher into the original provider accounts. OAuth credentials are neither
merged nor exported. Existing SSH `NoNewPrivileges` hardening is preserved.
Provider helper processes are short-lived rather than per-agent idle workers.

Architecture and API contract:
[`docs/REMOTE_RESEARCH_DESIGN.md`](docs/REMOTE_RESEARCH_DESIGN.md).
Catalog and dispatcher unit are in `deploy/`; model activation is an operator
decision, not a client override.

The Claude helper executes `/opt/claude-code/current/claude`, a root-managed
pointer to an installed, pinned native build. Updating a user's own
`~/.local/bin/claude` alone does not upgrade the service. Install the verified
build under `/opt/claude-code/`, update that pointer, and keep the previous
build for rollback. Fable 5.2 requires Claude Code 2.1.290 or newer; its catalog
minimum prevents an enabled preset from using an older CLI.

Claude subprocesses must disable automatic model substitution, including
refusal-triggered switching. A refusal remains a refusal on the selected model;
it must not become a successful answer from another model.

Quota observations distinguish logged out, unknown, stale and available.
Transport verification is separate from provider login and model inference
acceptance. Do not interpret missing observations as zero usage or unlimited
quota.

Claude quota lookup is cache-only: it reads a sanitized on-VPS observation
and never runs an inference probe or reads OAuth credentials — normal CLI
login checks are separate. An independently authorized collector may send one
fixed one-output-token Haiku request at most once every five minutes per
shared account; it consumes real inference quota and shares the account-wide
rate limit with research calls. The collector does not refresh OAuth: expired
credentials keep reporting unavailable until the native login lifecycle
refreshes them. Status reports only the cached observation, its age,
staleness, and sanitized failure reason; credentials remain on the VPS and
are never copied to clients. A stale, failed, expired or missing observation
stays distinct from a fresh value, and the probe is quota telemetry only: it
is not actual-model verification and not a research fallback.

## Development

The server uses Python 3.12 and the repository's MCP dependencies. Client
transport/setup scripts use Python 3.11+ and standard-library modules.
OpenSSH and a previously authorized dedicated research key are required.

Tests use fake transports and fake provider CLIs by default; they must not
silently consume a real provider subscription. Real acceptance runs on the VPS
through the native unified MCP after explicit deployment and login.

The prior independent interfaces are archived in
[`docs/LEGACY_README.md`](docs/LEGACY_README.md). Its relative paths refer to the
original repository root; it is not the supported client upgrade guide.
