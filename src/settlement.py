"""
settlement.py — Official and provisional market outcome parsing.

Official Polymarket resolution remains the canonical source of truth for scorecards.
Provisional outcomes exist only to accelerate research and dashboard visibility.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from fetch_markets import GAMMA_API, HTTP_TIMEOUT, ensure_market_schema

PROVISIONAL_PRICE_THRESHOLD = float(os.getenv("PROVISIONAL_PRICE_THRESHOLD", "0.99"))
PROVISIONAL_CONFIRMATIONS = int(os.getenv("PROVISIONAL_CONFIRMATIONS", "2"))
OFFICIAL_CLOSED_PRICE_THRESHOLD = float(os.getenv("OFFICIAL_CLOSED_PRICE_THRESHOLD", "0.99"))


def fetch_market_state(market_id: str) -> dict[str, Any]:
    resp = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    market = resp.json()

    raw_prices = market.get("outcomePrices", "[]")
    prices = json.loads(raw_prices) if isinstance(raw_prices, str) else raw_prices
    price_yes = None
    price_no = None
    try:
        if prices:
            price_yes = float(prices[0])
        if len(prices) > 1:
            price_no = float(prices[1])
        elif price_yes is not None:
            price_no = round(1.0 - price_yes, 6)
    except (TypeError, ValueError, IndexError):
        price_yes = None
        price_no = None

    return {
        "market_id": market_id,
        "closed": bool(market.get("closed")),
        "price_yes": price_yes,
        "price_no": price_no,
        "payload": market,
    }


def determine_official_outcome(state: dict[str, Any]) -> int | None:
    price_yes = state.get("price_yes")
    if price_yes is None or not state.get("closed"):
        return None
    price_yes = float(price_yes)
    if price_yes >= OFFICIAL_CLOSED_PRICE_THRESHOLD:
        return 1
    if price_yes <= 1.0 - OFFICIAL_CLOSED_PRICE_THRESHOLD:
        return 0
    return None


def determine_provisional_outcome(
    snapshots: list[dict[str, Any]],
    *,
    threshold: float = PROVISIONAL_PRICE_THRESHOLD,
    confirmations: int = PROVISIONAL_CONFIRMATIONS,
) -> int | None:
    if confirmations <= 0 or len(snapshots) < confirmations:
        return None

    recent = snapshots[:confirmations]
    yes_values = [_as_float(snapshot.get("price_yes")) for snapshot in recent]
    if any(value is None for value in yes_values):
        return None

    yes_high = all(value >= threshold for value in yes_values)
    yes_low = all(value <= (1.0 - threshold) for value in yes_values)

    if yes_high and not yes_low:
        return 1
    if yes_low and not yes_high:
        return 0
    return None


def mark_official_resolved(
    db: sqlite3.Connection,
    market_id: str,
    outcome: int,
    *,
    resolved_at: str | None = None,
    commit: bool = True,
) -> None:
    ensure_market_schema(db)
    resolved_at = resolved_at or datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE markets
        SET resolved = 1,
            outcome = ?,
            official_resolved_at = ?,
            provisional_outcome = NULL,
            provisional_resolved_at = NULL,
            provisional_source = NULL
        WHERE id = ?
        """,
        (int(outcome), resolved_at, market_id),
    )
    if commit:
        db.commit()


def mark_provisional_resolved(
    db: sqlite3.Connection,
    market_id: str,
    outcome: int,
    *,
    source: str,
    resolved_at: str | None = None,
    commit: bool = True,
) -> None:
    ensure_market_schema(db)
    resolved_at = resolved_at or datetime.now(timezone.utc).isoformat()
    db.execute(
        """
        UPDATE markets
        SET provisional_outcome = ?,
            provisional_resolved_at = ?,
            provisional_source = ?
        WHERE id = ? AND resolved = 0
        """,
        (int(outcome), resolved_at, source, market_id),
    )
    if commit:
        db.commit()


def clear_provisional_resolution(
    db: sqlite3.Connection,
    market_id: str,
    *,
    commit: bool = True,
) -> None:
    ensure_market_schema(db)
    db.execute(
        """
        UPDATE markets
        SET provisional_outcome = NULL,
            provisional_resolved_at = NULL,
            provisional_source = NULL
        WHERE id = ?
        """,
        (market_id,),
    )
    if commit:
        db.commit()


