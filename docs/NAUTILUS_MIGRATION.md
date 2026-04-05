# Nautilus Migration

The project is now split into two repos with different responsibilities.

## `predict`

This repo remains the research and operations source of truth:

- baseline strategy evolution
- sample-out promotion in `src/v3/`
- scoring and dashboard generation
- Telegram visibility and incident review

Do not add new live execution complexity here unless it is needed for short-term continuity.

## `predict-nautilus`

The sibling repo at `/root/program/predict-nautilus` is the execution-focused project:

- NautilusTrader runner and venue integration
- Polymarket market-data and execution wiring
- maker intent state, sizing, and layer logic
- execution telemetry and local execution store

## Migration rule

Research and validation should happen in `predict` first. Execution policy should only be copied into `predict-nautilus` after the logic is stable enough to trade.
