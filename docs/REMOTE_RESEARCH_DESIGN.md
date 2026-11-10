# Unified Remote Research: Lead-Owned Contract

This is a breaking upgrade. It supersedes the per-skill 300-second idle-timeout
experiment. That experiment is preserved in a different worktree and must not
be copied into this release. There are no legacy API aliases or history imports.
Old files and databases are preserved as backups, not exposed as new sessions.

## Public Interface

The only new client Skill is `remote-research`; the MCP registration is
`remote_research`. It exposes exactly seven tools:

```python
research_status(provider: Literal["claude", "gemini"] | None = None)
research_once(prompt: str, provider: Literal["claude", "gemini"],
              model: str | None = None, use_web: bool = True,
              max_turns: int = 50, timeout_seconds: int = 900)
research_start(prompt: str, provider: Literal["claude", "gemini"],
               model: str | None = None, title: str | None = None,
               use_web: bool = True, max_turns: int = 50,
               timeout_seconds: int = 900)
research_continue(conversation_id: str, prompt: str,
                  max_turns: int = 50, timeout_seconds: int = 900)
research_list_conversations(provider: Literal["claude", "gemini"],
                            include_closed: bool = False,
                            limit: int = 50, offset: int = 0)
research_export_conversation(conversation_id: str, format: str = "markdown")
research_close(conversation_id: str)
```

The chosen provider is mandatory for new work; there is no provider fallback.
`model=None` resolves the selected provider's configured default. Unknown or
disabled models fail before backend execution. Status lists disabled models
truthfully; configuration is not proof of inference entitlement.

The exact MCP instructions are:

> Run bounded remote research with an explicit provider and model. Never change
> a conversation's provider, model or effort on continuation. Do not expose
> native session IDs or credentials.

Exact tool descriptions, in the order above:

- Return sanitized provider availability, model choices, and quota observations.
- Run one isolated research request with the selected provider and model.
- Start a research conversation with an immutable provider and model selection.
- Continue an owned conversation using its original provider and model.
- List owned unified conversations for one provider.
- Export readable content from one owned research conversation.
- Close one owned conversation mapping without deleting its transcripts.

Responses retain the provider's sanitized payload and add `provider`, the public
`model` selection, and `reasoning_effort` where known. `research_status` returns
top-level `principal` from trusted gateway settings independently of provider
login, plus `providers`, keyed by provider, with sanitized status, `quota`, configured
`default_model`, and model metadata. Claude's quota normalizes `opus_quota`;
Gemini's quota uses its existing `quota`. A failed provider must not suppress
another provider's status. Authentication and actual inference availability are
distinct. Never turn missing quota observations into zero or unlimited usage.

### Evidence Levels

Each root-owned catalog model has an `evidence_policy`. Its default is
`execution_model`, preserving the existing per-result `actual_models` check.
Only Gemini can explicitly use `cli_session_binding`, and that policy requires
catalog `verified=false`. This policy is server-owned, not a client override or
a new wire-selection field. A single catalog snapshot supplies both selection
validation and the evidence policy before a model call starts.

Binding acceptance requires one native `init` followed only by `step_update`
events and one terminal `result`; their conversation UUIDs must correlate
without being exported. The CLI-declared model must match the pinned native
selection; conflicting model declarations, failed or denied results, blank
responses, and absent/invalid/nonpositive usage fail closed. High effort is
explicitly pinned by the backend command and labelled as such, not reported as
an independently observed engine setting. Reported execution models, if
present, must still match; binding never overrides contradictory evidence.

An accepted binding response retains `actual_models=[]` and adds an exact
sanitized `model_evidence` object with level `cli_session_binding`,
`reported_model`, pinned effort and its source, successful correlation and
usage booleans, and `backend_model_verified=false`. It is usable session
binding evidence, not independent proof of the actual Google backend model.
Status exposes the configured policy separately from login, quota, and
execution verification. Clients must never promote binding to execution proof.

## Persistence and History

The gateway uses its own SQLite route map; original provider databases and
transcripts remain in their original homes. A gateway UUID never contains a
native provider session identifier. Every lookup is scoped by principal.

```sql
CREATE TABLE IF NOT EXISTS routes (
  id TEXT PRIMARY KEY,
  principal TEXT NOT NULL,
  provider TEXT NOT NULL CHECK(provider IN ('claude','gemini')),
  backend_id TEXT NOT NULL,
  model TEXT NOT NULL,
  model_cli_value TEXT NOT NULL,
  model_family TEXT NOT NULL,
  reasoning_effort TEXT,
  title TEXT,
  use_web INTEGER NOT NULL CHECK(use_web IN (0,1)),
  created_at TEXT NOT NULL,
  last_activity_at TEXT NOT NULL,
  closed_at TEXT,
  UNIQUE(principal,provider,backend_id)
);
```

