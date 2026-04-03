# Research Promotion Report

- Run ID: `e969d44a3a5c`
- Generated: `2026-04-03T06:11:14.221858+00:00`
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
| Trades | 0 | 3 | 3 |
| Directional accuracy | 100.0% | 60.9% | -39.13pp |
| Avg Brier | 0.2456 | 0.2238 | -0.0218 |
| Win rate | 0.0% | 66.7% | +66.67pp |
| ROI | +0.00% | +71.69% | +71.69pp |
| P&L | $+0.00 | $+161.31 | $+161.31 |
| Max drawdown | $+0.00 | $+76.12 | $+76.12 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `+71.69pp`
- Aggregate win-rate delta: `+66.67pp`
- Trade ratio: `1.00`
- Drawdown ratio: `inf`

### Why It Failed

- drawdown ratio inf > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | FAIL | +71.69pp | +66.67pp | inf | no |
| 1 | PASS | +0.00pp | +0.00pp | 1.00 | yes |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: drawdown ratio inf > allowed 1.00
