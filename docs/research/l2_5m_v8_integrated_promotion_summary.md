# 5m L2 v8 Integrated Candidate Promotion Summary

Window: `2026-02-13 17:00:00 UTC` to `2026-03-19 04:00:00 UTC`.

Data:
- BTC 5m Polymarket L2: `https://s.wangshuox.com/poly_l2/btc-updown-5m-{start_ts}.parquet`
- BTC 5m snapshot: `https://s.wangshuox.com/poly_snapshot/btc-updown-5m-{start_ts}.parquet`
- Manifest availability: 660 usable rows out of 665; one market window had a missing snapshot parquet.

## Decision

Promote the integrated rule set to the `v8_integrated_candidate` profile for paper/shadow validation:

1. `router_overlay_ensemble`
2. `lvn_volume_scout`
3. `momentum_shape_ensemble`
4. `router_core`
5. `reversal_shape_ensemble`

Do not replace `production_current` yet. This profile should be used by paper trading and TUI first.
`v6_integrated_candidate` remains as a compatibility alias for older scripts and reports.

## Runtime Defaults

- Market: BTC 5m
- L2 slug timestamp mode: `start`
- Decision offset: `5s`
- Prior: `ensemble_logreg_raw_xgb`
- Prior minimum edge: `0.01`
- Fallback offset: `15s`
- Avoid using 30s as the primary entry point; several candidates show materially worse average edge by then.

## Key L2 Results

Raw 5s replay:

| Rule | Trades | Win Rate | ROI | Avg Edge |
|---|---:|---:|---:|---:|
| `router_overlay_ensemble` | 149 | 61.07% | +20.97% | +0.0641 |
| `lvn_volume_scout` | 149 | 59.73% | +19.73% | +0.0737 |
| `reversal_shape_ensemble` | 82 | 60.98% | +15.45% | +0.0547 |
| `router_core` | 131 | 58.02% | +12.25% | +0.0526 |
| `momentum_shape_ensemble` | 149 | 55.70% | +11.83% | +0.0776 |

Prior-gated 5s replay with `ensemble_logreg_raw_xgb`, edge `0.01`:

| Rule | Signals | Win Rate | ROI | Avg Edge |
|---|---:|---:|---:|---:|
| `router_overlay_ensemble` | 61 | 67.21% | +27.73% | +0.0492 |
| `lvn_volume_scout` | 68 | 63.24% | +22.87% | +0.0674 |
| `momentum_shape_ensemble` | 60 | 63.33% | +22.12% | +0.0709 |
| `router_core` | 66 | 63.64% | +20.64% | +0.0454 |
| `reversal_shape_ensemble` | 58 | 63.79% | +17.84% | +0.0447 |

## Interpretation

The 5m L2 data is usable. The previous 404s came from using the wrong date range and from treating the 5m slug timestamp as an end timestamp.

The edge decays quickly with time. In both raw and prior-gated replay, 5s is consistently stronger than 15s and 30s. This supports a WSS/order-book-first runtime rather than minute-level polling.

The prior should be used as a gate for the initial paper profile, but it should stay configurable by rule. At 30s it raises win rate but can produce non-positive average edge after fill for several rules, which means late execution can erase the model edge.

## Source Reports

- `docs/research/l2_5m_integrated_feb13_mar19_offsets_raw.md`
- `docs/research/l2_5m_integrated_feb13_mar19_offsets_prior001.md`
- `docs/research/l2_5m_integrated_feb13_mar19_raw.md`
- `docs/research/l2_5m_integrated_feb13_mar19_prior001.md`
