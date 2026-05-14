"""
notifier.py — Lightweight Telegram notifications for production and research.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args, **_kwargs):
        return False


ENV_PATH = Path(__file__).parent.parent / ".env"
TELEGRAM_TIMEOUT_SECONDS = 10


def _load_env() -> None:
    load_dotenv(ENV_PATH)


def _env(name: str) -> str | None:
    value = os.getenv(name)
    return value or None


def _env_bool(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def telegram_configured() -> bool:
    _load_env()
    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    enabled = _env_bool("TELEGRAM_NOTIFICATIONS_ENABLED", True)
    return bool(enabled and token and chat_id)


def send_telegram_message(text: str) -> bool:
    _load_env()
    if not telegram_configured():
        return False

    token = _env("TELEGRAM_BOT_TOKEN")
    chat_id = _env("TELEGRAM_CHAT_ID")
    prefix = _env("TELEGRAM_NOTIFY_PREFIX") or "predict"

    payload = {
        "chat_id": chat_id,
        "text": f"[{prefix}]\n{text}",
        "disable_web_page_preview": True,
    }
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    try:
        response = requests.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        response.raise_for_status()
        return True
    except requests.RequestException as exc:
        print(f"Telegram notify failed: {exc}")
        return False


def notify_baseline_trade(
    *,
    market_id: str,
    question: str,
    cycle: int,
    signal: dict[str, Any],
    regime_label: str,
    market_price_yes: float,
    seconds_to_expiry: int | None = None,
) -> bool:
    if not signal.get("should_trade"):
        return False

    estimate = float(signal.get("estimate", 0.5))
    direction = signal.get("direction") or ("UP" if estimate >= 0.5 else "DOWN")
    confidence = str(signal.get("confidence", "low"))
    conviction = int(signal.get("conviction_score", 0))
    reason = str(signal.get("reason", "")).strip()

    message = "\n".join(
        [
            "Baseline trade signal",
            f"cycle: {cycle}",
            f"market_id: {market_id}",
            f"question: {question}",
            f"direction: {direction}",
            f"estimate: {estimate:.2%}",
            f"market_yes: {market_price_yes:.2%}",
            f"seconds_to_expiry: {seconds_to_expiry if seconds_to_expiry is not None else 'n/a'}",
            f"confidence: {confidence}",
            f"conviction: {conviction}",
            f"regime: {regime_label}",
            f"reason: {reason or 'n/a'}",
        ]
    )
    return send_telegram_message(message)


def notify_deepseek_promotion(
    *,
    run_id: str,
    baseline: str,
    challenger: str,
    results: dict[str, Any],
    report_path: Path,
) -> bool:
    gate = results.get("gate", {})
    if challenger != "deepseek_v3" or not gate.get("passed"):
        return False

    fold_bits = []
    for fold in gate.get("fold_checks", []):
        fold_bits.append(
            f"fold {fold['fold_index']}: "
            f"ROI {fold['roi_delta']:+.2f}pp, "
            f"WR {fold['win_rate_delta']:+.2f}pp, "
            f"trade_ratio {fold['trade_ratio']:.2f}"
        )

    message = "\n".join(
        [
            "DeepSeek promotion PASSED",
            f"run_id: {run_id}",
            f"baseline: {baseline}",
            f"challenger: {challenger}",
            f"aggregate_roi_delta: {gate.get('aggregate_roi_delta', 0.0):+.2f}pp",
            f"aggregate_wr_delta: {gate.get('aggregate_win_rate_delta', 0.0):+.2f}pp",
            f"passing_folds: {gate.get('passing_folds', 0)}/{len(gate.get('fold_checks', []))}",
            f"trade_ratio: {gate.get('trade_ratio', 0.0):.2f}",
            f"drawdown_ratio: {gate.get('drawdown_ratio', 0.0):.2f}",
            "details:",
            *(fold_bits or ["no fold details"]),
            f"report: {report_path}",
        ]
    )
    return send_telegram_message(message)
