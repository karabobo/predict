# Research Promotion Report

- Run ID: `3ae603ed4fa6`
- Generated: `2026-04-02T20:52:23.248595+00:00`
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
| Directional accuracy | 100.0% | 41.7% | -58.33pp |
| Avg Brier | 0.2456 | 0.2685 | +0.0229 |
| Win rate | 0.0% | 33.3% | +33.33pp |
| ROI | +0.00% | -36.98% | -36.98pp |
| P&L | $+0.00 | $-83.21 | $-83.21 |
| Max drawdown | $+0.00 | $+152.25 | $+152.25 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `-36.98pp`
- Aggregate win-rate delta: `+33.33pp`
- Trade ratio: `1.00`
- Drawdown ratio: `inf`

### Why It Failed

- aggregate ROI delta -36.98pp < required 5.00pp
- drawdown ratio inf > allowed 1.00
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +92.06pp | +100.00pp | inf | yes |
| 1 | FAIL | -101.50pp | +0.00pp | inf | no |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: aggregate ROI delta -36.98pp < required 5.00pp
