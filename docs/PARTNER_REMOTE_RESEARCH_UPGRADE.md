# Partner Upgrade: Unified Remote Research

This is a breaking client upgrade. There is one new Skill and one new MCP;
there is no old tool-name emulation or historical conversation import.
Provider credentials and desktop/management access are not part of this upgrade.

## Message for Users

After upgrading, simply ask your assistant to use **Gemini 3.8**, **Opus 5.5**,
or **Fable 5.2**, followed by your question. You can choose a different model
for a new task; ask to continue the same task for follow-up questions.

There is one remote research entry now. You do not need to log in to the
providers, run model tests, or handle server configuration. Your assistant
handles the setup checks. Contact the service owner if it cannot complete a
request; it will not silently switch to a different model.

The procedure below is for the assistant performing the upgrade, not a checklist
that an end user must work through manually.

Distribute this prompt only after the owner publishes the reviewed commit and
authorizes existing partner keys for the unified endpoint. The owner may supply
an immutable commit SHA; use it instead of assuming a moving branch was reviewed.

## Prompt

```text
Upgrade this computer to the unified remote-research Skill and remote_research
MCP from this authorized GitHub repository:
https://github.com/rapidhere121345/codex2calude-skill

This is a BREAKING upgrade. Do not preserve old API aliases, import old research
conversations, or keep two active Claude/Gemini research client registrations.
Do preserve old files and configuration in protected rollback backups.

1. Identify the actual MCP host (Codex or Devin), active configuration scopes,
   installed Skill directories, authorized principal, and existing dedicated
   research SSH key and pinned known_hosts PATHS. Never read, print, copy,
   upload, or regenerate the private key. Do not assume the principal is owner
   or that every partner uses the name partner; use the existing authorization.
   If any of these facts is ambiguous, ask instead of overwriting configuration.

2. Fetch the repository into a fresh staging checkout from the exact GitHub URL
   above. If the owner supplied a reviewed commit SHA, check out that commit
   and report it; otherwise record the exact published main commit you obtained.
   Do not use an untrusted fork, download a credential ZIP, or install a local
   Claude/Gemini/Antigravity provider to replace the bridge.

3. Read skills/remote-research/SKILL.md and the root README from that checkout.
   Install ONLY the new remote-research Skill in this host's active Skill root.
   If it already exists, back it up OUTSIDE active Skill discovery before
   replacing it. Do not overlay files and leave an old proxy behind.

4. Preserve unrelated host configuration and take protected backups. Resolve a
   symlinked client home to its actual directory and pass that explicit path.
   From the installed new Skill run:

   python3 scripts/setup_client.py --client <codex-or-devin> \
     --principal <actual-principal> --verify

   Supply --key-path and --known-hosts-path for the existing dedicated key/pin
   when necessary, plus --codex-home or --devin-home for nondefault locations.
   Do not use --import-key, create a new key, disable host-key verification,
   broaden SSH permissions, or modify server-side accounts from this computer.
   If the existing key is not yet authorized for the unified endpoint, stop
   and ask the owner; do not try another principal or borrow an owner's key.

5. Require transport verification to report the expected principal and EXACTLY:
   research_status, research_once, research_start, research_continue,
   research_list_conversations, research_export_conversation, research_close.

   Provider authentication/model availability is separate from transport.
   A provider requiring remote login is not evidence that installing another
   client or retrying OAuth locally will fix it.

6. Only after verification succeeds, disable the obsolete claude_research and
   gemini_research registrations in the applicable active scopes. The installer
   handles recognized entries; inspect/report customized or managed-scope
   conflicts rather than deleting them blindly. Do not leave duplicate active
   registrations. If verification fails, preserve or restore the old working
   configuration and report the sanitized error.

7. Archive the old claude-research/gemini-research Skill directories outside the
   active Skill root. Do not delete transcripts, private keys, workspaces, or
   unrelated Skills. Safely refresh/restart the MCP host after active turns
   finish so old proxy processes actually exit. Configuration edits alone are
   not runtime replacement; do not kill an entire desktop or all Python/SSH.

8. After the native unified tools appear, inspect research_status. Offer only
   enabled provider/model choices returned by that server. Do not advertise
   Fable 5.2 as available merely because a catalog entry exists; do not rename
   the deployed Gemini Flash preset Pro.

9. For each provider the user wants and that is enabled/authenticated, make one
   tiny native research_once call with the explicit provider/model,
   use_web=false, and:
   Reply with REMOTE_RESEARCH_ACCEPTANCE_OK only.

   Check that the actual reply, stripping surrounding whitespace, matches the
   marker. Respect the server's evidence_policy. Claude execution_model requires
   matching actual_models. Gemini cli_session_binding requires matching
   model_evidence with level=cli_session_binding, the selected reported_model
   and High effort, and backend_model_verified=false. Report this as CLI session
   binding verification, not proof of the backend's actual execution model;
   actual_models can remain empty and execution verified must remain false.
   Never infer a binding pass from missing metadata. Do not use an auxiliary
   command-line bridge client for inference acceptance. An unavailable provider
   stays explicitly pending; do not silently switch provider, model, or policy.

10. Keep the installed Git commit, host/principal, backup paths, seven-tool
    verification, provider/model acceptance results, and restart/pending login
    requirements in a local upgrade report. Tell the user briefly whether the
    upgrade is ready or needs the service owner's attention, and how to choose
    the three models. Provide technical details only when requested or needed
    for support. Never include private key contents, provider tokens, broker
    tokens, raw private logs, or full configuration files.

The new transport shares one long-lived SSH connection between local agents
using the same user/principal/identity. Do not add an SSH connection per agent
or research call. The owner dashboard and remote desktop are not partner
upgrade requirements and must not be requested.
```

## Acceptance Boundaries

The prompt does not authorize deleting old data, exposing RDP, granting a new
principal, or moving provider credentials. A transport-only installation is
reported as such until native model acceptance succeeds. Unknown results after
a lost response must not be blindly replayed.
