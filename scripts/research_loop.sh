#!/usr/bin/env bash
#
# research_loop.sh — Runs the v3 promotion harness continuously on Linux.
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/research-loop.log"
LOCK_FILE="$REPO_DIR/.research-loop.lock"

RESEARCH_SLEEP_SECONDS="${RESEARCH_SLEEP_SECONDS:-3600}"
RESEARCH_TIMEOUT_SECONDS="${RESEARCH_TIMEOUT_SECONDS:-1800}"
RESEARCH_BASELINE="${RESEARCH_BASELINE:-production_baseline}"
RESEARCH_CHALLENGER="${RESEARCH_CHALLENGER:-deepseek_v3}"
RESEARCH_DAYS="${RESEARCH_DAYS:-3}"
RESEARCH_WARM_UP="${RESEARCH_WARM_UP:-80}"
RESEARCH_FOLDS="${RESEARCH_FOLDS:-2}"
RESEARCH_BET_SIZE="${RESEARCH_BET_SIZE:-75}"
RESEARCH_MIN_EDGE="${RESEARCH_MIN_EDGE:-0.05}"
RESEARCH_MAX_EVAL_CONTEXTS="${RESEARCH_MAX_EVAL_CONTEXTS:-12}"

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
    echo "Another research_loop.sh instance is already running." >&2
    exit 1
fi

log "=== Research promotion loop starting ==="
log "Repo: $REPO_DIR"
log "Baseline: $RESEARCH_BASELINE"
log "Challenger: $RESEARCH_CHALLENGER"
log "Sleep between runs: ${RESEARCH_SLEEP_SECONDS}s"

run=0
while true; do
    run=$((run + 1))
    log "--- Research run $run ---"

    if (
        cd "$REPO_DIR" &&
        PYTHONPATH="$REPO_DIR" timeout "$RESEARCH_TIMEOUT_SECONDS" "$VENV_PYTHON" -u src/v3/promotion.py \
            --baseline "$RESEARCH_BASELINE" \
            --challenger "$RESEARCH_CHALLENGER" \
            --days "$RESEARCH_DAYS" \
            --warm-up "$RESEARCH_WARM_UP" \
            --folds "$RESEARCH_FOLDS" \
            --bet-size "$RESEARCH_BET_SIZE" \
            --min-edge "$RESEARCH_MIN_EDGE" \
            --max-eval-contexts "$RESEARCH_MAX_EVAL_CONTEXTS"
    ) 2>&1 | tee -a "$LOG_FILE"; then
        log "Research run $run completed successfully"
    else
        code=$?
        log "Research run $run failed with exit code $code"
    fi

    log "Sleeping ${RESEARCH_SLEEP_SECONDS}s until next research run..."
    sleep "$RESEARCH_SLEEP_SECONDS"
done
