# Polymarket BTC 5-Min Candle — Program Rules

## Objective
Run a low-noise production baseline on Polymarket BTC 5-minute markets, then challenge it with sample-out research before promoting any new model or rule.

## Current System

```text
fetch_markets.py   -> fetch active BTC 5-min markets
predict.py         -> production baseline signal and trade/no-trade decision
score.py           -> resolve outcomes, update P&L, refresh scorecards
dashboard.py       -> build local and static dashboard output
v3/promotion.py    -> research-only promotion gate for challengers
notifier.py        -> Telegram alerts for baseline trades and DeepSeek promotion passes
```

## Production Rules
- Market scope: Polymarket BTC 5-minute up/down markets only.
- Source: Gamma API for markets plus BTC candle context from exchange data helpers.
- Production strategy: deterministic baseline with regime filtering and conviction gating.
- Trade policy: default to `NO_TRADE` unless edge and regime filters both clear.
- Notification policy: send Telegram only for real baseline trades and successful DeepSeek promotion passes.

## Research Rules
- AI does not sit in the production decision path.
- `deepseek_v3` is a research-only challenger.
- Every challenger must run through blocked time-series folds in `src/v3/promotion.py`.
- Promotion requires passing explicit gates on ROI delta, win rate, trade count, drawdown, and fold consistency.
- Failed challengers remain in research; they do not alter production behavior.

## Metrics That Matter
- Production priority: trade ROI, trade win rate, drawdown, and coverage.
- Research priority: sample-out improvement versus `production_baseline`.
- Calibration and Brier score remain supporting diagnostics, not the primary promotion target.

## Deployment
- GitHub Actions: test, run a production cycle, and publish generated artifacts.
- PM2 local mode: `predict-loop`, `dashboard`, and `research-loop`.
- Dashboard output: `docs/index.html`
- Research reports: `docs/research/latest.md`

## Versioning
Legacy prompt-evolution and Claude multi-agent work is retained as historical material only. Use `docs/ITERATIONS.md` to locate older designs and archived findings.
