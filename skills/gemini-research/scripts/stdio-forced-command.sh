#!/bin/sh
set -eu
umask 077

PRINCIPAL=${1-}
case "$PRINCIPAL" in
    owner|partner) ;;
    *) exit 64 ;;
esac

# A fresh environment prevents SSH clients from changing provider or billing
# mode. Personal Google OAuth credentials stay on this VPS service account.
cd /var/lib/gemini-mcp
# Bind this root-managed policy only after checking the authenticated account's
# exact Pro identifier and highest supported effort. The explicit selection
# gate reports pending without sending prompts while the value is `auto`.
GEMINI_PRO_CLI_VALUE=auto
GEMINI_PRO_EFFORT=max
if [ -f /etc/gemini-mcp-model-policy.conf ]; then
    . /etc/gemini-mcp-model-policy.conf
fi
case "$GEMINI_PRO_CLI_VALUE" in
    ''|*[!A-Za-z0-9_.:-]*) exit 78 ;;
esac
case "$GEMINI_PRO_EFFORT" in
    low|medium|high|max) ;;
    *) exit 78 ;;
esac
MODEL_POLICY=$(printf '{"pro":{"cli_value":"%s","family":"gemini","min_cli_version":"1.2.14","reasoning_effort":"%s"}}' "$GEMINI_PRO_CLI_VALUE" "$GEMINI_PRO_EFFORT")
exec /usr/bin/env -i \
    HOME=/var/lib/gemini-mcp \
    PATH=/opt/antigravity/current/bin:/usr/local/bin:/usr/bin:/bin \
    AGY_BIN=/opt/antigravity/current/bin/agy \
    GEMINI_MCP_WORK_ROOT=/var/lib/gemini-mcp/research \
    GEMINI_MCP_PRINCIPAL="$PRINCIPAL" \
    GEMINI_MCP_PRINCIPALS=owner,partner \
    GEMINI_MCP_MODELS="$MODEL_POLICY" \
    GEMINI_MCP_DEFAULT_MODEL=pro \
    GEMINI_MCP_REQUIRE_EXPLICIT_MODEL=1 \
    GEMINI_MCP_REQUIRED_CLI_VERSION=1.2.14 \
    GEMINI_MCP_PROMPT_LIMIT=20000 \
    GEMINI_MCP_CONTINUE_BURST_LIMIT=50 \
    GEMINI_MCP_CONTINUE_BURST_WINDOW_SECONDS=60 \
    GEMINI_MCP_CONCURRENCY=1 \
    GEMINI_MCP_OUTPUT_LIMIT=100000 \
    GEMINI_MCP_RESULT_LIMIT=100000 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    NO_COLOR=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    /opt/gemini-research-mcp/current/.venv/bin/gemini-research-mcp
