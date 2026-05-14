# Research Promotion Report

- Run ID: `70d5ead9a5ad`
- Generated: `2026-05-14T08:45:04.900448+00:00`
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
| Trades | 1 | 0 | -1 |
| Directional accuracy | 60.0% | 0.0% | -60.00pp |
| Avg Brier | 0.2495 | 0.2500 | +0.0005 |
| Win rate | 100.0% | 0.0% | -100.00pp |
| ROI | +42.55% | +0.00% | -42.55pp |
| P&L | $+31.91 | $+0.00 | $-31.91 |
| Max drawdown | $+0.00 | $+0.00 | $+0.00 |

## Gate Decision

- Result: `FAIL`
- Passing folds: `1/2`
- Aggregate ROI delta: `-42.55pp`
- Aggregate win-rate delta: `-100.00pp`
- Trade ratio: `0.00`
- Drawdown ratio: `0.00`

### Why It Failed

- aggregate ROI delta -42.55pp < required 5.00pp
- aggregate win-rate delta -100.00pp < required 0.00pp
- trade ratio 0.00 < required 0.60
- passing folds 1/2 < required 2

## Fold Checks

| Fold | Result | ROI Delta | WR Delta | Trade Ratio | Drawdown OK |
| --- | --- | ---: | ---: | ---: | --- |
| 0 | PASS | +0.00pp | +0.00pp | 1.00 | yes |
| 1 | FAIL | -42.55pp | -100.00pp | 0.00 | yes |

## Regime Breakdown

| Regime | Baseline Trades | Challenger Trades | ROI Delta | WR Delta | P&L Delta |
| --- | ---: | ---: | ---: | ---: | ---: |
| HIGH_VOL / NEUTRAL | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| HIGH_VOL / TRENDING | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| MEDIUM_VOL / NEUTRAL | 0 | 0 | +0.00pp | +0.00pp | $+0.00 |
| MEDIUM_VOL / TRENDING | 1 | 0 | -42.55pp | -100.00pp | $-31.91 |

## Recommendation

- Keep production pinned to `production_baseline`.
- Primary blocker: aggregate ROI delta -42.55pp < required 5.00pp
