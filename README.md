# Polymarket BTC 5-Min Prediction Bot

This repository runs a deterministic production strategy for Polymarket BTC 5-minute markets and a separate research loop for challenger evaluation. Production is rule-based; AI is restricted to research-only challengers such as `deepseek_v3`.

## Current Architecture

```text
src/fetch_markets.py   -> active BTC 5-min markets from Gamma API
src/predict.py         -> production baseline signal + regime filter + optional Telegram alert
src/score.py           -> resolve markets, score calls, compute P&L
src/dashboard.py       -> static dashboard and local web view
src/v3/promotion.py    -> sample-out baseline vs challenger evaluation
src/notifier.py        -> Telegram notifications
```

The repo now has three distinct concerns:
- Production: `contrarian_rule` baseline, scoring, dashboard, optional live trading.
- Research: blocked time-series promotion runs stored in `data/v3_research.db`.
- Reporting: `docs/index.html` plus `docs/research/latest.md`.

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional environment:
- `SILICON_FLOW_KEY` for `deepseek_v3` research runs
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for notifications

## Common Commands

```bash
python -m pytest tests/ -v
cd src && python ci_run.py
cd src && python dashboard.py
PYTHONPATH=. python src/v3/promotion.py --challenger deepseek_v3 --days 3 --warm-up 80 --folds 2
```

For the local background runner:

```bash
./scripts/pm2ctl.sh status
./scripts/pm2ctl.sh logs predict-loop --lines 100
```

## Project History

Earlier Claude multi-agent and prompt-evolution work is kept for reference, not current production use. See [docs/ITERATIONS.md](docs/ITERATIONS.md) for the version map and archived materials.

## Data and Output

- `data/predictions.db`: production markets, predictions, scores
- `data/v3_research.db`: challenger promotion runs
- `docs/index.html`: generated dashboard
- `docs/research/`: latest and historical promotion reports
