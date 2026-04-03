# Research Promotion Report

- Run ID: `9233beb7f3b7`
- Generated: `2026-04-02T17:46:56.091277+00:00`
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
| Trades | 0 | 1 | 1 |
| Directional accuracy | 0.0% | 50.0% | +50.00pp |
| Avg Brier | 0.2500 | 0.2690 | +0.0190 |
| Win rate | 0.0% | 0.0% | +0.00pp |
| ROI | +0.00% | -101.50% | -101.50pp |
| P&L | $+0.00 | $-76.12 | $-76.12 |
| Max drawdown | $+0.00 | $+76.12 | $+76.12 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `-101.50pp`
- Aggregate win-rate delta: `+0.00pp`
- Trade ratio: `1.00`
- Drawdown ratio: `inf`

### Why It Failed

- aggregate ROI delta -101.50pp < required 5.00pp
- drawdown ratio inf > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +0.00pp | +0.00pp | 1.00 | yes |
| 1 | FAIL | -101.50pp | +0.00pp | inf | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: aggregate ROI delta -101.50pp < required 5.00pp
