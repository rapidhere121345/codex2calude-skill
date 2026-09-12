#!/bin/sh
set -eu
umask 077

PRINCIPAL=${1-}
case "$PRINCIPAL" in
    owner|partner) ;;
    *) exit 64 ;;
esac

# Ignore client-supplied language/runtime secrets and use only server-owned
# paths and policy. Claude's OAuth login is read from ubuntu's home directory.
unset ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_CODE_OAUTH_TOKEN
unset CLAUDE_MCP_PRINCIPAL CLAUDE_MCP_PRINCIPALS CLAUDE_MCP_MODELS
unset CLAUDE_MCP_WORK_ROOT CLAUDE_BIN PYTHONPATH PYTHONHOME VIRTUAL_ENV
unset LD_PRELOAD LD_LIBRARY_PATH BASH_ENV ENV CDPATH SSH_ORIGINAL_COMMAND

HOME=/home/ubuntu
PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin
CLAUDE_BIN=/home/ubuntu/.local/bin/claude
CLAUDE_MCP_WORK_ROOT=/home/ubuntu/.local/share/claude-research-mcp
CLAUDE_MCP_PRINCIPAL=$PRINCIPAL
CLAUDE_MCP_PRINCIPALS=owner,partner
CLAUDE_MCP_MODELS='{"opus":{"cli_value":"opus","family":"opus","min_cli_version":"2.1.236"}}'
CLAUDE_MCP_REQUIRED_CLI_VERSION=2.1.236
CLAUDE_MCP_PROMPT_LIMIT=20000
CLAUDE_MCP_RATE_LIMIT_PER_HOUR=20
CLAUDE_MCP_CONCURRENCY=1
CLAUDE_MCP_OUTPUT_LIMIT=100000
CLAUDE_MCP_RESULT_LIMIT=100000
NO_COLOR=1

export HOME PATH CLAUDE_BIN CLAUDE_MCP_WORK_ROOT CLAUDE_MCP_PRINCIPAL
export CLAUDE_MCP_PRINCIPALS CLAUDE_MCP_MODELS CLAUDE_MCP_REQUIRED_CLI_VERSION
export CLAUDE_MCP_PROMPT_LIMIT CLAUDE_MCP_RATE_LIMIT_PER_HOUR
export CLAUDE_MCP_CONCURRENCY CLAUDE_MCP_OUTPUT_LIMIT CLAUDE_MCP_RESULT_LIMIT
export NO_COLOR

cd /home/ubuntu
exec /opt/claude-research-mcp/.venv/bin/claude-research-mcp