Route access uses `WHERE id=? AND principal=?`, never an unscoped UUID lookup.
Successful start registers its backend UUID before returning a gateway UUID.
Continue/export/close resolve this mapping and do not accept a new provider,
model, or effort. Closed routes reject continuation but remain exportable.
Close updates the gateway only after backend close succeeds.

Load the catalog afresh for status/new work/continuation. Before continuation,
the original public model must still be enabled and its native model, family
and effort must match the recorded snapshot. A changed or revoked policy fails
explicitly rather than selecting a different model. Changing only the provider
default does not change an existing conversation.

List reads only this gateway's principal/provider-scoped route map, sorted by
last_activity_at descending and id for stable pagination, with validated limit
and offset. Start records title/use_web/activity. Successful continuation
updates activity. There is no legacy history discovery, import, or ID fallback.
Unknown old IDs fail as unavailable. Do not delete old provider databases.

## Transport

One local broker per OS user, principal, SSH identity, pinned host and deployment
configuration. All local agent stdio facades share that broker's single SSH
process and remote MCP session. An attached facade keeps the shared connection
alive even when idle; do not reconnect every few minutes.

The local broker persists for 60 seconds after its last facade detaches, then
closes its SSH child and exits. Multiple facades must not create parallel SSH
children, including during simultaneous cold start.

Local IPC is loopback TCP with a random 256-bit token in a private metadata file.
Use mode 0700 directories / 0600 files on POSIX and a current-user-only ACL on
Windows; fail closed when the directory/ACL cannot be secured. Never put that
token in argv, logs, MCP results, or Git. Symlinked or foreign-owned state is
rejected. Startup uses an interprocess file lock (fcntl on POSIX, msvcrt on
Windows), not a best-effort PID check.

The facade CLI is:

```text
python3 scripts/remote_research_client.py --config /absolute/private/client.json
```

The private, versioned config contains `principal` and `ssh_command` argv.
No shell string is executed. The SSH argv remains dedicated-key, strict-pinned,
BatchMode, no forwarding/no PTY, to researchmcp@44.255.229.140 port22022.

The broker remaps every client request ID; simultaneous clients may reuse IDs.
It shares one remote initialize handshake, answers subsequent facade initialize
from the cached handshake, and swallows private handshake replay responses.
Retain the last successful initialize metadata across transport loss so a new
facade can initialize while offline; subsequent real work reconnects privately.
When no successful metadata exists, an explicit initialize may acquire it with
the same bounded backoff. A rejected remote initialize retires that SSH child.
Ping is answered locally. Cancellation targets only that facade's remapped
request. Facade EOF never cancels another facade's work.

Progress tokens, if supported, are namespaced and routed to their owner;
unscoped sensitive notifications are dropped rather than broadcast. Unknown
server-initiated requests are rejected, never sent to an arbitrary agent.

An unexpected SSH loss returns explicit unknown-result errors for already
written requests. It must never replay a tools/call. Unsent work can wait for a
new transport, with a 30-second queue expiry; reconnect requires actual queued
work, not a ping or timer. Reconnect replays only the handshake.

Limits: 64 attached facades, 128 pending/queued requests, 8 MiB queued bytes,
32 MiB maximum framed line. Oversize or unauthenticated IPC clients are closed.
Per-facade output pumps are bounded to 128 frames / 32 MiB, with a 128 MiB shared
output-byte budget. A pump may wait two seconds for its local drain before
disconnecting only that facade. The shared SSH reader never waits on a facade's
drain, including progress notifications.
These are resource controls, not model budgets or subscription quotas.

## Provider Separation

New forced-command SSH account: `researchmcp`. Keys bind a fixed principal.
It cannot read either provider's OAuth home. It exposes only unified MCP.

Production's SSH service enforces `NoNewPrivileges=yes`. Keep that protection:
do not invoke sudo from an SSH gateway or relax the existing service sandbox.

A separate root-managed broker listens only on
`/run/remote-research/provider.sock`. It accepts a peer only when Linux
`SO_PEERCRED` reports the `researchmcp` UID. Directory root:researchmcp 0750,
socket root:researchmcp 0660. No TCP listener or arbitrary shell operation.

Gateway invocation is one bounded JSON line over this socket:
`{"provider":"claude","principal":"owner","tool":"claude_status","arguments":{}}`.
Provider, principal and tool must be independently allowlisted before any spawn.
The broker uses a fixed executable and `subprocess`'s explicit `user`, `group`
and empty supplementary groups to drop to the existing provider account.
It does not use a caller-selected executable or `preexec_fn`. At most eight
handler connections may run concurrently. A quiet valid model request must not
block another provider or be mistaken for a dead helper.

