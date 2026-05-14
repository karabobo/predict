"""
live_trading.py — Real order placement on Polymarket via the official Python SDK.

This module is intentionally conservative:
- disabled by default
- requires explicit env configuration
- only places BUY market orders on the predicted outcome token
- logs every terminal decision to SQLite to avoid duplicate orders
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional during lightweight import checks
    def load_dotenv(*_args, **_kwargs):
        return False

DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
DEFAULT_HOST = "https://clob.polymarket.com"
DEFAULT_CHAIN_ID = 137
RETRYABLE_STATUSES = {"failed"}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class LiveTradingConfig:
    enabled: bool
    dry_run: bool
    host: str
    chain_id: int
    signature_type: int
    private_key: str | None
    funder: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    order_type: str
    min_edge: float
    min_seconds_to_expiry: int
    medium_bet_usd: float
    high_bet_usd: float


def load_live_trading_config() -> LiveTradingConfig:
    load_dotenv(Path(__file__).parent.parent / ".env")

    return LiveTradingConfig(
        enabled=_env_bool("POLYMARKET_LIVE_TRADING", False),
        dry_run=_env_bool("POLYMARKET_DRY_RUN", False),
        host=os.getenv("POLYMARKET_HOST", DEFAULT_HOST),
        chain_id=int(os.getenv("POLYMARKET_CHAIN_ID", str(DEFAULT_CHAIN_ID))),
        signature_type=int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0")),
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        funder=os.getenv("POLYMARKET_FUNDER_ADDRESS"),
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
        order_type=os.getenv("POLYMARKET_ORDER_TYPE", "FAK").upper(),
        min_edge=float(os.getenv("POLYMARKET_MIN_LIVE_EDGE", "0.0")),
        min_seconds_to_expiry=int(os.getenv("POLYMARKET_MIN_SECONDS_TO_EXPIRY", "45")),
        medium_bet_usd=float(os.getenv("POLYMARKET_MEDIUM_BET_USD", "1")),
        high_bet_usd=float(os.getenv("POLYMARKET_HIGH_BET_USD", "1")),
    )


def bet_amount_for_prediction(row: dict[str, Any], config: LiveTradingConfig) -> float:
    """Map confidence / conviction to live dollar size."""
    conviction = int(row.get("conviction_score") or 0)
    confidence = (row.get("confidence") or "").lower()

    if conviction >= 4 or confidence == "high":
        return config.high_bet_usd
    if conviction >= 3 or confidence == "medium":
        return config.medium_bet_usd
    return 0.0


def build_trade_plan(
    row: dict[str, Any],
    config: LiveTradingConfig,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Turn a stored prediction into a concrete order plan."""
    current_time = now or datetime.now(timezone.utc)
    amount_usd = bet_amount_for_prediction(row, config)
    if amount_usd <= 0:
        return None, "no_bet_size"

    estimate = float(row["estimate"])
    if estimate >= 0.5:
        direction = "UP"
        token_id = row.get("token_yes")
        market_price = float(row["price_yes"])
        predicted_prob = estimate
    else:
        direction = "DOWN"
        token_id = row.get("token_no")
        market_price = float(row["price_no"])
        predicted_prob = 1.0 - estimate

    if not token_id:
        return None, "missing_token_id"
    if not (0 < market_price < 1):
        return None, "invalid_market_price"

    expected_edge = predicted_prob - market_price
    if expected_edge < config.min_edge:
        return None, "edge_below_threshold"

    seconds_to_expiry = int((_parse_timestamp(row["end_date"]) - current_time).total_seconds())
    if seconds_to_expiry <= config.min_seconds_to_expiry:
        return None, "too_close_to_expiry"

    return {
        "prediction_id": row["prediction_id"],
        "market_id": row["market_id"],
        "question": row["question"],
        "direction": direction,
        "token_id": token_id,
        "side": "BUY",
        "confidence": row.get("confidence"),
        "conviction_score": row.get("conviction_score"),
        "bet_amount_usd": amount_usd,
        "predicted_prob": predicted_prob,
        "market_price": market_price,
        "expected_edge": expected_edge,
        "seconds_to_expiry": seconds_to_expiry,
    }, None


def _order_type_value(config: LiveTradingConfig):
    from py_clob_client.clob_types import OrderType

    if config.order_type not in {OrderType.FAK, OrderType.FOK}:
        raise ValueError("POLYMARKET_ORDER_TYPE must be FAK or FOK for market orders")
    return config.order_type


def _build_client(config: LiveTradingConfig):
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    if not config.private_key:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required when live trading is enabled")

    temp_client = ClobClient(config.host, chain_id=config.chain_id, key=config.private_key)
    funder = config.funder
    if not funder and config.signature_type == 0:
        funder = temp_client.get_address()

    if not funder:
        raise RuntimeError("POLYMARKET_FUNDER_ADDRESS is required for proxy wallet signature types")

    if config.api_key and config.api_secret and config.api_passphrase:
        creds = ApiCreds(
            api_key=config.api_key,
            api_secret=config.api_secret,
            api_passphrase=config.api_passphrase,
        )
    else:
        creds = temp_client.create_or_derive_api_creds()

    if not creds:
        raise RuntimeError("Unable to derive Polymarket API credentials")

    return ClobClient(
        host=config.host,
        chain_id=config.chain_id,
        key=config.private_key,
        creds=creds,
        signature_type=config.signature_type,
        funder=funder,
    )


