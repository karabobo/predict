# Research Promotion Report

- Run ID: `7ee9c3ed0df5`
- Generated: `2026-04-02T14:37:47.659440+00:00`
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
| Trades | 1 | 7 | 6 |
| Directional accuracy | 0.0% | 60.9% | +60.87pp |
| Avg Brier | 0.2556 | 0.2377 | -0.0179 |
| Win rate | 0.0% | 57.1% | +57.14pp |
| ROI | -101.50% | +96.35% | +197.85pp |
| P&L | $-76.12 | $+505.83 | $+581.95 |
| Max drawdown | $+76.12 | $+152.25 | $+76.12 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `+197.85pp`
- Aggregate win-rate delta: `+57.14pp`
- Trade ratio: `7.00`
- Drawdown ratio: `2.00`

### Why It Failed

- drawdown ratio 2.00 > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +93.47pp | +100.00pp | inf | yes |
| 1 | FAIL | +198.33pp | +50.00pp | 6.00 | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: drawdown ratio 2.00 > allowed 1.00
