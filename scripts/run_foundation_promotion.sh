#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CHALLENGER="${1:-v6_foundation}"
shift || true

PYTHONPATH=. .venv/bin/python src/v3/promotion.py \
  --baseline production_baseline \
  --challenger "$CHALLENGER" \
  --days 14 \
  --warm-up 80 \
  --folds 4 \
  --bet-size 75 \
  --max-eval-contexts 0 \
  "$@"
