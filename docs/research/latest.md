# Research Promotion Report

- Run ID: `451b2b4d3f47`
- Generated: `2026-04-05T12:08:42.842755+00:00`
- Baseline: `production_baseline`
- Challenger: `deepseek_v3`
- Dataset days: `3`
- Warm-up markets: `80`
- Folds: `2`
- Max eval contexts per fold: `12`
- Promotion gate: `FAIL`

## Summary

| Metric | Baseline | Challenger | Delta |
| --- | ---: | ---: | ---: |
| Eval markets | 24 | 24 | 0 |
| Trades | 0 | 0 | 0 |
| Directional accuracy | 0.0% | 62.5% | +62.50pp |
| Avg Brier | 0.2526 | 0.2421 | -0.0106 |
| Win rate | 0.0% | 0.0% | +0.00pp |
| ROI | +0.00% | +0.00% | +0.00pp |
| P&L | $+0.00 | $+0.00 | $+0.00 |
| Max drawdown | $+0.00 | $+0.00 | $+0.00 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `2/2`
- Aggregate ROI delta: `+0.00pp`
- Aggregate win-rate delta: `+0.00pp`
- Trade ratio: `1.00`
- Drawdown ratio: `0.00`

### Why It Failed

- aggregate ROI delta +0.00pp < required 5.00pp

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +0.00pp | +0.00pp | 1.00 | yes |
| 1 | PASS | +0.00pp | +0.00pp | 1.00 | yes |

## Regime Breakdown

| Regime | Baseline Trades | Challenger Trades | ROI Delta | WR Delta | P&L Delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| LOW_VOL / MEAN_REVERTING | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| LOW_VOL / NEUTRAL | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| MEDIUM_VOL / MEAN_REVERTING | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: aggregate ROI delta +0.00pp < required 5.00pp
