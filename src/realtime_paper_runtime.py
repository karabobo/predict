"""
realtime_paper_runtime.py - helpers for the v8 WSS paper runtime.

This file keeps timing and fill decisions separate from network clients so the
runtime can be tested deterministically.
"""

from __future__ import annotations

import os
import sqlite3
from types import SimpleNamespace
from datetime import datetime, timezone
from typing import Any

from btc_data import fetch_btc_candles, fetch_btc_spot_price
from decision_audit import ensure_decision_audit_schema, log_decision_audit, log_rule_evaluations
from fetch_markets import fetch_active_markets, store_markets
from live_prior import evaluate_prior_gate
from paper_trading import PaperTradingConfig, ensure_paper_schema, execute_book_paper_order, load_paper_trading_config
from realtime_wss import LiveBookStore
from predict import ACTIVE_AGENT, alpha_router_signal, compute_regime_from_candles, ensure_db_schema, store_prediction
from realtime_signal import (
    apply_realtime_price_to_candles,
    ensure_realtime_schema,
    reference_price_for_market,
    select_realtime_market,
)

ENTRY_START_OFFSET_SECONDS = 0
PRIOR_SCOUT_MIN_EDGE = 0.01


def market_entry_offset_seconds(market: dict[str, Any], *, now: datetime | None = None) -> int:
    current = now or datetime.now(timezone.utc)
    start = market["start_date"]
    return max(int((current - start).total_seconds()), 0)


def selected_entry_slot(
    offset_seconds: int,
    *,
    start: int = ENTRY_START_OFFSET_SECONDS,
) -> int | None:
    if offset_seconds >= start:
        return offset_seconds
    return None


def execute_v8_book_paper_order(
    db: sqlite3.Connection,
    *,
    market: dict[str, Any],
    signal: dict[str, Any],
    book_store: LiveBookStore,
    config: PaperTradingConfig,
    now: datetime | None = None,
    prior_prob: float | None = None,
    prior_edge: float | None = None,
    prediction_id: int | None = None,
    rule_profile: str | None = None,
) -> dict[str, Any]:
    rule_profile = rule_profile or realtime_rule_profile()
    config = config or load_paper_trading_config()
    offset = market_entry_offset_seconds(market, now=now)
    slot = selected_entry_slot(offset)
    if slot is None:
        return {"market_id": market["id"], "status": "outside_entry_window", "entry_offset_seconds": offset}
    if market.get("end_date") is not None:
        current = now or datetime.now(timezone.utc)
        seconds_to_expiry = max(int((market["end_date"] - current).total_seconds()), 0)
        if seconds_to_expiry < config.min_seconds_to_expiry:
            return {
                "market_id": market["id"],
                "status": "skipped_too_close_to_expiry",
                "entry_offset_seconds": offset,
                "seconds_to_expiry": seconds_to_expiry,
            }
    yes = book_store.get(market.get("token_yes"))
    no = book_store.get(market.get("token_no"))
    result = execute_book_paper_order(
        db,
        market_id=market["id"],
        question=market.get("question", ""),
        token_yes=market.get("token_yes"),
        token_no=market.get("token_no"),
        signal=signal,
        yes_book=yes.book if yes else None,
        no_book=no.book if no else None,
        config=config,
        prediction_id=prediction_id,
        rule_profile=rule_profile,
        entry_offset_seconds=slot,
        prior_prob=prior_prob,
        prior_edge=prior_edge,
    )
    result.setdefault("entry_offset_seconds", slot)
    return result


