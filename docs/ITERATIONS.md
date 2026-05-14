# Project Iterations

This repository has gone through several distinct design phases. Older code and docs are kept for reference, but only the current production path should be treated as active.

## Current

Current production is a deterministic baseline with separate research challengers.

- Production path: `src/predict.py`, `src/score.py`, `src/dashboard.py`
- Strategy modules: `src/strategies/`
- Research gate: `src/v3/arena.py`, `src/v3/promotion.py`
- Reports: `docs/research/latest.md`

## V3

V3 was the research-heavy phase focused on walk-forward testing, ML challengers, and arena-style comparisons.

- Main code: `src/v3/`
- Key notes: `docs/BACKTEST_FINDINGS.md`, `docs/bot-V3.1.md`, `docs/archive/v3-hybrid-quant-spec.md`, `docs/archive/v3-staged-implementation-plan.md`
- Status: partially retained, but only promotion tooling is still active

## V2

V2 was the LLM ensemble phase with conviction sizing and progressively tighter agent filtering.

- Historical references: `docs/archive/v2-project-evolution.md`, `docs/archive/v2-backtest-results.md`, `docs/archive/v2-deployment-plan.md`
- Status: archived for lessons learned, not used in production

## V1

V1 was the original prompt-driven multi-agent experiment.

- Historical references: early V1 material is summarized inside `docs/archive/v2-project-evolution.md` and `docs/archive/v2-backtest-results.md`
- Characteristics: Claude-backed agents, prompt iteration, Brier-first optimization
- Status: obsolete

## How To Read The Repo

- Use `README.md` for the current operating model.
- Use `program.md` for current production and research rules.
- Use `docs/ROADMAP.md` for active direction.
- Use `docs/archive/` only when you need historical context.
