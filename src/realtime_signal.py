"""
realtime_signal.py — Rolling realtime signal loop for current BTC 5m markets.

This loop intentionally avoids settlement, scoring, research, and dashboard
generation. It only refreshes nearby markets, patches the latest BTC context
with a lightweight spot price, runs the alpha router, and sends Telegram on
deduped trade signal changes.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from btc_data import fetch_btc_candles, fetch_btc_spot_price
from fetch_markets import DB_PATH, fetch_active_markets, init_db, store_markets
from notifier import notify_baseline_trade
from predict import (
    ACTIVE_AGENT,
    _apply_regime_filter,
    _parse_timestamp,
    alpha_router_signal,
    compute_regime_from_candles,
    contrarian_signal,
    ensure_db_schema,
    store_prediction,
)

DEFAULT_SLEEP_SECONDS = 5
DEFAULT_MARKET_LOOKAHEAD_SECONDS = 360
DEFAULT_MARKET_GRACE_SECONDS = 30
DEFAULT_BTC_CONTEXT_REFRESH_SECONDS = 20
DEFAULT_NEAR_TIE_PCT = 0.00005
DEFAULT_SHADOW_RULE_PROFILE = "absorption_candidates_live"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _ensure_column(db: sqlite3.Connection, table: str, column_definition: str) -> None:
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")
        db.commit()
    except sqlite3.OperationalError:
        pass


def ensure_realtime_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_signal_state (
            market_id TEXT PRIMARY KEY,
            question TEXT,
            status TEXT,
            signal_key TEXT,
            should_trade INTEGER,
            direction TEXT,
            estimate REAL,
            confidence TEXT,
            conviction_score INTEGER,
            reason TEXT,
            regime TEXT,
            market_price_yes REAL,
            seconds_to_expiry INTEGER,
            reference_price REAL,
            current_price REAL,
            live_direction TEXT,
            distance_from_reference_pct REAL,
            price_source TEXT,
            updated_at TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            signal_key TEXT NOT NULL,
            direction TEXT,
            estimate REAL,
            confidence TEXT,
            conviction_score INTEGER,
            reason TEXT,
            regime TEXT,
            market_price_yes REAL,
            seconds_to_expiry INTEGER,
            reference_price REAL,
            current_price REAL,
            live_direction TEXT,
            distance_from_reference_pct REAL,
            price_source TEXT,
            notified INTEGER DEFAULT 0,
            prediction_id INTEGER,
            created_at TEXT,
            UNIQUE (market_id, signal_key)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_shadow_rule_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            rule_name TEXT NOT NULL,
            signal_key TEXT NOT NULL,
            would_trade INTEGER,
            direction TEXT,
            estimate REAL,
            confidence TEXT,
            conviction_score INTEGER,
            reason TEXT,
            regime TEXT,
            market_price_yes REAL,
            seconds_to_expiry INTEGER,
            reference_price REAL,
            current_price REAL,
            live_direction TEXT,
            distance_from_reference_pct REAL,
            price_source TEXT,
            created_at TEXT,
            UNIQUE (market_id, profile_name, rule_name, signal_key)
        )
        """
    )
    for column in (
        "reference_price REAL",
        "current_price REAL",
        "live_direction TEXT",
        "distance_from_reference_pct REAL",
        "price_source TEXT",
    ):
        _ensure_column(db, "realtime_shadow_rule_events", column)
    db.commit()


def refresh_active_markets(db: sqlite3.Connection) -> int:
    markets = fetch_active_markets()
    store_markets(db, markets)
    return len(markets)