def run_v8_realtime_paper_once(
    db: sqlite3.Connection,
    *,
    book_store: LiveBookStore,
    btc_ticker: dict[str, Any] | None = None,
    btc_context: dict[str, Any] | None = None,
    prior_model: Any | None = None,
    now: datetime | None = None,
    config: PaperTradingConfig | None = None,
    refresh_markets: bool = True,
    cycle: int = 0,
) -> dict[str, Any]:
    ensure_db_schema(db)
    ensure_realtime_schema(db)
    ensure_paper_schema(db)
    ensure_decision_audit_schema(db)
    if refresh_markets:
        store_markets(db, fetch_active_markets())
    current = now or datetime.now(timezone.utc)
    market = select_realtime_market(db, now=current)
    if market is None:
        return {"status": "no_market"}
    config = config or load_paper_trading_config()
    offset = market_entry_offset_seconds(market, now=current)
    slot = selected_entry_slot(offset)
    if slot is None:
        return {
            "status": "outside_entry_window",
            "market_id": market["id"],
            "entry_offset_seconds": offset,
            "rule_profile": realtime_rule_profile(),
        }
    seconds_to_expiry = max(int((market["end_date"] - current).total_seconds()), 0)
    if seconds_to_expiry < config.min_seconds_to_expiry:
        return {
            "status": "skipped_too_close_to_expiry",
            "market_id": market["id"],
            "entry_offset_seconds": offset,
            "seconds_to_expiry": seconds_to_expiry,
            "rule_profile": realtime_rule_profile(),
        }

    btc_context = btc_context or fetch_btc_candles(limit=20)
    if not btc_context or not btc_context.get("candles"):
        return {"status": "no_btc_context", "market_id": market["id"]}
    ticker = btc_ticker or fetch_btc_spot_price()
    current_price = float(ticker["price"])
    candles = apply_realtime_price_to_candles(btc_context["candles"], current_price)
    regime = compute_regime_from_candles(candles)
    from v3.rule_registry import resolve_profile_rules, run_rule_profile

    rule_profile = realtime_rule_profile()
    profile_result = run_rule_profile(candles, regime, rule_profile)
    log_rule_evaluations(
        db,
        market_id=market["id"],
        cycle=cycle,
        rule_profile=rule_profile,
        entry_offset_seconds=slot,
        evaluations=profile_result.evaluations,
    )
    signal = alpha_router_signal(candles, regime, rule_names=resolve_profile_rules(rule_profile))
    prior_result = None
    if not signal.get("should_trade"):
        prior_scout = _prior_scout_signal(
            prior_model=prior_model,
            candles=candles,
            regime=regime,
            rule_profile=rule_profile,
        )
        if prior_scout is None:
            log_decision_audit(
                db,
                market_id=market["id"],
                cycle=cycle,
                rule_profile=rule_profile,
                entry_offset_seconds=slot,
                stage="final",
                signal=signal,
                status="no_trade_signal",
            )
            return {
                "status": "no_trade_signal",
                "market_id": market["id"],
                "entry_offset_seconds": slot,
                "rule_profile": rule_profile,
            }
        signal = prior_scout["signal"]
        prior_result = prior_scout["prior_result"]
        log_decision_audit(
            db,
            market_id=market["id"],
            cycle=cycle,
            rule_profile=rule_profile,
            entry_offset_seconds=slot,
            stage="prior",
            rule_name="prior_probability_scout",
            signal=signal,
            prior_prob=prior_result.prob_up,
            prior_direction=prior_result.direction,
            prior_edge=prior_result.edge,
            passed=prior_result.passed,
            status=prior_result.reason,
        )

    if prior_model is not None and prior_result is None:
        prior_result = evaluate_prior_gate(
            model=prior_model,
            context=SimpleNamespace(formatted_candles=candles, production_regime=regime, market={}),
            predicted_direction=signal.get("direction"),
            min_edge=0.01,
        )
        log_decision_audit(
            db,
            market_id=market["id"],
            cycle=cycle,
            rule_profile=rule_profile,
            entry_offset_seconds=slot,
            stage="prior",
            rule_name=signal.get("strategy_name"),
            signal=signal,
            prior_prob=prior_result.prob_up,
            prior_direction=prior_result.direction,
            prior_edge=prior_result.edge,
            passed=prior_result.passed,
            status=prior_result.reason,
        )
        if not prior_result.passed:
            log_decision_audit(
                db,
                market_id=market["id"],
                cycle=cycle,
                rule_profile=rule_profile,
                entry_offset_seconds=slot,
                stage="final",
                rule_name=signal.get("strategy_name"),
                signal=signal,
                prior_prob=prior_result.prob_up,
                prior_direction=prior_result.direction,
                prior_edge=prior_result.edge,
                passed=False,
                status=f"skipped_{prior_result.reason}",
            )
            return {
                "status": f"skipped_{prior_result.reason}",
                "market_id": market["id"],
                "entry_offset_seconds": slot,
                "rule_profile": rule_profile,
                "prior_prob": prior_result.prob_up,
                "prior_edge": prior_result.edge,
            }

    reference_price, _ = reference_price_for_market(market, candles)
    prediction_id = store_prediction(
        db,
        market["id"],
        ACTIVE_AGENT,
        signal,
        regime["label"],
        cycle,
        market_price_yes_snapshot=float(market["price_yes"]),
        seconds_to_expiry=max(int((market["end_date"] - current).total_seconds()), 0),
        prediction_source="v8_wss_paper_runtime",
    )
    result = execute_v8_book_paper_order(
        db,
        market=market,
        signal=signal,
        book_store=book_store,
        config=config,
        now=current,
        prior_prob=prior_result.prob_up if prior_result else None,
        prior_edge=prior_result.edge if prior_result else None,
        prediction_id=prediction_id,
        rule_profile=rule_profile,
    )
    result.update(
        {
            "reference_price": reference_price,
            "current_price": current_price,
            "regime": regime["label"],
            "prediction_id": prediction_id,
            "rule_profile": rule_profile,
        }
    )
    _log_book_decision(
        db,
        market=market,
        book_store=book_store,
        signal=signal,
        cycle=cycle,
        rule_profile=rule_profile,
        entry_offset_seconds=slot,
        prior_result=prior_result,
        status=str(result.get("status") or ""),
    )
    log_decision_audit(
        db,
        market_id=market["id"],
        cycle=cycle,
        rule_profile=rule_profile,
        entry_offset_seconds=slot,
        stage="final",
        rule_name=signal.get("strategy_name"),
        signal=signal,
        prior_prob=prior_result.prob_up if prior_result else None,
        prior_direction=prior_result.direction if prior_result else None,
        prior_edge=prior_result.edge if prior_result else None,
        passed=result.get("status") == "paper_filled",
        status=str(result.get("status") or ""),
        payload={
            "prediction_id": prediction_id,
            "reference_price": reference_price,
            "current_price": current_price,
            "average_price": result.get("average_price"),
            "expected_edge": result.get("expected_edge"),
        },
    )
    return result


