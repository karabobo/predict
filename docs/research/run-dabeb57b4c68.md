# Research Promotion Report

- Run ID: `dabeb57b4c68`
- Generated: `2026-04-02T05:10:38.741584+00:00`
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
| Trades | 0 | 2 | 2 |
| Directional accuracy | 100.0% | 54.2% | -45.83pp |
| Avg Brier | 0.2368 | 0.2442 | +0.0074 |
| Win rate | 0.0% | 100.0% | +100.00pp |
| ROI | +0.00% | +91.07% | +91.07pp |
| P&L | $+0.00 | $+136.61 | $+136.61 |
| Max drawdown | $+0.00 | $+0.00 | $+0.00 |

## Gate Decision

- Result: `PASS`
- Passing folds: `2/2`
- Aggregate ROI delta: `+91.07pp`
- Aggregate win-rate delta: `+100.00pp`
- Trade ratio: `1.00`
- Drawdown ratio: `0.00`

### Why It Passed

- Challenger cleared every aggregate gate and fold check.

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +91.07pp | +100.00pp | inf | yes |
| 1 | PASS | +0.00pp | +0.00pp | 1.00 | yes |

## Recommendation

- Promote `deepseek_v3` for the next production challenge round.
