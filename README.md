# Remote Research MCP

One Skill, one MCP, explicit provider and model selection.

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
verification state. Fable is a disabled candidate until the remote account
passes real verification. A name in configuration is not proof of availability.
Gemini's deployed preset is Flash, not an incorrectly relabeled Pro model.

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

Quota observations distinguish logged out, unknown, stale and available.
Transport verification is separate from provider login and model inference
acceptance. Do not interpret missing observations as zero usage or unlimited
quota.

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