def select_realtime_market(
    db: sqlite3.Connection,
    *,
    now: datetime | None = None,
    lookahead_seconds: int = DEFAULT_MARKET_LOOKAHEAD_SECONDS,
    grace_seconds: int = DEFAULT_MARKET_GRACE_SECONDS,
) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    min_end = (now - timedelta(seconds=grace_seconds)).isoformat()
    max_end = (now + timedelta(seconds=lookahead_seconds)).isoformat()
    rows = db.execute(
        """
        SELECT id, question, price_yes, price_no, end_date, token_yes
        FROM markets
        WHERE resolved = 0
          AND end_date > ?
          AND end_date <= ?
        ORDER BY end_date ASC
        LIMIT 12
        """,
        (min_end, max_end),
    ).fetchall()
    if not rows:
        return None

    candidates: list[dict[str, Any]] = []
    for row in rows:
        end_dt = _parse_timestamp(str(row["end_date"]))
        start_dt = end_dt - timedelta(minutes=5)
        if now >= end_dt:
            status = "grace"
        elif start_dt <= now < end_dt:
            status = "active"
        else:
            status = "warmup"
        candidates.append(
            {
                "id": str(row["id"]),
                "question": str(row["question"] or ""),
                "price_yes": float(row["price_yes"] or 0.0),
                "price_no": float(row["price_no"] or 0.0),
                "end_date": end_dt,
                "start_date": start_dt,
                "token_yes": row["token_yes"],
                "status": status,
            }
        )

    active = [m for m in candidates if m["status"] == "active"]
    if active:
        return active[0]
    warmup = [m for m in candidates if m["status"] == "warmup"]
    if warmup:
        return warmup[0]
    return candidates[0]


def apply_realtime_price_to_candles(candles: list[dict[str, Any]], price: float) -> list[dict[str, Any]]:
    patched = deepcopy(candles)
    if not patched:
        return patched

    last = dict(patched[-1])
    open_price = float(last.get("open", price) or price)
    high = max(float(last.get("high", price) or price), price, open_price)
    low = min(float(last.get("low", price) or price), price, open_price)
    body = abs(price - open_price)
    full_range = high - low
    last["close"] = price
    last["high"] = high
    last["low"] = low
    last["direction"] = "UP" if price >= open_price else "DOWN"
    last["body_pct"] = round((price - open_price) / open_price * 100, 4) if open_price > 0 else 0.0
    last["wick_ratio"] = round(1.0 - (body / full_range), 2) if full_range > 0 else 0.0
    patched[-1] = last
    return patched


def reference_price_for_market(market: dict[str, Any], candles: list[dict[str, Any]]) -> tuple[float | None, str]:
    start = market["start_date"]
    for candle in reversed(candles):
        raw = candle.get("open_time")
        if not raw:
            continue
        try:
            open_dt = _parse_timestamp(str(raw))
        except ValueError:
            continue
        if abs((open_dt - start).total_seconds()) <= 150:
            return float(candle["open"]), "matched_candle_open"
    if candles:
        return float(candles[-1]["open"]), "latest_candle_open_fallback"
    return None, "missing"


def live_direction(
    *,
    reference_price: float | None,
    current_price: float,
    near_tie_pct: float = DEFAULT_NEAR_TIE_PCT,
) -> tuple[str, float | None]:
    if reference_price is None or reference_price <= 0:
        return "UNKNOWN", None
    distance_pct = (current_price - reference_price) / reference_price
    if abs(distance_pct) <= near_tie_pct:
        return "NEAR_TIE", distance_pct
    return ("UP" if distance_pct > 0 else "DOWN"), distance_pct


def build_signal_key(signal: dict[str, Any]) -> str:
    reason = str(signal.get("reason", ""))
    reason_hash = hashlib.sha1(reason.encode("utf-8")).hexdigest()[:12]
    parts = [
        "trade" if signal.get("should_trade") else "skip",
        str(signal.get("direction") or "SKIP"),
        str(signal.get("confidence", "low")),
        str(int(signal.get("conviction_score", 0) or 0)),
        reason_hash,
    ]
    return ":".join(parts)


