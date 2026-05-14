#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Python venv not found: $VENV_PYTHON" >&2
    echo "Create it with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

mkdir -p "$REPO_DIR/logs" "$REPO_DIR/.pm2"

cd "$REPO_DIR"
"$REPO_DIR/scripts/pm2ctl.sh" start ecosystem.config.cjs
"$REPO_DIR/scripts/pm2ctl.sh" save
"$REPO_DIR/scripts/pm2ctl.sh" status
