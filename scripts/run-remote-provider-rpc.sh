#!/bin/sh
# Root-owned provider RPC helper. Fixed argv only: PROVIDER PRINCIPAL.
# Resets the environment; provider_rpc derives policy from the catalog.
set -eu
umask 077

PROVIDER=${1-}
PRINCIPAL=${2-}
case "$PROVIDER" in
    claude|gemini) ;;
    *) exit 64 ;;
esac
case "$PRINCIPAL" in
    owner|partner|partner2|partner3|partner4) ;;
    *) exit 64 ;;
esac

exec /usr/bin/env -i \
    PATH=/usr/local/bin:/usr/bin:/bin \
    HOME=/nonexistent \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    /opt/remote-research-mcp/current/.venv/bin/python \
    -m remote_research_mcp.provider_rpc "$PROVIDER" "$PRINCIPAL"
