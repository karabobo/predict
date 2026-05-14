# Project Map and Operations Runbook

Last reviewed: 2026-05-13 UTC

This file is the source of truth for how the local `predict` project and the
execution companion `predict-nautilus` fit together.

## Repository Roles

| Path | Role | Git state | Runtime manager |
| --- | --- | --- | --- |
| `/root/program/predict` | Research, signal generation, scoring, dashboard, Telegram visibility | Branch `v6`, remote `origin` -> `git@github.com:karabobo/predict.git` | PM2 with local `PM2_HOME=.pm2` |
| `/root/program/predict-nautilus` | NautilusTrader execution companion for paper/live venue integration | Local repo with no commits and no remote | `systemd --user` |

The intended boundary is:

- `predict` owns the baseline rules, research gates, scorekeeping, dashboard, and
  alerting review.
- `predict-nautilus` owns Polymarket market-data/execution plumbing, risk sizing,
  paper/live mode behavior, and Nautilus strategy wiring.
- Baseline signal code is intentionally copied into `predict-nautilus`; changes
  should be promoted from `predict` only after tests and research review.

## Current Runtime Topology

### `predict`

Manage with:

```bash
cd /root/program/predict
./scripts/pm2ctl.sh status
./scripts/pm2ctl.sh logs realtime-loop --lines 120
./scripts/pm2ctl.sh restart realtime-loop
```

PM2 apps currently defined in `ecosystem.config.cjs`:

| PM2 app | Entrypoint | Cadence | Responsibility |
| --- | --- | --- | --- |
| `realtime-loop` | `scripts/realtime_signal_loop.sh` -> `src/realtime_signal.py` | about 5s | Watch current BTC 5m market, patch spot price, emit deduped realtime trade alerts |
| `predict-loop` | `scripts/predict_loop.sh` -> `src/predict_cycle.py` | about 30s | Fetch markets and write one baseline prediction for unseen markets |
| `settlement-loop` | `scripts/settlement_loop.sh` -> `src/settlement_cycle.py` | about 30s | Resolve settled markets and provisional outcomes |
| `ops-loop` | `scripts/ops_loop.sh` -> `src/ops_cycle.py` | about 300s | Score, optional live-order hook, rebuild dashboard |
| `dashboard` | `src/dashboard.py` | long-running web server | Serve local dashboard |
| `research-loop` | `scripts/research_loop.sh` -> `src/v3/promotion.py` | about 1h | Run blocked promotion checks |
| `coach-loop` | `scripts/coach_loop.sh` -> `src/v3/coach_cycle.py` | about 300s | Coach audits only when `COACH_LOOP_ENABLED=true` |

Telegram notifications are centralized in `src/notifier.py` and use:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_NOTIFY_PREFIX=predict`

Two `predict` paths can generate baseline trade alerts:

- `src/realtime_signal.py`: realtime, event-deduped alerts.
- `src/predict.py`: normal prediction-cycle alerts.

If messages look similar but not identical, first check whether they came from
`realtime-loop` or `predict-loop`.

### `predict-nautilus`

Manage with:

```bash
systemctl --user status predict-nautilus-paper --no-pager
systemctl --user restart predict-nautilus-paper
journalctl --user -u predict-nautilus-paper -f
```

Current state:

| Unit | Entrypoint | State | Responsibility |
| --- | --- | --- | --- |
| `predict-nautilus-paper.service` | `scripts/run_paper.py` | enabled and running | Nautilus paper runner with Telegram enabled |
| `predict-nautilus-shadow.service` | `scripts/run_shadow.py` | disabled/inactive | Shadow market-data runner |

Important environment flags:

- `NAUTILUS_ENABLE_EXECUTION=false`: no real venue execution.
- `NAUTILUS_ENABLE_TELEGRAM=true`: paper events may notify.
- `TELEGRAM_NOTIFY_PREFIX=predict-nautilus-live`: distinguish from `predict`.

The name `predict-nautilus-live` is only a Telegram prefix today; live execution
still requires `NAUTILUS_ENABLE_EXECUTION=true`.

## Data Policy

`predict` has generated SQLite files that are runtime state, not source code.

| File | Purpose | Git policy |
| --- | --- | --- |
| `data/predictions.db` | Production markets, predictions, realtime events, scores | Local runtime state only; too large for GitHub |
| `data/v3_research.db` | Research runs and coach audits | Local runtime state |
| `data/polymarket_backtest.db` | Historical BTC 5m backtest dataset | Local runtime state |
| `docs/index.html` | Generated dashboard snapshot | Tracked |
| `docs/research/latest.md` | Latest research summary | Tracked |
| `docs/research/rule_candidates.md` | Coach/rule candidate summary | Tracked |

GitHub rejects files over 100 MB. `data/predictions.db` has exceeded that
threshold, so it must stay out of commits. Preserve it locally for operations;
do not force-add it.

## Routine Commands

### `predict`

```bash
cd /root/program/predict
.venv/bin/python -m pytest tests/ -v
./scripts/pm2ctl.sh status
./scripts/pm2ctl.sh logs predict-loop --lines 120
./scripts/pm2ctl.sh logs realtime-loop --lines 120
```

One-shot local cycle:

```bash
cd /root/program/predict/src
../.venv/bin/python -u ci_run.py
```

### `predict-nautilus`

```bash
cd /root/program/predict-nautilus
.venv/bin/python -m pytest -v
.venv/bin/python scripts/run_paper.py --check-config
systemctl --user status predict-nautilus-paper --no-pager
```

## Cleanup Rules

- Keep secrets only in `.env` files or systemd environment files; never hard-code
  them in scripts.
- Use PM2 only for `predict`; use `systemd --user` only for `predict-nautilus`.
- Treat `predict-nautilus` as execution code. Research changes start in
  `predict` and move over only when deliberately promoted.
- Do not commit logs, `.pm2`, `.venv`, `__pycache__`, `.pytest_cache`, or large
  SQLite runtime databases.
