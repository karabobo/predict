# Research Promotion Report

- Run ID: `21a8ec66baf9`
- Generated: `2026-04-02T04:09:48.147602+00:00`
- Baseline: `production_baseline`
- Challenger: `deepseek_v3`
- Dataset days: `1`
- Warm-up markets: `40`
- Folds: `1`
- Max eval contexts per fold: `2`
- Promotion gate: `PASS`

## Summary

| Metric | Baseline | Challenger | Delta |
| --- | ---: | ---: | ---: |
| Eval markets | 2 | 2 | 0 |
| Trades | 0 | 1 | 1 |
| Directional accuracy | 0.0% | 50.0% | +50.00pp |
| Avg Brier | 0.2500 | 0.2295 | -0.0205 |
| Win rate | 0.0% | 100.0% | +100.00pp |
| ROI | +0.00% | +184.13% | +184.13pp |
| P&L | $+0.00 | $+138.09 | $+138.09 |
| Max drawdown | $+0.00 | $+0.00 | $+0.00 |

## Gate Decision

- Result: `PASS`
- Passing folds: `1/1`
- Aggregate ROI delta: `+184.13pp`
- Aggregate win-rate delta: `+100.00pp`
- Trade ratio: `1.00`
- Drawdown ratio: `0.00`

### Why It Passed

- Challenger cleared every aggregate gate and fold check.

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +184.13pp | +100.00pp | inf | yes |

## Recommendation

- Promote `deepseek_v3` for the next production challenge round.
