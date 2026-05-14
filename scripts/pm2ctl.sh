#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PM2_HOME="$REPO_DIR/.pm2"
PM2_BIN="$REPO_DIR/node_modules/.bin/pm2"

if [ ! -x "$PM2_BIN" ]; then
    echo "pm2 not installed locally. Run: npm install pm2 --no-save" >&2
    exit 1
fi

mkdir -p "$PM2_HOME" "$REPO_DIR/logs"
export PM2_HOME

exec "$PM2_BIN" "$@"
