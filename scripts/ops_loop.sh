#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
SRC_DIR="$REPO_DIR/src"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/ops_loop.log"
LOCK_FILE="$REPO_DIR/.ops-loop.lock"
SLEEP_SECONDS="${OPS_SLEEP_SECONDS:-300}"
CYCLE_TIMEOUT_SECONDS="${OPS_TIMEOUT_SECONDS:-300}"

mkdir -p "$LOG_DIR"

log() {
    echo "[$(TZ=America/New_York date '+%Y-%m-%d %I:%M:%S %p ET')] $*" | tee -a "$LOG_FILE"
}

if [ ! -x "$VENV_PYTHON" ]; then
    echo "Python venv not found: $VENV_PYTHON" >&2
    exit 1
fi

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "Another ops_loop.sh instance is already running." >&2
    exit 1
fi

log "=== Ops loop starting ==="
log "Repo: $REPO_DIR"
log "Sleep between cycles: ${SLEEP_SECONDS}s"
log "Cycle timeout: ${CYCLE_TIMEOUT_SECONDS}s"

cycle=0
while true; do
    cycle=$((cycle + 1))
    log "--- Ops cycle $cycle ---"

    if (cd "$SRC_DIR" && timeout "$CYCLE_TIMEOUT_SECONDS" "$VENV_PYTHON" -u ops_cycle.py) 2>&1 | tee -a "$LOG_FILE"; then
        log "Ops cycle $cycle completed successfully"
    else
        code=$?
        log "Ops cycle $cycle failed with exit code $code"
    fi

    log "Sleeping ${SLEEP_SECONDS}s until next ops cycle..."
    sleep "$SLEEP_SECONDS"
done
