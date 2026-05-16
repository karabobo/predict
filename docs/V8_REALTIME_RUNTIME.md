# v8 Realtime Paper Runtime

The v8 runtime is the WSS/order-book-first paper path. The default paper profile
is `v8_broad_paper_candidate`, which wraps the L2-promoted v8 set with a wider
baseline/volume scout layer before prior and book-edge gates.

## Run

Smoke test without websocket connections:

```bash
scripts/v8_realtime_paper_loop.sh --once --no-wss --no-refresh
```

Realtime paper loop:

```bash
scripts/v8_realtime_paper_loop.sh
```

systemd user service, replacing PM2 for this runtime:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/predict-v8-realtime-paper.service ~/.config/systemd/user/
cp deploy/systemd/predict-settlement.service ~/.config/systemd/user/
cp deploy/systemd/predict-settlement.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now predict-v8-realtime-paper.service
systemctl --user enable --now predict-settlement.timer
```

Useful checks:

```bash
systemctl --user status predict-v8-realtime-paper.service
journalctl --user -u predict-v8-realtime-paper.service -f
systemctl --user list-timers predict-settlement.timer
```

Defaults:

- `PREDICT_RULE_PROFILE=v8_broad_paper_candidate`
- `POLYMARKET_PAPER_TRADING=1`
- `POLYMARKET_PAPER_MIN_EDGE=0.01`
- continuous active-market evaluation; no hard-coded 5s/15s delayed-entry gate
- `entry_offset_seconds` records the actual second in the 5m market for later attribution
- `POLYMARKET_PAPER_MIN_SECONDS_TO_EXPIRY=45` avoids late fills near settlement

## Data Flow

1. Refresh active BTC 5m Polymarket markets from Gamma.
2. Subscribe to Polymarket market WSS for current YES/NO token IDs.
3. Subscribe to Coinbase BTC ticker WSS.
4. Maintain an in-memory `LiveBookStore`.
5. During the active 5m market window, run v8 rules and optional prior gate.
6. Simulate a paper fill from the current WSS book and write `paper_orders`.
7. Settlement cycle updates ended markets, then settles `paper_orders` with win/loss and PnL.

## Notifications

Telegram is no longer the default notification path. Notification events are
written to SQLite in `notification_events` by default, which makes them stable
for the TUI/dashboard or any local poller.

Optional webhook fanout:

```bash
export NOTIFICATION_WEBHOOK_URL="https://example.com/predict-hook"
export WEBHOOK_NOTIFICATIONS_ENABLED=1
```

Telegram can still be enabled explicitly for compatibility:

```bash
export TELEGRAM_NOTIFICATIONS_ENABLED=1
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

## Notes

- `production_current` is unchanged.
- `v8_integrated_candidate` remains the narrower L2-promoted profile.
- `v8_broad_paper_candidate` adds `baseline_current`, `baseline_v2_lvn_alpha2`,
  `baseline_v6_broad_shape`, and `baseline_router_v2_candidate_filter` as a
  wider paper-only scout layer.
- `prior_probability_scout` is enabled by default for the broad paper profile.
  Disable it explicitly with `PREDICT_PRIOR_SCOUT_ENABLED=0` when you only want
  rule-derived signals.
- `v6_integrated_candidate` remains a compatibility alias for v8.
- If `data/models/baseline_prior.pkl` is absent, the daemon runs without a loaded prior and reports `prior_loaded=0`.
- Paper fills record `fill_source=wss_book`, `entry_offset_seconds`, book bid/ask/spread, and book hash.
- Settled paper fills record `settlement_outcome`, `settlement_source`, `won`, `pnl_usd`, and `roi`.
