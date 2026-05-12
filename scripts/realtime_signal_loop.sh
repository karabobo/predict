#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
SRC_DIR="$REPO_DIR/src"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/realtime_signal_loop.log"
LOCK_FILE="$REPO_DIR/.realtime-signal-loop.lock"

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
    echo "Another realtime_signal_loop.sh instance is already running." >&2
    exit 1
fi

log "=== Realtime signal loop starting ==="
log "Repo: $REPO_DIR"
log "Sleep: ${PREDICT_REALTIME_SLEEP_SECONDS:-5}s"
if [ "${PREDICT_SHADOW_RULE_PROFILE:-}" = "" ] || [ "${PREDICT_SHADOW_RULE_PROFILE:-}" = "shadow_coach_candidates" ]; then
    export PREDICT_SHADOW_RULE_PROFILE="absorption_candidates_live"
fi
log "Shadow profile: ${PREDICT_SHADOW_RULE_PROFILE}"

cd "$SRC_DIR"
exec "$VENV_PYTHON" -u realtime_signal.py 2>&1 | tee -a "$LOG_FILE"
