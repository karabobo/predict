# Polymarket Bot — Roadmap

## Status Key
- **DONE** — Completed and deployed
- **ACTIVE** — Currently in progress
- **NEXT** — Approved, ready to build
- **DEFERRED** — Documented, not started
- **FAILED** — Attempted, did not pass gate

---

## Part 1: Infrastructure (DONE)

Core pipeline running autonomously on GitHub Actions, dashboard on GitHub Pages.

- Polymarket Gamma API integration for BTC 5-min markets
- Auto-resolution and Brier scoring
- CI/CD: predict every 5 min, deploy dashboard
- Analytics dashboard with P&L simulation, streaks, calibration
- BTC candle data: Kraken primary, Coinbase fallback (replaced Binance)

---

## Part 2: Backtesting System (DONE)

Two backtesting engines built and validated.

### V1/V2 Backtest (`src/backtest.py`)
- Replay historical candles through LLM agent pipeline
- Synthetic market construction, no look-ahead bias
- Cost: ~$10 per 200-market run

### V3 Backtest (`src/v3/backtest.py`)
- Walk-forward with expanding window
- 14 days of Coinbase historical data (4,012 markets)
- Realistic friction: 1.5% round-trip + random slippage
- Regime-stratified reporting
- Cost: $0 (pure computation)

---

## Part 3: LLM Agent Ensemble (DONE -> SUPERSEDED)

Three iterations of Claude-powered prediction agents.

| Version | Win Rate | ROI | Cost/day | Verdict |
|---------|----------|-----|----------|---------|
| V1 (3 agents) | 50.8% | -13% | ~$1.50 | Lost money |
| V2 (3 agents + conviction) | 55.2% | +19% | ~$1.50 | Conviction system worked |
| V2.1 (2 agents, no LOW bets) | 59.7% | +53% on MEDIUM | ~$1.50 | Best LLM version |

**Key finding:** Conviction-based bet sizing — not the agents — drove profitability.
The LLM agents are expensive ($1.50/day) and add marginal signal over simple rules.

See `docs/BACKTEST_FINDINGS.md` for full analysis.

---

## Part 4: ML Model Attempt (FAILED)

V3 XGBoost + Logistic Regression with 32 features.

- **Result:** 51.3% WR, +0.5% ROI — failed to beat contrarian rule baseline
- **Decision gate:** Required +3pp WR or +5pp ROI over baseline. Did not pass.
- **Calibration:** Failed on 6/8 bins. Kelly sizing would be dangerous.
- **Root cause:** Too many features (32) for too few samples (500). 5-min BTC is too noisy for ML to find patterns beyond simple exhaustion rules.

See `docs/BACKTEST_FINDINGS.md` and `src/v3/model.py` for details.

---

## Part 4.5: Sample-Out Promotion Harness (ACTIVE)

Research is now being rewired around the current production baseline instead of the legacy contrarian-only backtest.

- `src/v3/arena.py` provides the shared baseline/challenger evaluation layer
- `src/v3/promotion.py` is the canonical research entrypoint
- blocked time-series folds are used for sample-out evaluation
- every run is stored in `data/v3_research.db`
- challengers must pass explicit promotion gates before they can be considered for production

Example:

```bash
PYTHONPATH=. python src/v3/promotion.py --challenger legacy_regime_filtered
```

Current contenders:
- `production_baseline`
- `legacy_contrarian`
- `legacy_regime_filtered`
- `legacy_enhanced`
- `v3_ml`
- `deepseek_v3`

---

## Part 5: Production Baseline Split (DONE)

Production was split away from legacy LLM agents and moved to a deterministic baseline plus research-only challengers.

- `predict.py` now runs the production baseline directly
- regime labels are stored with predictions
- dashboard and scorecards separate production from historical or research agents
- AI challengers are evaluated only in `src/v3/promotion.py`

The live system is now in the validation phase: keep collecting resolved production trades while challengers attempt promotion sample-out.

---

## Part 6: Live Paper Trading (DEFERRED)

> Blocked until the production baseline remains positive over a larger live sample and the promotion harness produces a stable challenger or sizing upgrade.

### Prerequisites
- Sustained live production profitability
- Stable sample-out validation from research
- Polygon wallet with USDC
- `py-clob-client` SDK for CLOB order placement

### Plan
- `src/trade.py` — rule signal → order conversion
- Paper trading: log what we would have traded, track hypothetical P&L
- Regime filter active: skip mean-reverting markets
- Fixed $75 bet size (no Kelly until calibration proven)
- Daily loss limit: -$300 (4 consecutive losses → stop for 1 hour)
- Run 500 paper trades before any real capital

### After paper trading validates
- Micro-live: $5-10 bets for 200 trades
- Scale: $25 → $50 → $75 based on continued performance
- Full legacy plan in `docs/archive/v2-deployment-plan.md`

---

## Part 7: Mac Mini Deployment (DEFERRED)

Move from GitHub Actions (unreliable cron, 1-30 min delays) to always-on Mac Mini.
Only worthwhile if Part 5/6 prove the edge is real.

- `scripts/mac-mini-loop.sh` — continuous loop with git push
- `scripts/com.polymarket.bot.plist` — launchd daemon
- Keep GitHub Pages dashboard (push HTML from Mini)