def _refresh_collateral_cache(client, config: LiveTradingConfig) -> None:
    """Refresh CLOB-side balance / allowance cache for the collateral wallet."""
    from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

    params = BalanceAllowanceParams(
        asset_type=AssetType.COLLATERAL,
        signature_type=config.signature_type,
    )

    try:
        client.update_balance_allowance(params)
        info = client.get_balance_allowance(params)
        print(f"  Collateral cache refreshed: {info}")
    except Exception as exc:
        print(f"  Collateral cache refresh skipped: {exc}")


def _pending_live_predictions(db: sqlite3.Connection) -> list[dict[str, Any]]:
    now_iso = datetime.now(timezone.utc).isoformat()
    cursor = db.execute(
        """
        SELECT
            p.id AS prediction_id,
            p.market_id,
            p.agent,
            p.estimate,
            p.confidence,
            p.predicted_at,
            p.cycle,
            p.conviction_score,
            m.question,
            m.end_date,
            m.price_yes,
            m.price_no,
            m.condition_id,
            m.token_yes,
            m.token_no
        FROM predictions p
        JOIN markets m ON m.id = p.market_id
        WHERE m.resolved = 0
          AND m.end_date > ?
          AND COALESCE(p.conviction_score, 0) >= 3
          AND NOT EXISTS (
              SELECT 1
              FROM live_orders lo
              WHERE lo.prediction_id = p.id
                AND lo.status NOT IN ('failed')
          )
        ORDER BY m.end_date ASC, p.predicted_at ASC
        """,
        (now_iso,),
    )
    columns = [col[0] for col in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _log_live_order(
    db: sqlite3.Connection,
    row: dict[str, Any],
    plan: dict[str, Any] | None,
    *,
    status: str,
    success: bool,
    order_type: str | None,
    response: Any = None,
    error_text: str | None = None,
    dry_run: bool = False,
    order_id: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO live_orders (
            prediction_id, market_id, token_id, direction, side, confidence,
            conviction_score, order_type, bet_amount_usd, predicted_prob,
            market_price, expected_edge, status, order_id, success, dry_run,
            response_json, error_text, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row["prediction_id"],
            row["market_id"],
            plan.get("token_id") if plan else None,
            plan.get("direction") if plan else None,
            plan.get("side") if plan else None,
            row.get("confidence"),
            row.get("conviction_score"),
            order_type,
            plan.get("bet_amount_usd") if plan else None,
            plan.get("predicted_prob") if plan else None,
            plan.get("market_price") if plan else None,
            plan.get("expected_edge") if plan else None,
            status,
            order_id,
            1 if success else 0,
            1 if dry_run else 0,
            json.dumps(response) if response is not None else None,
            error_text,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()


def _status_from_exception(exc: Exception) -> str:
    message = str(exc).lower()
    if "allowance" in message:
        return "failed_allowance"
    if "balance" in message:
        return "failed_balance"
    if "signature" in message or "l2 auth" in message or "invalid creds" in message:
        return "failed_auth"
    if "cloudflare" in message or "geoblock" in message or "restricted region" in message:
        return "failed_blocked"
    return "failed"


def execute_live_orders(db: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """Place live orders for newly-created predictions."""
    config = load_live_trading_config()
    if not config.enabled:
        print("  Live trading disabled")
        return []

    close_db = False
    if db is None:
        db = sqlite3.connect(DB_PATH)
        close_db = True

    try:
        pending = _pending_live_predictions(db)
        if not pending:
            print("  No pending live orders")
            return []

        client = _build_client(config)
        if not config.dry_run:
            _refresh_collateral_cache(client, config)

        from py_clob_client.clob_types import MarketOrderArgs

        order_type = _order_type_value(config)
        results = []

        for row in pending:
            plan, reason = build_trade_plan(row, config)
            if reason:
                _log_live_order(
                    db,
                    row,
                    None,
                    status=f"skipped_{reason}",
                    success=False,
                    order_type=order_type,
                )
                print(f"  Skip live order for {row['market_id']}: {reason}")
                continue

            if config.dry_run:
                _log_live_order(
                    db,
                    row,
                    plan,
                    status="dry_run",
                    success=False,
                    order_type=order_type,
                    dry_run=True,
                )
                print(
                    f"  DRY RUN: BUY {plan['direction']} ${plan['bet_amount_usd']:.2f} "
                    f"edge={plan['expected_edge']:+.3f} market={plan['market_price']:.3f}"
                )
                results.append({"market_id": row["market_id"], "status": "dry_run"})
                continue

            try:
                order_args = MarketOrderArgs(
                    token_id=plan["token_id"],
                    amount=plan["bet_amount_usd"],
                    side="BUY",
                    order_type=order_type,
                )
                order = client.create_market_order(order_args)
                response = client.post_order(order, order_type)
                status = response.get("status", "submitted")
                order_id = response.get("orderID") or response.get("orderId")

                _log_live_order(
                    db,
                    row,
                    plan,
                    status=status,
                    success=True,
                    order_type=order_type,
                    response=response,
                    order_id=order_id,
                )
                print(
                    f"  LIVE ORDER: BUY {plan['direction']} ${plan['bet_amount_usd']:.2f} "
                    f"@<={order_args.price:.3f} status={status} order_id={order_id}"
                )
                results.append({"market_id": row["market_id"], "status": status, "order_id": order_id})
            except Exception as exc:
                status = _status_from_exception(exc)
                _log_live_order(
                    db,
                    row,
                    plan,
                    status=status,
                    success=False,
                    order_type=order_type,
                    error_text=str(exc),
                )
                print(f"  Live order failed for {row['market_id']}: {exc}")
                results.append({"market_id": row["market_id"], "status": status, "error": str(exc)})

        return results
    finally:
        if close_db:
            db.close()


if __name__ == "__main__":
    db = sqlite3.connect(DB_PATH)
    execute_live_orders(db)
    db.close()
