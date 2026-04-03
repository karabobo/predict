"""
metrics.py — Shared signal/trade metric helpers.

Used by both the CLI scorecard and the dashboard so production reporting
does not drift across multiple implementations.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from typing import Any


def ensure_prediction_schema(db: sqlite3.Connection) -> None:
    """Lightweight SQLite migration for reporting columns."""
    for column in [
        "regime TEXT",
        "conviction_score TEXT",
        "should_trade INTEGER DEFAULT 1",
        "market_price_yes_snapshot REAL",
        "seconds_to_expiry INTEGER",
    ]:
        try:
            db.execute(f"ALTER TABLE predictions ADD COLUMN {column}")
            db.commit()
        except sqlite3.OperationalError:
            pass


def bet_size_for_conviction(conviction_score: Any) -> float:
    conviction = _as_int(conviction_score)
    if conviction >= 4:
        return 200.0
    if conviction >= 3:
        return 75.0
    return 0.0


def trade_eligible(row: dict[str, Any]) -> bool:
    conviction = _as_int(_value(row, "conviction_score", 0))
    should_trade = _as_bool(_value(row, "should_trade", conviction >= 3))
    return should_trade and bet_size_for_conviction(conviction) > 0


def prediction_direction(row: dict[str, Any]) -> str:
    estimate = _as_float(_value(row, "estimate", 0.5))
    if estimate > 0.5:
        return "UP"
    if estimate < 0.5:
        return "DOWN"
    return "SKIP"


def select_latest_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = _group_rows_by_market_agent(rows)
    selected = []
    for members in grouped.values():
        selected.append(dict(members[-1]))
    return selected


def select_exposure_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Pick one representative row per market-agent path.

    If the path ever became trade-eligible, keep the first trade row because that
    is the first point of operational exposure. Otherwise keep the latest row as
    the final non-trade snapshot.
    """
    grouped = _group_rows_by_market_agent(rows)
    selected = []
    for members in grouped.values():
        exposure = next((dict(row) for row in members if trade_eligible(row)), None)
        if exposure is not None:
            exposure["_selection_mode"] = "first_trade"
            selected.append(exposure)
            continue
        latest = dict(members[-1])
        latest["_selection_mode"] = "latest_snapshot"
        selected.append(latest)
    return selected


