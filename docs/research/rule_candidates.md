# Rule Candidate Report

- Generated: `2026-05-05T18:56:56.287679+00:00`

## Sample Scope

- Scope: `局部样本`
- Ended-market coverage: `2025-12-18T04:30:00+00:00` -> `2026-04-01T14:15:00+00:00`
- Markets in local study set: `25418` total, `21613` resolved
- Source files: `hf_polymarket_btc_5m_markets.parquet`

> These rule results come from the current local partial historical sample, not a full-history production-equivalent archive.

## Spotlight

- Candidate: `baseline_router_v2`
- Label: 大基线 Router V2
- Neutral-50 ROI: `+16.31%`
- Model-Edge-8 ROI: `+20.97%`
- Trades: `409`
- Win rate: `55.99%`

## Recent Windows

### Neutral-50

- Last 14d: `50` trades, WR `58.00%`, ROI `+25.11%`, P&L `+1475.00`
- Last 30d: `119` trades, WR `57.98%`, ROI `+19.93%`, P&L `+2800.00`

### Model-Edge-8

- Last 14d: `50` trades, WR `58.00%`, ROI `+27.19%`, P&L `+1597.34`
- Last 30d: `119` trades, WR `57.98%`, ROI `+21.43%`, P&L `+3010.48`

## Takeaways

- 下方所有规则候选结果都来自当前本地历史样本（2025-12-18 至 2026-04-01），属于局部样本研究，不代表完整历史上的生产证据。
- Router V2 目前是这份本地样本里最像“大基线”的候选：409 笔交易，中性入场 ROI +16.31%，保守入场 ROI +20.97%。
- 最近 14 天样本偏稀：50 笔，ROI +25.11%。
- 最近 30 天中性入场下共有 119 笔，ROI +19.93%。
- 相比 V3 / V4 这类稀疏高质量腿，Router V2 的样本宽度明显更大（409 笔），因此更适合作为当前的大基线研究方向。
- Router 能成立，是因为三条分支职责不对称：低波动负责提供宽度（251 笔，+12.26%），中波动补中等宽度（220 笔，+3.26%），高波动则保持少而精（78 笔，+30.46%）。
- 用平衡版中波动延续腿替换原始宽版后，中波动分支的中性入场 ROI 从 +3.26% 提升到 +10.89%，保守入场 ROI 也从 +6.24% 提升到 +13.94%。
- LVN alpha=3 更适合作为研究叠加因子，而不是新基线：虽然全样本中性入场 ROI 提升到 +11.63%（570 笔），但在保守入场下回落到 +14.53%，最近 30 天也更弱。
- V4 稀疏叠加更像质量兜底，而不是放大量能：全样本中性入场 ROI 为 +10.90%，最近 30 天表现基本没有被破坏。
- 稀疏组合没有形成干净叠加：中性入场 ROI +11.73% 只比单独 LVN 略高，但执行更保守时仍弱于基础 router。
- V4 仍然在质量上最强（25 笔，+52.00%），但 Router V2 因为能把宽度扩到 409 笔，更适合作为大基线候选。
- 两条最强反转腿职责不同：冲高回落做空负责提供宽度（58 笔，+17.24%），宽底反弹做多则提供最高的独立质量（43 笔，+34.26%）。
- Baseline V2 alpha>=2 能改善宽骨架的中性入场表现（+7.10%），但一旦按更保守的入场口径处理，就会掉到 -0.19%。
- Baseline V2 alpha=3 更像稀疏高质量叠加：中性入场（+23.81%）、保守入场（+9.40%）和轻保守入场（+3.89%）都保持为正。

## Baseline Router Family

| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 大基线 Router V1 | 549 | 54.64% | +10.79% | +14.88% | +19.83% | +15.20% |
| 大基线 Router V2 | 409 | 55.99% | +16.31% | +20.97% | +25.11% | +19.93% |
| 低波动分支 V1 | 251 | 54.58% | +12.26% | n/a | +12.20% | +34.55% |
| 中波动分支 V1 | 220 | 51.82% | +3.26% | +6.24% | +17.14% | +10.61% |
| 中波动分支 V3 | 80 | 53.75% | +10.89% | +13.94% | +22.31% | +15.31% |
| 高波动分支 V1 | 78 | 62.82% | +30.46% | n/a | +36.99% | +17.19% |

## Router Overlay Candidates

| Overlay | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Router + LVN alpha=3 叠加 | 570 | 54.91% | +11.63% | +14.53% | +17.80% | +15.65% |
| Router + V4 稀疏叠加 | 550 | 54.73% | +10.90% | +14.99% | +19.83% | +15.47% |
| Router + 稀疏组合叠加 | 571 | 54.99% | +11.73% | +14.63% | +17.80% | +15.91% |

## Baseline V3 vs V4 Reversal Family

| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| V3 反转核心 | 101 | 61.39% | +27.86% | +24.53% | +31.76% | +17.86% |
| V4 十窗口状态反转 | 25 | 76.00% | +52.00% | +50.72% | +20.00% | +44.44% |

## Baseline V4 Reversal Legs

| Leg | Trades | WR | Neutral ROI | Edge-8 ROI | Last 14d ROI | Last 30d ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| V4 子腿：冲高回落做空 | 58 | 58.62% | +17.24% | +14.47% | +7.69% | +16.67% |
| V4 子腿：宽底反弹做多 | 43 | 65.12% | +34.26% | +30.58% | +52.17% | +18.60% |

## Baseline V2 Research Skeletons

| Rule | Trades | WR | Neutral ROI | Edge-8 ROI | Edge-5 ROI | Last 14d ROI | Last 30d ROI |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| LVN Alpha≥2 骨架 | 182 | 51.65% | +7.10% | -0.19% | -5.48% | -73.91% | +11.11% |
| LVN Alpha=3 骨架 | 21 | 61.90% | +23.81% | +9.40% | +3.89% | -100.00% | +33.33% |

## Coach-Derived Rule Drafts

| Draft | Family | Scope | Trades | WR | Neutral ROI | Edge-8 ROI |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| Block baseline in observed regime [LOW_VOL / NEUTRAL] (`coach_spec__toxicity_coach__block_high_vol_neutral__low_vol_neutral`) | regime_block | LOW_VOL / NEUTRAL | 1162 | 48.11% | -4.68% | -5.87% |
| Loosen streak threshold [LOW_VOL / TRENDING] (`coach_spec__skip_coach__loosen_streak_threshold__low_vol_trending`) | threshold_loosen | LOW_VOL / TRENDING | 1321 | 47.69% | -4.75% | -5.86% |

## Latest Candidate Runs

