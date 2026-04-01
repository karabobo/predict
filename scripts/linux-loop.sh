#!/usr/bin/env bash
#
# linux-loop.sh — Runs the Polymarket bot continuously on Linux.
# Intended for simple server/container deployment without GitHub Actions.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
SRC_DIR="$REPO_DIR/src"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/loop.log"
LOCK_FILE="$REPO_DIR/.linux-loop.lock"
SLEEP_SECONDS="${SLEEP_SECONDS:-300}"
CYCLE_TIMEOUT_SECONDS="${CYCLE_TIMEOUT_SECONDS:-300}"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Python venv not found: $VENV_PYTHON" >&2
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another linux-loop.sh instance is already running." >&2
    exit 1
fi

log "=== Polymarket bot loop starting ==="
log "Repo: $REPO_DIR"
log "Python: $VENV_PYTHON"
log "Sleep between cycles: ${SLEEP_SECONDS}s"
log "Cycle timeout: ${CYCLE_TIMEOUT_SECONDS}s"

cycle=0
while true; do
    cycle=$((cycle + 1))
    log "--- Cycle $cycle ---"

    if (cd "$SRC_DIR" && timeout "$CYCLE_TIMEOUT_SECONDS" "$VENV_PYTHON" -u ci_run.py) 2>&1 | tee -a "$LOG_FILE"; then
        log "Cycle $cycle completed successfully"
    else
        code=$?
        log "Cycle $cycle failed with exit code $code"
    fi

    log "Sleeping ${SLEEP_SECONDS}s until next cycle..."
    sleep "$SLEEP_SECONDS"
done