def compute_path_risk(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped = _group_rows_by_market_agent(rows)
    results: dict[str, dict[str, Any]] = {}

    for (_market_id, agent), members in grouped.items():
        metrics = results.setdefault(agent, _empty_path_risk_metrics(agent))
        latest = members[-1]
        directions = [prediction_direction(row) for row in members]
        trade_directions = [prediction_direction(row) for row in members if trade_eligible(row)]
        ever_trade = any(trade_eligible(row) for row in members)
        latest_trade = trade_eligible(latest)

        metrics["market_paths"] += 1
        metrics["total_updates"] += len(members)
        metrics["max_updates_per_market"] = max(metrics["max_updates_per_market"], len(members))

        if ever_trade:
            metrics["ever_trade_markets"] += 1
        if ever_trade and not latest_trade:
            metrics["trade_then_skip_markets"] += 1
        if len(set(directions)) > 1:
            metrics["direction_flip_markets"] += 1
        if len(set(direction for direction in trade_directions if direction != "SKIP")) > 1:
            metrics["trade_direction_flip_markets"] += 1
        if any(direction in {"UP", "DOWN"} for direction in directions) and directions[-1] == "SKIP":
            metrics["call_then_skip_markets"] += 1

    for metrics in results.values():
        if metrics["market_paths"] > 0:
            metrics["avg_updates_per_market"] = metrics["total_updates"] / metrics["market_paths"]
            if metrics["ever_trade_markets"] > 0:
                metrics["trade_withdraw_rate"] = (
                    metrics["trade_then_skip_markets"] / metrics["ever_trade_markets"]
                )
    return results


def compute_pnl(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregate per-agent binary-options P&L with conviction gating."""
    if not rows:
        return {}

    results: dict[str, dict[str, Any]] = {}

    for row in rows:
        agent = _as_str(row, "agent", "unknown")
        metrics = results.setdefault(agent, _empty_agent_metrics(agent))

        conviction = _as_int(_value(row, "conviction_score", 0))
        should_trade = _as_bool(_value(row, "should_trade", conviction >= 3))
        wager = bet_size_for_conviction(conviction)

        if not should_trade or wager <= 0:
            metrics["num_skips"] += 1
            continue

        estimate = _as_float(_value(row, "estimate", 0.5))
        price_yes = _as_float(_value(row, "market_price_yes_snapshot", _value(row, "price_yes", 0.5)))
        outcome = _as_int(_value(row, "outcome", 0))
        direction = "UP" if estimate >= 0.5 else "DOWN"
        entry_price = _entry_price(direction, price_yes)
        won = (direction == "UP" and outcome == 1) or (direction == "DOWN" and outcome == 0)
        pnl = wager * (1.0 / entry_price - 1.0) if won else -wager

        metrics["total_wagered"] += wager
        metrics["total_pnl"] += pnl
        metrics["num_bets"] += 1

        if won:
            metrics["num_wins"] += 1
            metrics["gross_wins"] += pnl
        else:
            metrics["num_losses"] += 1
            metrics["gross_losses"] += pnl

        metrics["bet_results"].append({
            "market_id": _as_str(row, "market_id", ""),
            "agent": agent,
            "direction": direction,
            "estimate": estimate,
            "price_yes": price_yes,
            "entry_price": entry_price,
            "outcome": outcome,
            "conviction_score": conviction,
            "wager": wager,
            "pnl": pnl,
            "won": won,
        })

        _update_drawdown(metrics, pnl)

    for agent_metrics in results.values():
        if agent_metrics["total_wagered"] > 0:
            agent_metrics["roi"] = (
                agent_metrics["total_pnl"] / agent_metrics["total_wagered"] * 100.0
            )
        if agent_metrics["num_wins"] > 0:
            agent_metrics["avg_win"] = agent_metrics["gross_wins"] / agent_metrics["num_wins"]
        if agent_metrics["num_losses"] > 0:
            agent_metrics["avg_loss"] = agent_metrics["gross_losses"] / agent_metrics["num_losses"]
        if agent_metrics["num_bets"] > 0:
            agent_metrics["win_rate"] = agent_metrics["num_wins"] / agent_metrics["num_bets"]

    return results


def compute_ensemble_pnl(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate a simple ensemble trade result from resolved prediction rows."""
    if not rows:
        return _empty_ensemble_metrics()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_as_str(row, "market_id", "")].append(row)

    ensemble_rows: list[dict[str, Any]] = []
    skipped = 0

    for market_id, members in grouped.items():
        eligible = [
            row for row in members
            if trade_eligible(row)
        ]
        if not eligible:
            skipped += 1
            continue

        avg_estimate = sum(_as_float(_value(row, "estimate", 0.5)) for row in eligible) / len(eligible)
        if abs(avg_estimate - 0.5) < 1e-9:
            skipped += 1
            continue

        ensemble_rows.append({
            "market_id": market_id,
            "agent": "ensemble",
            "estimate": avg_estimate,
            "price_yes": _as_float(_value(eligible[0], "price_yes", 0.5)),
            "outcome": _as_int(_value(eligible[0], "outcome", 0)),
            "conviction_score": max(_as_int(_value(row, "conviction_score", 0)) for row in eligible),
            "should_trade": 1,
        })

    pnl = compute_pnl(ensemble_rows)
    result = pnl.get("ensemble", _empty_ensemble_metrics())
    result["num_skipped"] = skipped
    return result


def compute_ev_breakeven(pnl_by_agent: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compute win-rate breakeven and expected value from per-bet results."""
    bet_results = _collect_bet_results(pnl_by_agent)
    if not bet_results:
        return {
            "total_bets": 0,
            "current_wr": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "breakeven_wr": 0.5,
            "margin": -0.5,
            "ev": 0.0,
        }

    wins = [bet["pnl"] for bet in bet_results if bet["won"]]
    losses = [bet["pnl"] for bet in bet_results if not bet["won"]]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    current_wr = len(wins) / len(bet_results)

    if avg_win > 0 and avg_loss == 0:
        breakeven_wr = 0.0
    elif avg_win == 0 and avg_loss < 0:
        breakeven_wr = 1.0
    else:
        denom = avg_win + abs(avg_loss)
        breakeven_wr = abs(avg_loss) / denom if denom > 0 else 0.5

    ev = current_wr * avg_win + (1 - current_wr) * avg_loss
    return {
        "total_bets": len(bet_results),
        "current_wr": current_wr,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "breakeven_wr": breakeven_wr,
        "margin": current_wr - breakeven_wr,
        "ev": ev,
    }


def build_distribution_svg(pnl_by_agent: dict[str, dict[str, Any]]) -> str:
    """Render a compact SVG of per-bet P&L distribution."""
    bet_results = _collect_bet_results(pnl_by_agent)
    width = 720
    height = 220
    zero_y = height / 2

    if not bet_results:
        return (
            f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
            f'<rect width="{width}" height="{height}" fill="#0d1117"/>'
            f'<text x="{width/2}" y="{height/2}" fill="#8b949e" font-size="14" text-anchor="middle">'
            "No settled trades yet"
            "</text></svg>"
        )

    pnls = [bet["pnl"] for bet in bet_results]
    max_abs = max(max(abs(p) for p in pnls), 1.0)
    spacing = width / (len(pnls) + 1)
    circles = []

    for index, pnl in enumerate(pnls, start=1):
        x = spacing * index
        y = zero_y - (pnl / max_abs) * (height * 0.35)
        color = "#3fb950" if pnl >= 0 else "#f85149"
        circles.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" />')

    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{width}" height="{height}" fill="#0d1117" rx="12" />'
        f'<line x1="30" y1="{zero_y:.1f}" x2="{width-30}" y2="{zero_y:.1f}" stroke="#30363d" stroke-width="1" />'
        f'<text x="30" y="28" fill="#8b949e" font-size="12">Wins spread</text>'
        f'<text x="30" y="{height-20}" fill="#8b949e" font-size="12">Losses cluster</text>'
        + "".join(circles) +
        "</svg>"
    )


def _empty_agent_metrics(agent: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "total_pnl": 0.0,
        "total_wagered": 0.0,
        "num_bets": 0,
        "num_skips": 0,
        "num_wins": 0,
        "num_losses": 0,
        "gross_wins": 0.0,
        "gross_losses": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "roi": 0.0,
        "win_rate": 0.0,
        "max_drawdown": 0.0,
        "bet_results": [],
        "_equity": 0.0,
        "_peak_equity": 0.0,
    }


def _empty_ensemble_metrics() -> dict[str, Any]:
    metrics = _empty_agent_metrics("ensemble")
    metrics["num_skipped"] = 0
    metrics.pop("_equity", None)
    metrics.pop("_peak_equity", None)
    return metrics


def _empty_path_risk_metrics(agent: str) -> dict[str, Any]:
    return {
        "agent": agent,
        "market_paths": 0,
        "total_updates": 0,
        "avg_updates_per_market": 0.0,
        "max_updates_per_market": 0,
        "ever_trade_markets": 0,
        "trade_then_skip_markets": 0,
        "trade_withdraw_rate": 0.0,
        "direction_flip_markets": 0,
        "trade_direction_flip_markets": 0,
        "call_then_skip_markets": 0,
    }


def _update_drawdown(metrics: dict[str, Any], pnl: float) -> None:
    metrics["_equity"] += pnl
    metrics["_peak_equity"] = max(metrics["_peak_equity"], metrics["_equity"])


def _group_rows_by_market_agent(rows: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (_as_str(row, "market_id", ""), _as_str(row, "agent", "unknown"))
        grouped[key].append(row)
    for key in grouped:
        grouped[key] = sorted(grouped[key], key=lambda row: _as_str(row, "predicted_at", ""))
    return grouped
    drawdown = metrics["_peak_equity"] - metrics["_equity"]
    metrics["max_drawdown"] = max(metrics["max_drawdown"], drawdown)


def _collect_bet_results(pnl_by_agent: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for agent_metrics in pnl_by_agent.values():
        results.extend(agent_metrics.get("bet_results", []))
    return results


def _entry_price(direction: str, price_yes: float) -> float:
    price_yes = min(max(price_yes, 0.01), 0.99)
    return price_yes if direction == "UP" else min(max(1.0 - price_yes, 0.01), 0.99)


def _value(row: Any, key: str, default: Any) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _as_str(row: Any, key: str, default: str) -> str:
    value = _value(row, key, default)
    return default if value is None else str(value)


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
