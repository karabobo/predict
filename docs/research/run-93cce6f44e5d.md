# Research Promotion Report

- Run ID: `93cce6f44e5d`
- Generated: `2026-04-03T00:59:35.390130+00:00`
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
| Trades | 0 | 11 | 11 |
| Directional accuracy | 0.0% | 62.5% | +62.50pp |
| Avg Brier | 0.2500 | 0.2434 | -0.0066 |
| Win rate | 0.0% | 63.6% | +63.64pp |
| ROI | +0.00% | +46.38% | +46.38pp |
| P&L | $+0.00 | $+382.63 | $+382.63 |
| Max drawdown | $+0.00 | $+152.25 | $+152.25 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `0/2`
- Aggregate ROI delta: `+46.38pp`
- Aggregate win-rate delta: `+63.64pp`
- Trade ratio: `1.00`
- Drawdown ratio: `inf`

### Why It Failed

- drawdown ratio inf > allowed 1.00
- passing folds 0/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | FAIL | +44.05pp | +57.14pp | inf | no |
| 1 | FAIL | +50.45pp | +75.00pp | inf | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: drawdown ratio inf > allowed 1.00