def sync_settlements(
    db: sqlite3.Connection,
    *,
    include_provisional: bool = True,
    now_iso: str | None = None,
    fetch_state: Callable[[str], dict[str, Any]] = fetch_market_state,
) -> dict[str, int]:
    ensure_market_schema(db)
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    rows = db.execute(
        """
        SELECT id, end_date, provisional_outcome
        FROM markets
        WHERE resolved = 0
          AND end_date <= ?
        ORDER BY end_date ASC
        """,
        (now_iso,),
    ).fetchall()

    counts = {
        "checked": len(rows),
        "official_resolved": 0,
        "provisional_created": 0,
        "provisional_cleared": 0,
        "errors": 0,
    }
    provisional_source = (
        f"gamma_outcome_prices:{PROVISIONAL_PRICE_THRESHOLD:.2f}x{PROVISIONAL_CONFIRMATIONS}"
    )

    for row in rows:
        market_id = str(row["id"])
        try:
            state = fetch_state(market_id)
        except Exception:
            counts["errors"] += 1
            continue

        observed_at = datetime.now(timezone.utc).isoformat()
        if state.get("price_yes") is not None:
            _store_price_snapshot(
                db,
                market_id=market_id,
                observed_at=observed_at,
                price_yes=_as_float(state.get("price_yes")),
                price_no=_as_float(state.get("price_no")),
                closed=1 if state.get("closed") else 0,
            )

        official_outcome = determine_official_outcome(state)
        if official_outcome is not None:
            mark_official_resolved(
                db,
                market_id,
                official_outcome,
                resolved_at=observed_at,
                commit=False,
            )
            counts["official_resolved"] += 1
            continue

        if not include_provisional or state.get("price_yes") is None:
            continue

        snapshots = _load_recent_snapshots(db, market_id, PROVISIONAL_CONFIRMATIONS)
        provisional_outcome = determine_provisional_outcome(snapshots)
        current_provisional = row["provisional_outcome"]

        if provisional_outcome is not None:
            if current_provisional is None or int(current_provisional) != provisional_outcome:
                mark_provisional_resolved(
                    db,
                    market_id,
                    provisional_outcome,
                    source=provisional_source,
                    resolved_at=observed_at,
                    commit=False,
                )
                counts["provisional_created"] += 1
        elif current_provisional is not None:
            clear_provisional_resolution(db, market_id, commit=False)
            counts["provisional_cleared"] += 1

    db.commit()
    return counts


def run_settlement_phase(db: sqlite3.Connection) -> dict[str, int]:
    counts = sync_settlements(db, include_provisional=True)
    print("[settlement] Checked ended markets:", counts["checked"])
    if counts["official_resolved"]:
        print(f"[settlement] Officially resolved: {counts['official_resolved']}")
    if counts["provisional_created"]:
        print(f"[settlement] Provisional outcomes created: {counts['provisional_created']}")
    if counts["provisional_cleared"]:
        print(f"[settlement] Provisional outcomes cleared: {counts['provisional_cleared']}")
    if counts["errors"]:
        print(f"[settlement] API/state errors: {counts['errors']}")
    return counts


def _store_price_snapshot(
    db: sqlite3.Connection,
    *,
    market_id: str,
    observed_at: str,
    price_yes: float | None,
    price_no: float | None,
    closed: int,
) -> None:
    db.execute(
        """
        INSERT INTO market_price_snapshots (market_id, observed_at, price_yes, price_no, closed)
        VALUES (?, ?, ?, ?, ?)
        """,
        (market_id, observed_at, price_yes, price_no, int(closed)),
    )


def _load_recent_snapshots(
    db: sqlite3.Connection,
    market_id: str,
    confirmations: int,
) -> list[dict[str, Any]]:
    rows = db.execute(
        """
        SELECT observed_at, price_yes, price_no, closed
        FROM market_price_snapshots
        WHERE market_id = ?
        ORDER BY observed_at DESC
        LIMIT ?
        """,
        (market_id, confirmations),
    ).fetchall()
    return [dict(row) for row in rows]


def _as_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
