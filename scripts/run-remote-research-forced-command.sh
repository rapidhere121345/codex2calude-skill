#!/bin/sh
# SSH forced command for the researchmcp account. PRINCIPAL is bound by the
# authorized_keys command; the environment is rebuilt, never inherited.
set -eu
umask 077

PRINCIPAL=${1-}
case "$PRINCIPAL" in
    owner|partner|partner2|partner3|partner4) ;;
    *) exit 64 ;;
esac

exec /usr/bin/env -i \
    HOME=/var/lib/remote-research \
    PATH=/usr/local/bin:/usr/bin:/bin \
    REMOTE_RESEARCH_PRINCIPAL="$PRINCIPAL" \
    PYTHONNOUSERSITE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    /opt/remote-research-mcp/current/.venv/bin/remote-research-mcp
