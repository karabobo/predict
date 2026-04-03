# Research Promotion Report

- Run ID: `652fe66bea92`
- Generated: `2026-04-03T02:01:44.892819+00:00`
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
| Trades | 0 | 8 | 8 |
| Directional accuracy | 0.0% | 54.2% | +54.17pp |
| Avg Brier | 0.2556 | 0.2601 | +0.0045 |
| Win rate | 0.0% | 50.0% | +50.00pp |
| ROI | +0.00% | +34.60% | +34.60pp |
| P&L | $+0.00 | $+207.61 | $+207.61 |
| Max drawdown | $+0.00 | $+228.38 | $+228.38 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `+34.60pp`
- Aggregate win-rate delta: `+50.00pp`
- Trade ratio: `1.00`
- Drawdown ratio: `inf`

### Why It Failed

- drawdown ratio inf > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +123.61pp | +100.00pp | inf | yes |
| 1 | FAIL | +21.89pp | +42.86pp | inf | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: drawdown ratio inf > allowed 1.00
