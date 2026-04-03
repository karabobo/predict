# Research Promotion Report

- Run ID: `ad02ffc583b3`
- Generated: `2026-04-02T13:32:09.048564+00:00`
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
| Trades | 1 | 5 | 4 |
| Directional accuracy | 66.7% | 58.3% | -8.33pp |
| Avg Brier | 0.2468 | 0.2523 | +0.0055 |
| Win rate | 0.0% | 40.0% | +40.00pp |
| ROI | -101.50% | -43.67% | +57.83pp |
| P&L | $-76.12 | $-163.76 | $-87.63 |
| Max drawdown | $+76.12 | $+196.50 | $+120.37 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `+57.83pp`
- Aggregate win-rate delta: `+40.00pp`
- Trade ratio: `5.00`
- Drawdown ratio: `2.58`

### Why It Failed

- drawdown ratio 2.58 > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +0.00pp | +0.00pp | 1.00 | yes |
| 1 | FAIL | +57.83pp | +40.00pp | 5.00 | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: drawdown ratio 2.58 > allowed 1.00
