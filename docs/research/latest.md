# Research Promotion Report

- Run ID: `a9a21031494e`
- Generated: `2026-05-12T18:22:20.595613+00:00`
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
| Directional accuracy | 0.0% | 0.0% | +0.00pp |
| Avg Brier | 0.2500 | 0.2500 | +0.0000 |
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
| LOW_VOL / NEUTRAL | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| LOW_VOL / TRENDING | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| MEDIUM_VOL / MEAN_REVERTING | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| MEDIUM_VOL / NEUTRAL | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: aggregate ROI delta +0.00pp < required 5.00pp
