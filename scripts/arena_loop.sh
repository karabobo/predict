#!/usr/bin/env bash
set -euo pipefail

# Legacy arena loop kept for manual research runs. Production local loops are
# managed by PM2 via ecosystem.config.cjs.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Python venv not found: $VENV_PYTHON" >&2
    exit 1
fi

while true; do
    (
        cd "$REPO_DIR/src"
        "$VENV_PYTHON" -u ci_run.py
    )

    echo "--- Cycle complete. Waiting 5 minutes... ---"
    sleep 300
done
