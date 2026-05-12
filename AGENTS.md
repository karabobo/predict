# Repository Guidelines

## Project Structure
Three concerns in one repo:
- **Production**: `src/predict.py` (baseline signal + regime filter), `src/score.py` (resolve + P&L), `src/fetch_markets.py`, `src/dashboard.py`, `src/notifier.py`
- **Research**: `src/v3/` — challenger evaluation (`promotion.py`), coach audits (`coaches.py`, `coach_cycle.py`), backtest harness (`backtest_rules.py`, `build_polymarket_dataset.py`)
- **Reporting**: `docs/index.html` (generated dashboard), `docs/research/latest.md` (promotion reports)

Execution lives in a separate project: `/root/program/predict-nautilus` (NautilusTrader). This repo is for baseline research, promotion gating, reporting, and ops review.

## Git Workflow — Critical
CI auto-commits to `data/` and `docs/` every ~5 minutes. Local state goes stale fast.

1. **Always `git pull` before reading `data/predictions.db` or any data file.** The dashboard (GitHub Pages) is the canonical view — if local numbers differ, your data is stale.
2. **Always push after making changes.** A change not on GitHub doesn't exist.
3. **Use `git pull --rebase` before pushing.** Expect CI conflicts on push; your code changes win, CI will regenerate the DB.
4. Commit prefix: `feat:` for manual work, `Auto:` for scheduled bot updates.

## Commands
Python 3.12. Venv is `.venv` (dot-prefixed).

```bash
# Setup
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt

# Full test suite — gates CI, run before every commit
python -m pytest tests/ -v

# Focused: trading math
python -m pytest tests/test_pnl.py tests/test_regression.py -v

# Single prediction cycle (one-shot, CI uses this)
cd src && python ci_run.py

# Local dashboard on http://localhost:5050
cd src && python dashboard.py
```

### Research & Backtest Commands (require `PYTHONPATH=.`)
```bash
PYTHONPATH=. python src/v3/promotion.py --challenger deepseek_v3 --days 3 --warm-up 80 --folds 2
PYTHONPATH=. python src/v3/coach_cycle.py
PYTHONPATH=. python src/v3/backtest_rules.py --db data/polymarket_backtest.db --btc-candles /root/Data/btc_5m.parquet --rule baseline_current
PYTHONPATH=. python src/v3/build_polymarket_dataset.py --input /root/Data/hf_polymarket_btc_5m_markets.parquet --output data/polymarket_backtest.db
PYTHONPATH=. python src/v3/sync_btc5m_history.py --source-file /root/Data/hf_markets.parquet
```

### PM2 Local Runner (requires `npm install pm2 --no-save`)
```bash
./scripts/pm2ctl.sh status
./scripts/pm2ctl.sh logs predict-loop --lines 100
```
7 managed loops: `realtime-loop`, `predict-loop`, `ops-loop`, `settlement-loop`, `dashboard`, `research-loop`, `coach-loop`. Logs in `logs/`. Timezone: `America/New_York`.

## Data Files
- `data/predictions.db` — production markets, predictions, scores (auto-committed by CI)
- `data/v3_research.db` — challenger promotion runs and coach audits
- `data/polymarket_backtest.db` — local historical BTC 5m dataset for rule backtests
- `docs/BREAK_FIX_LOG.md` — document production incidents here

## Bot Design Rules
- **No agent bias.** No built-in directional bias (UP or DOWN). All bias comes from human macro config.
- **Paper trade first.** Every new signal needs 200+ resolved predictions in paper trading before real capital.
- **Conviction gates real money.** Only `conviction >= 3` places bets. Conviction 0–2 = skip.
- **AI is research-only.** AI does not sit in the production decision path. `deepseek_v3` is a research challenger only.

## Coding Style
Python, 4-space indent, `snake_case`. Match surrounding style. Add new strategy/data logic under `src/`, not in scripts. Tests: small functions, explicit assertions, minimal fixtures. No formatter config — match the code.

## Testing
`pytest` gates CI. Add tests for every logic change. Add a regression test for every production bug. Name: `tests/test_<feature>.py`, cases `test_<expected_behavior>()`. Prioritize `test_regression.py` for incident prevention and `test_smoke.py` for import/wiring failures.
