# Research Promotion Report

- Run ID: `bcd2e2ba7d5c`
- Generated: `2026-04-02T19:50:43.106296+00:00`
- Baseline: `production_baseline`
- Challenger: `deepseek_v3`
- Dataset days: `3`
- Warm-up markets: `80`
- Folds: `2`
- Max eval contexts per fold: `12`
- Promotion gate: `PASS`

## Summary

| Metric | Baseline | Challenger | Delta |
| --- | ---: | ---: | ---: |
| Eval markets | 24 | 24 | 0 |
| Trades | 1 | 2 | 1 |
| Directional accuracy | 0.0% | 58.3% | +58.33pp |
| Avg Brier | 0.2612 | 0.2388 | -0.0224 |
| Win rate | 0.0% | 50.0% | +50.00pp |
| ROI | -101.50% | -5.23% | +96.27pp |
| P&L | $-76.12 | $-7.84 | $+68.29 |
| Max drawdown | $+76.12 | $+76.12 | $+0.00 |

## Gate Decision

- Result: `PASS`
- Passing folds: `2/2`
- Aggregate ROI delta: `+96.27pp`
- Aggregate win-rate delta: `+50.00pp`
- Trade ratio: `2.00`
- Drawdown ratio: `1.00`

### Why It Passed

- Challenger cleared every aggregate gate and fold check.

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +0.00pp | +0.00pp | 1.00 | yes |
| 1 | PASS | +96.27pp | +50.00pp | 2.00 | yes |

## Recommendation

- Promote `deepseek_v3` for the next production challenge round.
