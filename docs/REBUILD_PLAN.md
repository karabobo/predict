# Rebuild Plan

Baseline date: 2026-05-14

## Current Baseline

- Runtime: Python 3.12.3 with project `.venv`.
- Dependency install: completed from `requirements.txt`.
- Test baseline: `135 passed in 23.58s` with `.venv/bin/python -m pytest tests/ -v`.
- Migration data health: core SQLite files passed `pragma quick_check`.

## Current Boundaries

The project currently combines four concerns:

- Production signal and operations: `fetch_markets.py`, `predict.py`, `realtime_signal.py`, `settlement.py`, `score.py`, `ci_run.py`, loop scripts.
- Strategy rules: `strategies/`, `v3/rule_registry.py`, `v3/rule_variants.py`.
- Research and promotion: `v3/arena.py`, `v3/promotion.py`, `v3/coaches.py`, `v3/probability_foundation.py`, backtest modules.
- Reporting: `dashboard.py`, generated `docs/index.html`, `docs/research/*`.

Execution is documented as a separate concern owned by `predict-nautilus`; this repo should remain the research, scoring, dashboard, and production signal source.

## Immediate Risks

- Invalid read-only `.git` directory blocks normal Git usage. Replace it with a real local repository before large edits.
- Several modules import by path side effects or mixed styles (`src.*`, top-level modules, `sys.path.insert`). This makes package boundaries fragile.
- Runtime paths are hard-coded in some commands and defaults, especially `/root/Data/*` and `/root/program/*`.
- `dashboard.py` is too large and mixes queries, metrics, templates, rendering, and server startup.
- `v3/rule_variants.py` and `v3/arena.py` are large rule/research hubs with high change risk.
- Environment handling is duplicated across `notifier.py`, `live_trading.py`, `ai_client.py`, scripts, and PM2 config.
- Data access is direct SQLite usage scattered across production, reporting, research, and live-trading modules.
- Historical docs and generated research reports are numerous; they should stay out of the active refactor path.

## Target Shape

Use explicit layers:

- `core`: shared config, paths, time, database connection helpers, environment loading.
- `market_data`: Gamma/CLOB/BTC data clients and parsers.
- `strategy`: production-safe signal interfaces and rule implementations.
- `ops`: prediction, realtime, settlement, scoring, notification orchestration.
- `research`: arena, promotion, coaches, models, historical backtests.
- `reporting`: dashboard queries, view models, HTML rendering, local server.
- `execution_bridge`: minimal handoff types and hooks for `predict-nautilus`, without growing live execution here.

## Refactor Phases

### Phase 0: Safety Rails

- Replace invalid `.git` with a valid local repository and commit the migrated baseline.
- Keep `.env`, `.venv`, runtime databases, PM2 state, logs, generated run reports, and caches untracked.
- Add a short baseline command note to `docs/TESTING.md` if missing.

### Phase 1: Configuration and Paths

- Introduce one shared module for project root, data paths, docs paths, and env loading.
- Replace hard-coded `/root/Data/*` defaults with repository-relative defaults where migrated files exist.
- Keep CLI flags for external overrides.
- Verify with full tests.

### Phase 2: Import and Package Hygiene

- Standardize internal imports on package imports.
- Remove ad hoc `sys.path.insert` from runtime modules where possible.
- Preserve script entrypoints with small wrappers if needed.
- Verify smoke and full test suite after each group.

### Phase 3: Data Access Boundary

- Add small repository modules for prediction DB, research DB, and backtest DB operations.
- Move schema creation and common queries out of orchestration modules.
- Do not change schemas unless tests and migration scripts cover the change.

### Phase 4: Production Ops Split

- Keep `predict.py`, `realtime_signal.py`, `settlement.py`, and `score.py` as thin orchestrators.
- Move reusable decision, storage, and notification logic into focused modules.
- Keep AI out of production paths.

### Phase 5: Dashboard Decomposition

- Split `dashboard.py` into query functions, metrics/view-model builders, template/rendering, and Flask server.
- Keep generated HTML output identical or intentionally diff-reviewed.
- Add focused tests around dashboard metrics before moving code.

### Phase 6: Research Consolidation

- Separate rule definitions, rule metadata, candidate loading, and promotion evaluation.
- Keep production rule profile behavior stable.
- Add regression tests for any renamed candidates or changed router behavior.

### Phase 7: Execution Boundary

- Reduce `live_trading.py` in this repo to explicit paper/live plan construction and audit recording.
- Keep venue execution details in `predict-nautilus`.
- Document the handoff contract.

## Verification Policy

Run after each phase:

```bash
.venv/bin/python -m pytest tests/ -v
```

Run targeted tests before full suite when changing a specific area:

```bash
.venv/bin/python -m pytest tests/test_smoke.py tests/test_regression.py -v
.venv/bin/python -m pytest tests/test_rule_registry.py tests/test_rule_variants.py -v
.venv/bin/python -m pytest tests/test_realtime_signal.py tests/test_settlement.py -v
```
