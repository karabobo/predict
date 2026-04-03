# Research Promotion Report

- Run ID: `e660fcd3b0be`
- Generated: `2026-04-02T21:54:05.720234+00:00`
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
| Directional accuracy | 50.0% | 54.2% | +4.17pp |
| Avg Brier | 0.2512 | 0.2385 | -0.0127 |
| Win rate | 0.0% | 71.4% | +71.43pp |
| ROI | -101.50% | +11.00% | +112.50pp |
| P&L | $-76.12 | $+57.77 | $+133.89 |
| Max drawdown | $+76.12 | $+76.12 | $+0.00 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `+112.50pp`
- Aggregate win-rate delta: `+71.43pp`
- Trade ratio: `7.00`
- Drawdown ratio: `1.00`

### Why It Failed

- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +73.05pp | +50.00pp | 2.00 | yes |
| 1 | FAIL | +26.78pp | +80.00pp | inf | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: passing folds 1/2 < required 2
