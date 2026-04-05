# Polymarket BTC 5-Min Prediction Bot

This repository runs a deterministic production strategy for Polymarket BTC 5-minute markets and a separate research layer for challenger evaluation and coach audits. Production is rule-based; AI is restricted to research-only challengers and coaches such as `deepseek_v3`.

Execution development is now being split out into a separate NautilusTrader project at `/root/program/predict-nautilus`. This repository remains the source for baseline research, promotion gating, reporting, and operational review.

## Current Architecture

```text
src/fetch_markets.py   -> active BTC 5-min markets from Gamma API
src/predict.py         -> production baseline signal + regime filter + optional Telegram alert
src/score.py           -> resolve markets, score calls, compute P&L
src/dashboard.py       -> static dashboard and local web view
src/v3/promotion.py    -> sample-out baseline vs challenger evaluation
src/v3/coaches.py      -> parallel skip/toxicity coach audits + rule-candidate tags
src/notifier.py        -> Telegram notifications
```

The repo now has three distinct concerns:
- Production: `contrarian_rule` baseline, scoring, dashboard, optional live trading.
- Research: blocked time-series promotion runs plus coach audits stored in `data/v3_research.db`.
- Reporting: `docs/index.html` plus `docs/research/latest.md`.

The new split is:
- `predict`: research/dev, baseline evolution, promotion harness, dashboard, Telegram visibility
- `predict-nautilus`: NautilusTrader execution project for Polymarket maker-style order management

## Local Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional environment:
- `SILICON_FLOW_KEY` for `deepseek_v3` challenger and coach runs
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for notifications

## Common Commands

```bash
python -m pytest tests/ -v
cd src && python ci_run.py
cd src && python dashboard.py
PYTHONPATH=. python src/v3/promotion.py --challenger deepseek_v3 --days 3 --warm-up 80 --folds 2
PYTHONPATH=. python src/v3/coach_cycle.py
PYTHONPATH=. python src/v3/build_polymarket_dataset.py --input /root/Data/hf_polymarket_btc_5m_markets.parquet --output data/polymarket_backtest.db
PYTHONPATH=. python src/v3/sync_btc5m_history.py --source-file /root/Data/hf_markets.parquet --copy-source-local
PYTHONPATH=. python src/v3/backtest_rules.py --db data/polymarket_backtest.db --btc-candles /root/Data/btc_5m.parquet --rule baseline_current --entry-price-source neutral_50
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
- `data/v3_research.db`: challenger promotion runs and coach audits
- `data/polymarket_backtest.db`: local historical BTC 5-minute market dataset for rule backtests
- `data/external/hf_markets.parquet`: downloaded full `markets.parquet` source from `SII-WANGZJ/Polymarket_data`
- `data/external/hf_polymarket_btc_5m_markets.parquet`: merged BTC 5-minute subset used to rebuild the local backtest DB
- `docs/index.html`: generated dashboard
- `docs/research/`: latest and historical promotion reports

## Historical BTC 5m Sync

The repo can now pull and refresh BTC 5-minute market history from the `SII-WANGZJ/Polymarket_data` dataset referenced at:

- `https://github.com/SII-WANGZJ/Polymarket_data`
- `https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data`

Recommended flow:

```bash
PYTHONPATH=. python src/v3/sync_btc5m_history.py
PYTHONPATH=. python src/v3/audit_backtest_coverage.py --db data/polymarket_backtest.db
```

If you already have a local `markets.parquet`, use:

```bash
PYTHONPATH=. python src/v3/sync_btc5m_history.py \
  --source-file /root/Data/hf_markets.parquet \
  --copy-source-local
```

This sync path downloads or reads the source `markets.parquet`, filters `btc-updown-5m-*`, merges it with the existing local subset, and rebuilds `data/polymarket_backtest.db`.
