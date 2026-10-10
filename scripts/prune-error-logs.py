#!/usr/bin/env python3
"""Remove expired private diagnostics without reading or printing their content."""

from pathlib import Path
import sys

from claude_research_mcp.raw_error_logs import prune_raw_errors


def main() -> int:
    root = Path("/var/lib/claude-mcp/research")
    results = [prune_raw_errors(root, principal=role) for role in ("owner", "partner", "partner2")]
    if not all(results):
        print("Private diagnostic retention check failed; inspect storage permissions.", file=sys.stderr)
        return 1
    print("Private diagnostic retention check completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