def realtime_rule_profile() -> str:
    return os.getenv("PREDICT_RULE_PROFILE", "v8_integrated_candidate").strip() or "v8_integrated_candidate"


def prior_scout_enabled() -> bool:
    value = os.getenv("PREDICT_PRIOR_SCOUT_ENABLED", "1")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _prior_scout_signal(
    *,
    prior_model: Any | None,
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    rule_profile: str,
) -> dict[str, Any] | None:
    if not prior_scout_enabled() or prior_model is None or rule_profile != "v8_broad_paper_candidate":
        return None
    result = evaluate_prior_gate(
        model=prior_model,
        context=SimpleNamespace(formatted_candles=candles, production_regime=regime, market={}),
        predicted_direction="UP",
        min_edge=PRIOR_SCOUT_MIN_EDGE,
    )
    prob_up = result.prob_up
    if prob_up is None:
        return None
    direction = "UP" if prob_up > 0.5 else "DOWN" if prob_up < 0.5 else None
    edge = abs(prob_up - 0.5)
    if direction is None or edge < PRIOR_SCOUT_MIN_EDGE:
        return None
    scout_result = evaluate_prior_gate(
        model=prior_model,
        context=SimpleNamespace(formatted_candles=candles, production_regime=regime, market={}),
        predicted_direction=direction,
        min_edge=PRIOR_SCOUT_MIN_EDGE,
    )
    conviction = 4 if edge >= 0.04 else 3
    signal = {
        "estimate": prob_up,
        "should_trade": True,
        "direction": direction,
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": f"prior_probability_scout | prob_up={prob_up:.3f} | edge={edge:.3f}",
        "regime_label": regime["label"],
        "strategy_name": "prior_probability_scout",
    }
    return {"signal": signal, "prior_result": scout_result}


def _log_book_decision(
    db: sqlite3.Connection,
    *,
    market: dict[str, Any],
    book_store: LiveBookStore,
    signal: dict[str, Any],
    cycle: int,
    rule_profile: str,
    entry_offset_seconds: int,
    prior_result: Any | None,
    status: str,
) -> None:
    direction = str(signal.get("direction") or "").upper()
    snapshot = None
    if direction == "DOWN" and market.get("token_no"):
        snapshot = book_store.get(market.get("token_no"))
    if snapshot is None:
        snapshot = book_store.get(market.get("token_yes"))
    metrics = snapshot.metrics if snapshot is not None else None
    log_decision_audit(
        db,
        market_id=market["id"],
        cycle=cycle,
        rule_profile=rule_profile,
        entry_offset_seconds=entry_offset_seconds,
        stage="book",
        rule_name=signal.get("strategy_name"),
        signal=signal,
        prior_prob=prior_result.prob_up if prior_result else None,
        prior_direction=prior_result.direction if prior_result else None,
        prior_edge=prior_result.edge if prior_result else None,
        book_ready=snapshot is not None,
        best_bid=metrics.best_bid if metrics else None,
        best_ask=metrics.best_ask if metrics else None,
        spread=metrics.spread if metrics else None,
        status=status,
    )
