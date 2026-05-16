# Probability Cross Source Validation

| Source | Eligible | Winner | Contender | Direction WR | Brier | Best edge | Best trades | Best WR | Best ROI | Best PnL | Edge 0 WR | Edge 0 PnL | Edge .005 WR | Edge .005 PnL |
| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| local | 21612 | `paper_logreg_5m_window` | `paper_logreg_5m_window` | 51.51% | 0.250285 | 0.0050 | 19058 | 51.76% | +3.53% | +50400.00 | 51.51% | +47700.00 | 51.76% | +50400.00 |
| local | 21612 | `paper_logreg_5m_window` | `paper_logreg_5m_raw` | 50.93% | 0.250605 | 0.0100 | 17494 | 51.19% | +2.38% | +31200.00 | 50.93% | +29550.00 | 50.93% | +27075.00 |
| local | 21612 | `paper_logreg_5m_window` | `ensemble_logreg_raw_xgb` | 51.00% | 0.250243 | 0.0050 | 17834 | 51.42% | +2.85% | +38100.00 | 51.00% | +31800.00 | 51.42% | +38100.00 |
| coinbase | 21612 | `paper_logreg_5m_window` | `paper_logreg_5m_window` | 51.51% | 0.250285 | 0.0050 | 19058 | 51.76% | +3.53% | +50400.00 | 51.51% | +47700.00 | 51.76% | +50400.00 |
| coinbase | 21612 | `paper_logreg_5m_window` | `paper_logreg_5m_raw` | 50.93% | 0.250605 | 0.0100 | 17494 | 51.19% | +2.38% | +31200.00 | 50.93% | +29550.00 | 50.93% | +27075.00 |
| coinbase | 21612 | `paper_logreg_5m_window` | `ensemble_logreg_raw_xgb` | 51.00% | 0.250243 | 0.0050 | 17834 | 51.42% | +2.85% | +38100.00 | 51.00% | +31800.00 | 51.42% | +38100.00 |
| binance | 21613 | `paper_logreg_5m_window` | `paper_logreg_5m_window` | 51.60% | 0.250350 | 0.0050 | 19045 | 51.80% | +3.60% | +51375.00 | 51.60% | +50775.00 | 51.80% | +51375.00 |
| binance | 21613 | `paper_logreg_5m_window` | `paper_logreg_5m_raw` | 50.94% | 0.251661 | 0.0000 | 21113 | 50.94% | +1.87% | +29625.00 | 50.94% | +29625.00 | 50.97% | +26925.00 |
| binance | 21613 | `paper_logreg_5m_window` | `ensemble_logreg_raw_xgb` | 50.22% | 0.250676 | 0.0200 | 9033 | 50.92% | +1.85% | +12525.00 | 50.22% | +7125.00 | 50.18% | +4575.00 |
| binance_us | 21613 | `paper_logreg_5m_window` | `paper_logreg_5m_window` | 50.53% | 0.251706 | 0.0100 | 15147 | 50.79% | +1.58% | +17925.00 | 50.53% | +16875.00 | 50.61% | +16575.00 |
| binance_us | 21613 | `paper_logreg_5m_window` | `paper_logreg_5m_raw` | 50.56% | 0.250290 | 0.0050 | 17413 | 50.89% | +1.77% | +23175.00 | 50.56% | +17775.00 | 50.89% | +23175.00 |
| binance_us | 21613 | `paper_logreg_5m_window` | `ensemble_logreg_raw_xgb` | 50.67% | 0.250316 | 0.0000 | 21113 | 50.67% | +1.35% | +21375.00 | 50.67% | +21375.00 | 50.75% | +15000.00 |

## Source Diagnostics

| Pair | Overlap | Mean close diff | Max close diff | Exact close matches | Mean volume diff | Exact volume matches |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| local vs coinbase | 26,496 | 0.0000 | 0.0000 | 26,496 | 0.0000 | 26,496 |
| binance vs coinbase | 26,496 | 55.3128 | 300.9900 | 7 | 43.7555 | 0 |
| binance_us vs coinbase | 26,496 | 92.0837 | 1,258.4300 | 4 | 35.1293 | 0 |
| binance_us vs binance | 26,497 | 58.4540 | 1,149.0800 | 5 | 74.8347 | 0 |

## Interpretation

- `local` and `coinbase` are byte-equivalent in the overlapping OHLCV range, so they should be treated as one source, not independent evidence.
- `binance` is a genuine independent source and confirms the window model: `paper_logreg_5m_window` improves both direction WR and PnL versus raw and legacy raw+xgb.
- `binance_us` is a second independent source, but the advantage is weaker. Window remains positive and is still selected by the arena score, while raw and legacy are competitive on PnL/WR.
- This reduces the overfitting concern materially, but does not eliminate it. The robust claim is: the window model has a persistent small positive edge across Coinbase/local and Binance, and remains positive on Binance US. The stronger claim that it is dominant across every exchange source is not supported yet.

## Decision

Keep `paper_logreg_5m_window` as the default broad prior for now. It is still the best default because:

- It wins clearly on Coinbase/local and Binance.
- It stays positive on Binance US.
- It avoids absolute price/volume leakage better than raw.
- It is broad enough for high sample coverage rather than being a narrow filter.

The next validation should use live paper fills and L2 entry prices, because this report compares probability quality with neutral entry pricing rather than actual market microstructure fills.
