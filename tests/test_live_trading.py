"""
Unit tests for live trading helpers.
"""
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fetch_markets import _parse_clob_token_ids
from live_trading import LiveTradingConfig, bet_amount_for_prediction, build_trade_plan


def _config():
    return LiveTradingConfig(
        enabled=True,
        dry_run=False,
        host="https://clob.polymarket.com",
        chain_id=137,
        signature_type=0,
        private_key="0xabc",
        funder="0xdef",
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        order_type="FAK",
        min_edge=0.0,
        min_seconds_to_expiry=45,
        medium_bet_usd=75.0,
        high_bet_usd=200.0,
    )


def test_parse_clob_token_ids_from_json_string():
    token_yes, token_no = _parse_clob_token_ids('["123","456"]')
    assert token_yes == "123"
    assert token_no == "456"


def test_high_confidence_uses_high_bet_size():
    row = {"confidence": "high", "conviction_score": 3}
    assert bet_amount_for_prediction(row, _config()) == 200.0


def test_build_trade_plan_for_up_signal():
    now = datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc)
    row = {
        "prediction_id": 1,
        "market_id": "m1",
        "question": "BTC up?",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "price_yes": 0.52,
        "price_no": 0.48,
        "token_yes": "yes-token",
        "token_no": "no-token",
        "end_date": (now + timedelta(minutes=5)).isoformat(),
    }

    plan, reason = build_trade_plan(row, _config(), now=now)
    assert reason is None
    assert plan["direction"] == "UP"
    assert plan["token_id"] == "yes-token"
    assert abs(plan["expected_edge"] - 0.10) < 1e-9
    assert plan["bet_amount_usd"] == 75.0


def test_build_trade_plan_respects_min_edge():
    now = datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc)
    row = {
        "prediction_id": 2,
        "market_id": "m2",
        "question": "BTC up?",
        "estimate": 0.62,
        "confidence": "medium",
        "conviction_score": 3,
        "price_yes": 0.60,
        "price_no": 0.40,
        "token_yes": "yes-token",
        "token_no": "no-token",
        "end_date": (now + timedelta(minutes=5)).isoformat(),
    }

    config = replace(_config(), min_edge=0.05)
    plan, reason = build_trade_plan(row, config, now=now)
    assert plan is None
    assert reason == "edge_below_threshold"


def test_build_trade_plan_skips_if_too_close_to_expiry():
    now = datetime(2026, 3, 25, 20, 0, tzinfo=timezone.utc)
    row = {
        "prediction_id": 3,
        "market_id": "m3",
        "question": "BTC down?",
        "estimate": 0.38,
        "confidence": "medium",
        "conviction_score": 3,
        "price_yes": 0.58,
        "price_no": 0.42,
        "token_yes": "yes-token",
        "token_no": "no-token",
        "end_date": (now + timedelta(seconds=30)).isoformat(),
    }

    plan, reason = build_trade_plan(row, _config(), now=now)
    assert plan is None
    assert reason == "too_close_to_expiry"