| Rule | Entry | Trades | WR | P&L | ROI |
| --- | --- | ---: | ---: | ---: | ---: |
| 大基线 Router V1 | neutral_50 | 549 | 54.64% | +6575.00 | +10.79% |
| 大基线 Router V1 | model_edge_8 | 549 | 54.64% | +9068.43 | +14.88% |
| 大基线 Router V2 | neutral_50 | 409 | 55.99% | +6675.00 | +16.31% |
| 大基线 Router V2 | model_edge_8 | 409 | 55.99% | +8581.18 | +20.97% |
| Router + LVN alpha=3 叠加 | neutral_50 | 570 | 54.91% | +7575.00 | +11.63% |
| Router + LVN alpha=3 叠加 | model_edge_8 | 570 | 54.91% | +9463.34 | +14.53% |
| Router + V4 稀疏叠加 | neutral_50 | 550 | 54.73% | +6650.00 | +10.90% |
| Router + V4 稀疏叠加 | model_edge_8 | 550 | 54.73% | +9142.54 | +14.99% |
| Router + 稀疏组合叠加 | neutral_50 | 571 | 54.99% | +7650.00 | +11.73% |
| Router + 稀疏组合叠加 | model_edge_8 | 571 | 54.99% | +9537.44 | +14.63% |
| 低波动分支 V1 | neutral_50 | 251 | 54.58% | +2600.00 | +12.26% |
| 中波动分支 V1 | neutral_50 | 220 | 51.82% | +975.00 | +3.26% |
| 中波动分支 V1 | model_edge_8 | 220 | 51.82% | +1863.47 | +6.24% |
| 中波动分支 V3 | neutral_50 | 80 | 53.75% | +1075.00 | +10.89% |
| 中波动分支 V3 | model_edge_8 | 80 | 53.75% | +1376.21 | +13.94% |
| 高波动分支 V1 | neutral_50 | 78 | 62.82% | +3000.00 | +30.46% |
| LVN Alpha≥2 骨架 | neutral_50 | 182 | 51.65% | +1200.00 | +7.10% |
| LVN Alpha≥2 骨架 | model_edge_8 | 182 | 51.65% | -32.23 | -0.19% |
| LVN Alpha≥2 骨架 | model_edge_5 | 182 | 51.65% | -926.78 | -5.48% |
| LVN Alpha=3 骨架 | neutral_50 | 21 | 61.90% | +1000.00 | +23.81% |
| LVN Alpha=3 骨架 | model_edge_8 | 21 | 61.90% | +394.90 | +9.40% |
| LVN Alpha=3 骨架 | model_edge_5 | 21 | 61.90% | +163.44 | +3.89% |
| V3 反转核心 | neutral_50 | 101 | 61.39% | +3225.00 | +27.86% |
| V3 反转核心 | model_edge_8 | 101 | 61.39% | +2838.97 | +24.53% |
| V4 十窗口状态反转 | neutral_50 | 25 | 76.00% | +975.00 | +52.00% |
| V4 十窗口状态反转 | model_edge_8 | 25 | 76.00% | +950.99 | +50.72% |
| V4 子腿：冲高回落做空 | neutral_50 | 58 | 58.62% | +750.00 | +17.24% |
| V4 子腿：冲高回落做空 | model_edge_8 | 58 | 58.62% | +629.29 | +14.47% |
| V4 子腿：宽底反弹做多 | neutral_50 | 43 | 65.12% | +2475.00 | +34.26% |
| V4 子腿：宽底反弹做多 | model_edge_8 | 43 | 65.12% | +2209.68 | +30.58% |
| LVN 做多 + 放量 + streak≥4 | neutral_50 | 21 | 61.90% | +750.00 | +30.61% |
| LVN 做多 + 放量 + streak≥4 | model_edge_8 | 21 | 61.90% | +512.96 | +20.94% |
| LVN 纯放量做多 | neutral_50 | 37 | 56.76% | +875.00 | +24.82% |
| LVN 纯放量做多 | model_edge_8 | 37 | 56.76% | +549.07 | +15.58% |
| LVN 做多 | neutral_50 | 74 | 54.05% | +950.00 | +13.48% |
| LVN 做多 | model_edge_8 | 74 | 54.05% | +357.41 | +5.07% |
| LVN 做多 | model_edge_5 | 74 | 54.05% | -32.46 | -0.46% |
| 仅 LOW_VOL / NEUTRAL | neutral_50 | 126 | 48.41% | +75.00 | +0.63% |
| Block baseline in observed regime [LOW_VOL / NEUTRAL] | neutral_50 | 1162 | 48.11% | -6675.00 | -4.68% |
| Block baseline in observed regime [LOW_VOL / NEUTRAL] | model_edge_8 | 1162 | 48.11% | -8384.60 | -5.87% |
| Loosen streak threshold [LOW_VOL / TRENDING] | neutral_50 | 1321 | 47.69% | -7575.00 | -4.75% |
| Loosen streak threshold [LOW_VOL / TRENDING] | model_edge_8 | 1321 | 47.69% | -9345.45 | -5.86% |
