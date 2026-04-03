# V2 Framework Redesign

## Why the current shape underperforms

The repository currently mixes four concerns in the same runtime path:

1. signal generation
2. trade selection
3. online execution
4. research experimentation

That creates noise. A strategy can look "smart" while only improving Brier, only improving paper P&L, or only changing how often it trades. The system needs a stable production baseline and a separate research loop.

## Target architecture

Use three layers:

### 1. Production layer
- Fetch BTC candles and Polymarket markets
- Run exactly one approved baseline strategy
- Apply regime filter and no-trade gate
- Store both the raw signal and the final trade decision
- Execute paper/live orders only from approved decisions

### 2. Research layer
- Walk-forward backtests
- Ablation studies
- Challenger strategy evaluation
- Probability calibration and regime breakdown

### 3. Governance layer
- Promotion rule: no strategy reaches production unless it beats the baseline on sample-out periods after fees
- Rollback rule: if live paper results drift below threshold, revert to baseline only

## Recommended code layout

```text
src/
  strategies/
    types.py          # strategy input/output contract
    regime.py         # shared market-state classifier
    momentum.py       # current deterministic baseline
  predict.py          # orchestration only
  ci_run.py           # pipeline entrypoint
  live_trading.py     # execution only
  v3/                 # research and backtesting only
```

The rule is simple: production code does not contain experimental branching, and research code does not place orders.

## How AI should be used

AI should be moved out of the per-market critical path.

Use AI for:
- offline hypothesis generation
- post-trade clustering and error review
- candidate feature proposals
- regime annotation experiments
- writing challenger rules that are later converted into deterministic code

Do not use AI for:
- final trade decision on 5-minute BTC markets
- prompt self-evolution in production
- online model switching
- probability estimates that cannot be calibrated or explained

The current baseline should remain deterministic until an AI challenger proves sample-out improvement versus the same fee/slippage model.

## What to optimize

Optimize in this order:

1. `selection`: improve when to skip
2. `regime filter`: remove toxic market states
3. `sizing`: map conviction to risk cleanly
4. `signal`: only after the first three are stable

Primary promotion metrics:
- net ROI after fees and slippage
- max drawdown
- Sharpe or similar risk-adjusted return
- trade count stability

Secondary metrics:
- Brier score
- calibration gap
- win rate

## How to reduce noise

Reduce noise by default:
- keep one production baseline
- cap features to a small, interpretable set
- separate raw signal from trade/no-trade decision
- skip mean-reverting regimes unless a strategy is built for them
- do not optimize on a single metric
- require challenger wins across multiple time slices, not one backtest
- avoid online retraining on tiny datasets

For this repository, the lowest-noise baseline is:
- streak-based momentum
- exhaustion confirmation
- regime filter
- conviction threshold before any trade

## Migration plan

### Phase 1
- isolate strategy contract and baseline logic
- make `predict.py` orchestration-only
- keep AI clients available but unused in production

### Phase 2
- store `should_trade`, regime, and conviction explicitly
- split scoreboards into signal metrics and trade metrics

### Phase 3
- add a challenger harness in `src/v3/`
- evaluate any AI model only as a challenger against the deterministic baseline

### Phase 4
- promote only if live paper trading confirms the edge

This gives the project a clear answer to "how should AI be used": as a research accelerator and challenger generator, not as the production trader.

## Research workflow

The canonical research entrypoint is now:

```bash
PYTHONPATH=. python src/v3/promotion.py --challenger legacy_regime_filtered
```

What it does:
- downloads historical BTC candles
- rebuilds synthetic markets
- evaluates the current production baseline (`production_baseline`)
- evaluates one challenger on the same blocked time-series folds
- applies an explicit promotion gate
- stores the run in `data/v3_research.db`

Built-in contenders:
- `production_baseline` — current live baseline from `src/strategies/`
- `legacy_contrarian`
- `legacy_regime_filtered`
- `legacy_enhanced`
- `v3_ml`
- `deepseek_v3` — SiliconFlow-hosted DeepSeek-V3, research-only

Default promotion gate:
- aggregate ROI delta >= `+5pp`
- aggregate win-rate delta >= `0pp`
- challenger trades >= `60%` of baseline volume
- max drawdown not worse than baseline
- at least `60%` of folds pass

This is the line between research and production: no challenger gets promoted because of one backtest, one metric, or one attractive chart.
