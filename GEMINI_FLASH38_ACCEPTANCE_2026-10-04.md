# Gemini Flash 3.8 VPS upgrade acceptance

Accepted on 2026-10-04 (Asia/Shanghai), using the already-installed native
`gemini_research` MCP tools and Skill.

## Change and compatibility

The VPS now fixes inference to `gemini-3.8-flash-high` with `--effort high`.
The service account's real `agy models` catalogue confirmed that exact model
identifier. Antigravity CLI remains version 1.2.14.

Client model strings are compatibility inputs. `pro`, an omitted model, and
legacy concrete model names resolve to the same VPS policy. Existing native
conversations retain their context; stored model snapshots no longer select
the inference model. Response model validation uses the effective Flash policy.

The seven MCP tool definitions, including schemas and descriptions, are exactly
unchanged. Their canonical JSON SHA-256 is
`1049f442938664708dca9bf37630d6cb9b3f1be04d9f44fd6ad18b37611087ca`.
The installed Skill and client MCP configuration hashes also remained unchanged.
No client update, reinstall, re-registration, or host restart was used.

## Real acceptance

| Path | Existing client request | Result |
| --- | --- | --- |
| Status | `gemini_status` after the VPS switch | Logged in, eligible, CLI compatible, legacy `pro` alias ready |
| One-shot | `gemini_once(model="pro", use_web=false)` | Correct arithmetic result and `FLASH38_ONCE_OK` |
| Old conversation | `gemini_continue` on a session created before deployment with the old Pro snapshot | Recalled the earlier acceptance passphrase and returned `FLASH38_CONTINUE_OK` |
| New web conversation | `gemini_start(model="gemini-3.1-pro-high", use_web=true)` | Ignored the old model name, searched/read official documentation, and returned `FLASH38_WEB_OK` with its source link |

Sanitized observation of the three actual VPS inference processes confirmed
`--model gemini-3.8-flash-high --effort high` for offline once, offline resume,
and web start. The old session's stored snapshot still named
`gemini-3.1-pro-high`; its resumed process nevertheless used Flash. The new
conversation stored the Flash/high policy. The web conversation's completed
tool-step payloads contain `search_web` and `read_url_content`.

The CLI returned empty `actual_models` usage metadata. It remains empty:
configuration and process arguments are not fabricated into provider usage
reports. Model-selection evidence here is the authenticated catalogue, actual
CLI arguments, successful inference, and stored new-conversation policy.

Both temporary acceptance conversations were closed through native MCP after
verification, retaining their transcripts.

## Automated verification and deployment

`.venv/bin/python -m pytest -q --tb=short` completed with **317 passed**.
`git diff --check` passed. The tests cover ignored model/effort inputs, all
three inference paths, legacy snapshots, empty usage metadata, and rejection of
a reported model that differs from the effective policy.

Active release: `/opt/gemini-research-mcp/releases/20261004-flash38-v2`.
Previous release retained: `/opt/gemini-research-mcp/releases/20261002-output-4m-v1`.
The original model-policy file is retained under
`/opt/gemini-research-mcp/backup-20261004-flash38-v2/model-policy.conf`.

Deployment acquired the existing inference concurrency lock, confirmed no
running requests, atomically changed the release link, and recycled idle
Gemini MCP processes. The existing client transport reconnected successfully.
The forced SSH wrapper, shared SSH listener configuration, authorization keys,
and Claude bridge were unchanged.