def _store_realtime_state(
    db: sqlite3.Connection,
    *,
    market: dict[str, Any],
    signal: dict[str, Any],
    signal_key: str,
    regime_label: str,
    seconds_to_expiry: int,
    reference_price: float | None,
    current_price: float,
    live_dir: str,
    distance_pct: float | None,
    price_source: str,
) -> None:
    db.execute(
        """
        INSERT INTO realtime_signal_state (
            market_id, question, status, signal_key, should_trade, direction,
            estimate, confidence, conviction_score, reason, regime,
            market_price_yes, seconds_to_expiry, reference_price, current_price,
            live_direction, distance_from_reference_pct, price_source, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id) DO UPDATE SET
            question = excluded.question,
            status = excluded.status,
            signal_key = excluded.signal_key,
            should_trade = excluded.should_trade,
            direction = excluded.direction,
            estimate = excluded.estimate,
            confidence = excluded.confidence,
            conviction_score = excluded.conviction_score,
            reason = excluded.reason,
            regime = excluded.regime,
            market_price_yes = excluded.market_price_yes,
            seconds_to_expiry = excluded.seconds_to_expiry,
            reference_price = excluded.reference_price,
            current_price = excluded.current_price,
            live_direction = excluded.live_direction,
            distance_from_reference_pct = excluded.distance_from_reference_pct,
            price_source = excluded.price_source,
            updated_at = excluded.updated_at
        """,
        (
            market["id"],
            market["question"],
            market["status"],
            signal_key,
            1 if signal.get("should_trade") else 0,
            signal.get("direction"),
            float(signal.get("estimate", 0.5)),
            str(signal.get("confidence", "low")),
            int(signal.get("conviction_score", 0) or 0),
            str(signal.get("reason", "")),
            regime_label,
            float(market["price_yes"]),
            seconds_to_expiry,
            reference_price,
            current_price,
            live_dir,
            distance_pct,
            price_source,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def _insert_realtime_event(
    db: sqlite3.Connection,
    *,
    market: dict[str, Any],
    signal: dict[str, Any],
    signal_key: str,
    regime_label: str,
    seconds_to_expiry: int,
    reference_price: float | None,
    current_price: float,
    live_dir: str,
    distance_pct: float | None,
    price_source: str,
) -> int | None:
    cursor = db.execute(
        """
        INSERT OR IGNORE INTO realtime_signal_events (
            market_id, signal_key, direction, estimate, confidence,
            conviction_score, reason, regime, market_price_yes,
            seconds_to_expiry, reference_price, current_price, live_direction,
            distance_from_reference_pct, price_source, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            market["id"],
            signal_key,
            signal.get("direction"),
            float(signal.get("estimate", 0.5)),
            str(signal.get("confidence", "low")),
            int(signal.get("conviction_score", 0) or 0),
            str(signal.get("reason", "")),
            regime_label,
            float(market["price_yes"]),
            seconds_to_expiry,
            reference_price,
            current_price,
            live_dir,
            distance_pct,
            price_source,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    if cursor.rowcount == 0:
        return None
    return int(db.execute("SELECT last_insert_rowid()").fetchone()[0])


def record_shadow_profile(
    db: sqlite3.Connection,
    *,
    market: dict[str, Any],
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    seconds_to_expiry: int,
    reference_price: float | None = None,
    current_price: float | None = None,
    live_direction: str | None = None,
    distance_from_reference_pct: float | None = None,
    price_source: str | None = None,
    profile_name: str | None = None,
) -> dict[str, Any]:
    profile_name = profile_name or os.getenv("PREDICT_SHADOW_RULE_PROFILE", DEFAULT_SHADOW_RULE_PROFILE)
    if not profile_name:
        return {"profile_name": "", "evaluated": 0, "inserted": 0, "errors": 0}

    from v3.rule_registry import run_rule_profile

    result = run_rule_profile(candles, regime, profile_name)
    inserted = 0
    evaluated = 0
    for evaluation in result.evaluations:
        if evaluation.error or not evaluation.signal:
            continue
        evaluated += 1
        signal = evaluation.signal
        signal_key = build_signal_key(signal)
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO realtime_shadow_rule_events (
                market_id, profile_name, rule_name, signal_key, would_trade,
                direction, estimate, confidence, conviction_score, reason,
                regime, market_price_yes, seconds_to_expiry, reference_price,
                current_price, live_direction, distance_from_reference_pct,
                price_source, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                market["id"],
                profile_name,
                evaluation.rule_name,
                signal_key,
                1 if signal.get("should_trade") else 0,
                signal.get("direction"),
                float(signal.get("estimate", 0.5)),
                str(signal.get("confidence", "low")),
                int(signal.get("conviction_score", 0) or 0),
                str(signal.get("reason", "")),
                regime["label"],
                float(market["price_yes"]),
                seconds_to_expiry,
                reference_price,
                current_price,
                live_direction,
                distance_from_reference_pct,
                price_source,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        inserted += int(cursor.rowcount > 0)
    db.commit()
    return {
        "profile_name": profile_name,
        "evaluated": evaluated,
        "inserted": inserted,
        "errors": len(result.errors),
    }


def run_realtime_once(
    db: sqlite3.Connection,
    *,
    cycle: int,
    btc_context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    ensure_db_schema(db)
    ensure_realtime_schema(db)
    refreshed = refresh_active_markets(db)
    now = now or datetime.now(timezone.utc)
    market = select_realtime_market(db, now=now)
    if market is None:
        return {"status": "no_market", "markets_refreshed": refreshed}

    if btc_context is None:
        btc_context = fetch_btc_candles(limit=20)
    if not btc_context or not btc_context.get("candles"):
        return {"status": "no_btc_context", "market_id": market["id"]}

    spot = fetch_btc_spot_price()
    current_price = float(spot["price"])
    candles = apply_realtime_price_to_candles(btc_context["candles"], current_price)
    regime = compute_regime_from_candles(candles)
    raw_signal = contrarian_signal(candles)
    alpha_signal = alpha_router_signal(candles, regime)
    final_signal = alpha_signal if alpha_signal.get("should_trade") else _apply_regime_filter(raw_signal, regime)
    if not final_signal.get("should_trade") and alpha_signal.get("reason"):
        final_signal["reason"] = f"{final_signal.get('reason', 'signal')} | {alpha_signal['reason']}"
        final_signal["strategy_name"] = alpha_signal.get("strategy_name", "alpha_router_no_trade")

    seconds_to_expiry = max(int((market["end_date"] - now).total_seconds()), 0)
    reference_price, reference_source = reference_price_for_market(market, candles)
    live_dir, distance_pct = live_direction(
        reference_price=reference_price,
        current_price=current_price,
        near_tie_pct=_env_float("PREDICT_REALTIME_NEAR_TIE_PCT", DEFAULT_NEAR_TIE_PCT),
    )
    final_signal.setdefault("meta", {})
    final_signal["meta"].update(
        {
            "realtime": True,
            "market_status": market["status"],
            "reference_price": reference_price,
            "reference_source": reference_source,
            "current_price": current_price,
            "live_direction": live_dir,
            "distance_from_reference_pct": distance_pct,
            "price_source": spot.get("source", "unknown"),
        }
    )
    shadow_summary = record_shadow_profile(
        db,
        market=market,
        candles=candles,
        regime=regime,
        seconds_to_expiry=seconds_to_expiry,
        reference_price=reference_price,
        current_price=current_price,
        live_direction=live_dir,
        distance_from_reference_pct=distance_pct,
        price_source=str(spot.get("source", "unknown")),
    )
    signal_key = build_signal_key(final_signal)
    _store_realtime_state(
        db,
        market=market,
        signal=final_signal,
        signal_key=signal_key,
        regime_label=regime["label"],
        seconds_to_expiry=seconds_to_expiry,
        reference_price=reference_price,
        current_price=current_price,
        live_dir=live_dir,
        distance_pct=distance_pct,
        price_source=str(spot.get("source", "unknown")),
    )

    notified = False
    prediction_id = None
    event_id = None
    if final_signal.get("should_trade"):
        event_id = _insert_realtime_event(
            db,
            market=market,
            signal=final_signal,
            signal_key=signal_key,
            regime_label=regime["label"],
            seconds_to_expiry=seconds_to_expiry,
            reference_price=reference_price,
            current_price=current_price,
            live_dir=live_dir,
            distance_pct=distance_pct,
            price_source=str(spot.get("source", "unknown")),
        )
        if event_id is not None:
            prediction_id = store_prediction(
                db,
                market["id"],
                ACTIVE_AGENT,
                final_signal,
                regime["label"],
                cycle,
                market_price_yes_snapshot=float(market["price_yes"]),
                seconds_to_expiry=seconds_to_expiry,
                prediction_source="realtime_loop",
            )
            db.execute(
                "UPDATE realtime_signal_events SET prediction_id = ? WHERE id = ?",
                (prediction_id, event_id),
            )
            db.commit()
            notified = notify_baseline_trade(
                market_id=market["id"],
                question=market["question"],
                cycle=cycle,
                signal=final_signal,
                regime_label=regime["label"],
                market_price_yes=float(market["price_yes"]),
                seconds_to_expiry=seconds_to_expiry,
            )
            db.execute(
                "UPDATE realtime_signal_events SET notified = ? WHERE id = ?",
                (1 if notified else 0, event_id),
            )
            db.commit()

    return {
        "status": "ok",
        "market_id": market["id"],
        "market_status": market["status"],
        "direction": final_signal.get("direction") or "SKIP",
        "should_trade": bool(final_signal.get("should_trade")),
        "estimate": float(final_signal.get("estimate", 0.5)),
        "regime": regime["label"],
        "seconds_to_expiry": seconds_to_expiry,
        "live_direction": live_dir,
        "distance_from_reference_pct": distance_pct,
        "signal_key": signal_key,
        "event_id": event_id,
        "prediction_id": prediction_id,
        "notified": notified,
        "price_source": spot.get("source", "unknown"),
        "markets_refreshed": refreshed,
        "shadow_profile": shadow_summary["profile_name"],
        "shadow_evaluated": shadow_summary["evaluated"],
        "shadow_inserted": shadow_summary["inserted"],
        "shadow_errors": shadow_summary["errors"],
    }


def run_loop() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    sleep_seconds = _env_int("PREDICT_REALTIME_SLEEP_SECONDS", DEFAULT_SLEEP_SECONDS)
    btc_refresh_seconds = _env_int(
        "PREDICT_REALTIME_BTC_REFRESH_SECONDS",
        DEFAULT_BTC_CONTEXT_REFRESH_SECONDS,
    )
    db = init_db()
    btc_context = None
    btc_loaded_at = 0.0
    cycle = 0
    print(
        f"[realtime] starting rolling poll loop sleep={sleep_seconds}s "
        f"btc_refresh={btc_refresh_seconds}s db={DB_PATH}"
    )
    try:
        while True:
            cycle += 1
            now_monotonic = time.monotonic()
            if btc_context is None or now_monotonic - btc_loaded_at >= btc_refresh_seconds:
                btc_context = fetch_btc_candles(limit=20)
                btc_loaded_at = now_monotonic
            try:
                result = run_realtime_once(db, cycle=cycle, btc_context=btc_context)
                if result["status"] == "ok":
                    print(
                        "[realtime] "
                        f"cycle={cycle} market={result['market_id']} "
                        f"{result['market_status']} {result['direction']} "
                        f"trade={int(result['should_trade'])} "
                        f"live={result['live_direction']} "
                        f"tte={result['seconds_to_expiry']}s "
                        f"regime={result['regime']} "
                        f"event={result['event_id']} notified={int(result['notified'])} "
                        f"shadow={result.get('shadow_inserted', 0)}/{result.get('shadow_evaluated', 0)}"
                    )
                else:
                    print(f"[realtime] cycle={cycle} status={result['status']} result={result}")
            except Exception as exc:
                print(f"[realtime] cycle={cycle} error={exc}")
            time.sleep(sleep_seconds)
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="Run one realtime iteration and exit")
    args = parser.parse_args()
    if args.once:
        db = init_db()
        try:
            result = run_realtime_once(db, cycle=int(time.time()))
            print(result)
        finally:
            db.close()
    else:
        run_loop()


if __name__ == "__main__":
    main()
