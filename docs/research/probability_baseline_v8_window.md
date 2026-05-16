# Probability Baseline v8 Window Upgrade

- Date: 2026-05-16
- Dataset: BTC 5m historical markets from `data/polymarket_backtest.db`
- Eligible markets: 21,612
- Rolling validation: warm-up 500, 6 blocked folds, lookback 20
- Entry-price assumption for PnL comparison: neutral 0.50

## Winner

`paper_logreg_5m_window` replaces the prior default `ensemble_logreg_raw_xgb`.

The old raw model learned from absolute BTC price and absolute volume. The new
window model uses normalized 5m structure: returns, range position, realized
volatility, streaks, wick ratios, and relative volume.

## Arena Results

| Rank | Contender | Direction WR | Brier | Best edge | Best trades | Best WR | Best ROI | Best PnL |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `paper_logreg_5m_window` | 51.51% | 0.250285 | 0.0050 | 19,058 | 51.76% | +3.53% | +50,400 |
| 2 | `ensemble_logreg_raw_window` | 51.42% | 0.250011 | 0.0000 | 21,112 | 51.42% | +2.84% | +45,000 |
| 3 | `paper_logreg_5m_raw` | 50.93% | 0.250605 | 0.0100 | 17,494 | 51.19% | +2.38% | +31,200 |
| 4 | `ensemble_logreg_raw_xgb` | 51.00% | 0.250243 | 0.0050 | 17,834 | 51.42% | +2.85% | +38,100 |
| 5 | `paper_xgb_5m_window` | 49.97% | 0.250480 | 0.0300 | 4,638 | 51.36% | +2.72% | +9,450 |

## Selected Threshold Curve

`paper_logreg_5m_window`:

| Min probability edge | Trades | Coverage | WR | ROI | PnL |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0.0000 | 21,112 | 100.00% | 51.51% | +3.01% | +47,700 |
| 0.0050 | 19,058 | 90.27% | 51.76% | +3.53% | +50,400 |
| 0.0100 | 16,990 | 80.48% | 51.89% | +3.78% | +48,150 |
| 0.0200 | 13,188 | 62.47% | 52.30% | +4.60% | +45,450 |
| 0.0300 | 9,887 | 46.83% | 52.80% | +5.59% | +41,475 |
| 0.0500 | 4,607 | 21.82% | 52.75% | +5.49% | +18,975 |

## Implementation

- Default `BaselineProbabilityEnsemble` is now `ensemble_logreg_window`.
- Live artifact path is now `data/models/baseline_prior.pkl`.
- Legacy `ensemble_logreg_raw_xgb` remains available for fallback comparison.
- Realtime daemon was restarted and reports `prior=1`.
