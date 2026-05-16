#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

export PREDICT_RULE_PROFILE="${PREDICT_RULE_PROFILE:-v8_broad_paper_candidate}"
export POLYMARKET_PAPER_TRADING="${POLYMARKET_PAPER_TRADING:-1}"
export POLYMARKET_PAPER_MIN_EDGE="${POLYMARKET_PAPER_MIN_EDGE:-0.01}"
export PYTHONPATH="${PYTHONPATH:-.:src}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"

exec .venv/bin/python src/v8_realtime_daemon.py "$@"