For once/start/continue the envelope also includes a trusted `selection`:

```json
{"public_model":"opus-5-5-high","cli_value":"claude-opus-5-5",
 "family":"opus","reasoning_effort":"high"}
```

Forward this metadata unchanged to the provider helper. Immediately before
execution, the helper compares it against the current root catalog and the
actual backend Settings policy. An update between gateway selection and helper
startup must fail closed, never silently change native model or effort.
Only a catalog-resolved backend model is passed to an inference function.

Provider status payloads must not overwrite the unified catalog's public
`models` or `default_model`. Provider status checks run independently/concurrently
with a 60-second end-to-end budget including semaphore waiting; busy is unknown
availability, not zero quota.

Provider RPC helpers run as the original `claudemcp` or `geminimcp` account.
A helper takes only a fixed provider and
validated principal, reads one bounded JSON request from stdin, invokes an
allowlisted existing tool function, and writes one sanitized JSON result.
It cannot select arbitrary executables, shell commands, paths or environment.

Allowed operations map to the original seven tool names for that provider.
Helpers use the existing app creation and function seam already used by tests:
`app._tool_manager._tools[tool].fn(**arguments)`. No native MCP client or extra
SSH session is needed per provider request.

There are no idle provider helper processes. They exist only for actual calls.
Existing per-account global concurrency file locks remain authoritative; the
gateway also permits at most one running helper per provider in its process.
On caller cancellation, closing helper stdin cancels its call and the helper's
same-UID cleanup terminates the provider CLI process group. Do not rely solely
on a cross-UID kill from the gateway. Gateway cancellation closes the Unix
connection; the privileged broker closes the helper's stdin and supervises
cleanup. The privileged broker has only CAP_SETUID, CAP_SETGID and CAP_KILL
(the latter is required for cross-UID stuck-helper supervision), and retains
NoNewPrivileges, strict filesystem protections and restricted writable roots.
The 15-second helper cleanup grace is not an inference inactivity timeout.
Initial envelope reads have a five-second deadline; normal inference keeps its
configured execution budget. Bound every input/output chunk, not just newlines.

The provider helper installs its rebuilt environment before loading/running a
backend. It must return a result while its stdin remains open; canceling an EOF
watcher must not leave an executor thread blocking process shutdown.

Helper startup builds a clean environment with each original provider HOME,
work root, CLI binary, policy and telemetry settings. No inherited API keys,
SSH_CONNECTION, gateway account state, or client-supplied model environment.

## Client Upgrade

One new `skills/remote-research` install. The setup command supports
`--client codex|devin`, `--principal`, `--key-path`, `--known-hosts-path`,
`--codex-home`, `--devin-home`, and `--verify`.

Reuse an already installed dedicated research key only after server-side
authorization for the new account; do not generate, import, copy or print an
existing private key during upgrade. Preserve its host pin.

Codex writes only its `mcp_servers.remote_research` TOML table; Devin writes only
`mcpServers.remote_research` in `mcp_config.json`. Preserve unrelated settings,
make protected backups, write atomically, and reject conflicting registrations.

Only after unified transport verification, disable the two recognized obsolete
research entries (`enabled=false` for Codex / `disabled=true` for Devin).
Keep protected rollback backups. No old tool aliases remain in the new runtime.
Unknown customized entries must not be silently overwritten or disabled.
Existing running hosts/proxies need a
safe refresh after their turns finish; editing config does not update a running
Python process.

Transport verification requires exactly the seven unified tools and matching
principal. Provider login, model smoke acceptance, and transport acceptance are
reported separately. Failed verification leaves the old configuration usable.

## Model Policy

Public Claude presets: `opus-5-5-high` and `fable-5-2-high`. Their intended native
CLI values are `claude-opus-5-5` and `claude-fable-5-2`, with explicit effort
`high`. The latter is a candidate until confirmed by the actual remote account;
it must be advertised disabled until real verification, never silently replaced.

The existing Gemini deployment binds legacy `pro` to native
`gemini-3.8-flash-high` with effort `high`. The unified public preset is
`gemini-3-8-flash-high`; do not rename it Pro in the new UI.

New Claude conversations snapshot native model, family and effort, as Gemini
already does. The unified gateway does not resume legacy records.

## Deployment Boundaries

Existing provider accounts and data remain separate and are not deleted.
Obsolete public client entrypoints do not receive API compatibility support.
Do not revoke keys or interrupt existing inference without explicit approval.
No public HTTP API, browser login material, provider credential export, or RDP
exposure beyond Tailscale is introduced. RDP and new privileged gateway setup
require explicit deployment approval and root-owned reversible backups.
