# Repository Guidelines

## Project Structure & Module Organization
Core application code lives in `src/`. The main cycle is split across modules such as `fetch_markets.py`, `predict.py`, `score.py`, `run_cycle.py`, and `dashboard.py`; current research and promotion logic lives under `src/v3/`. Tests are in `tests/` and are organized by behavior (`test_pnl.py`, `test_regime.py`, `test_regression.py`). Static dashboard output and long-form project notes live in `docs/`. Operational scripts are in `scripts/`, while high-level bot rules and macro context are tracked in `program.md` and `config/`.

## Build, Test, and Development Commands
Set up a local environment with `python -m venv venv && source venv/bin/activate && pip install -r requirements.txt`.
Run the full test suite with `python -m pytest tests/ -v`.
Run a single prediction cycle from `src/` with `python ci_run.py`.
Start the local dashboard from `src/` with `python dashboard.py` and open `http://localhost:5050`.
Run focused checks when touching trading math with `python -m pytest tests/test_pnl.py tests/test_regression.py -v`.

## Coding Style & Naming Conventions
Use Python with 4-space indentation, snake_case for modules, functions, and variables, and descriptive file names that match the domain (`btc_data.py`, `live_trading.py`). Keep modules focused; add new strategy or data logic under `src/` instead of embedding it in scripts. Follow the existing test style: small functions, explicit assertions, minimal fixtures. No formatter config is checked in, so match the surrounding style and keep imports and control flow simple.

## Testing Guidelines
`pytest` is the test framework. Treat tests as a deploy gate: CI stops before prediction runs if tests fail. Add or update tests for every logic change, and add a regression test for every production bug. Name new files `tests/test_<feature>.py` and new cases `test_<expected_behavior>()`. Prioritize `test_regression.py` for incident prevention and `test_smoke.py` for import or wiring failures.

## Commit & Pull Request Guidelines
Recent history uses short prefixes such as `feat:` for feature work and `Auto:` for scheduled bot updates. Follow that pattern, keep subject lines imperative, and separate generated cycle commits from manual code changes. Before opening a PR, run `python -m pytest tests/ -v`, summarize behavior changes, note any operational impact, and include screenshots when `docs/index.html` or dashboard output changes.

## Operational Notes
GitHub is the source of truth for generated data. If your change depends on current bot state, pull first, avoid relying on stale local artifacts, and document incidents in `docs/BREAK_FIX_LOG.md`.
