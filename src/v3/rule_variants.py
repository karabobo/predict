"""
Deterministic baseline variants for historical Polymarket backtests.

These variants are intentionally narrow and trace back to current coach findings.
They are research-only until sample-out validation says otherwise.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable

try:
    from src.strategies.momentum import (
        _direction,
        _has_shrinking_ranges,
        _has_volume_spike,
        _streak_length,
        conviction_from_signal_features,
        contrarian_signal,
        estimate_from_signal_features,
    )
except ModuleNotFoundError:  # pragma: no cover - direct `python src/dashboard.py`
    from strategies.momentum import (  # type: ignore[no-redef]
        _direction,
        _has_shrinking_ranges,
        _has_volume_spike,
        _streak_length,
        conviction_from_signal_features,
        contrarian_signal,
        estimate_from_signal_features,
    )


RuleFn = Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]]
RESEARCH_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "v3_research.db"
FOUNDATION_RULE_TAGS = {
    "low_vol_branch_v1",
    "clean_continuation_up",
    "clean_continuation_down",
    "medium_neutral_down_continuation",
    "medium_neutral_down_continuation_balanced",
    "medium_neutral_down_continuation_core",
    "flush_bounce_up",
    "spike_reversal_down",
    "spike_reversal_down_no_hvt",
}


def available_rules() -> dict[str, RuleFn]:
    rules = {
        "baseline_current": baseline_current,
        "baseline_v2_lvn_alpha2": baseline_v2_lvn_alpha2,
        "baseline_v2_lvn_alpha3": baseline_v2_lvn_alpha3,
        "spike_reversal_down": spike_reversal_down,
        "spike_reversal_down_no_hvt": spike_reversal_down_no_hvt,
        "spike_reversal_down_window10": spike_reversal_down_window10,
        "spike_reversal_down_window6": spike_reversal_down_window6,
        "flush_bounce_up": flush_bounce_up,
        "flush_bounce_up_window10": flush_bounce_up_window10,
        "flush_bounce_up_window6": flush_bounce_up_window6,
        "baseline_v3_spike_reversal": baseline_v3_spike_reversal,
        "baseline_v3_momentum_shape": baseline_v3_momentum_shape,
        "baseline_v3_reversal_core": baseline_v3_reversal_core,
        "baseline_v4_window_state": baseline_v4_window_state,
        "baseline_v4_window_state_w6": baseline_v4_window_state_w6,
        "clean_continuation_up": clean_continuation_up,
        "clean_continuation_down": clean_continuation_down,
        "baseline_v5_clean_continuation": baseline_v5_clean_continuation,
        "medium_neutral_down_continuation": medium_neutral_down_continuation,
        "medium_neutral_down_continuation_balanced": medium_neutral_down_continuation_balanced,
        "medium_neutral_down_continuation_core": medium_neutral_down_continuation_core,
        "baseline_v6_broad_shape": baseline_v6_broad_shape,
        "low_vol_branch_v1": low_vol_branch_v1,
        "medium_vol_branch_v1": medium_vol_branch_v1,
        "medium_vol_branch_v2": medium_vol_branch_v2,
        "medium_vol_branch_v3": medium_vol_branch_v3,
        "high_vol_branch_v1": high_vol_branch_v1,
        "baseline_router_v1": baseline_router_v1,
        "baseline_router_v2": baseline_router_v2,
        "baseline_router_v2_candidate_filter": baseline_router_v2_candidate_filter,
        "baseline_router_v1_plus_lvn_alpha3": baseline_router_v1_plus_lvn_alpha3,
        "baseline_router_v1_plus_v4": baseline_router_v1_plus_v4,
        "baseline_router_v1_plus_sparse_combo": baseline_router_v1_plus_sparse_combo,
        "only_low_vol_trending": only_low_vol_trending,
        "only_low_vol_neutral": only_low_vol_neutral,
        "only_lvn_up": only_lvn_up,
        "only_lvn_up_compression": only_lvn_up_compression,
        "only_lvn_volume_spike": only_lvn_volume_spike,
        "only_lvn_up_volume_spike": only_lvn_up_volume_spike,
        "only_lvn_up_volume_spike_streak4p": only_lvn_up_volume_spike_streak4p,
        "only_lvn_up_pure_volume_spike": only_lvn_up_pure_volume_spike,
        "candidate_lvn_up_volume_spike": only_lvn_up_volume_spike,
        "candidate_lvn_up_volume_spike_streak4p": only_lvn_up_volume_spike_streak4p,
        "loosen_streak_low_vol_trending": loosen_streak_low_vol_trending,
        "allow_trending_volume_spike": allow_trending_volume_spike,
        "block_high_vol_neutral": block_high_vol_neutral,
        "block_high_vol_trending": block_high_vol_trending,
        "block_medium_vol_trending": block_medium_vol_trending,
        "only_low_vol": only_low_vol,
        "raise_conviction_low_vol_neutral": raise_conviction_low_vol_neutral,
        "combo_lvt_relaxed": combo_lvt_relaxed,
    }
    rules.update(load_dynamic_coach_rule_drafts())
    return rules


def load_dynamic_coach_rule_drafts(db_path: Path = RESEARCH_DB_PATH) -> dict[str, RuleFn]:
    metadata = load_dynamic_coach_rule_metadata(db_path)
    rules: dict[str, RuleFn] = {}
    for rule_name, spec in metadata.items():
        fn = _build_rule_from_spec(spec)
        if fn is not None:
            rules[rule_name] = fn
    return rules


def load_dynamic_coach_rule_metadata(db_path: Path = RESEARCH_DB_PATH) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "coach_rule_candidate_specs" not in tables:
            return {}
        rows = db.execute(
            """
            SELECT spec_name, spec_label, family, target_scope, template_action, config_json
            FROM coach_rule_candidate_specs
            WHERE eligible_for_ablation = 1
            ORDER BY net_helpful DESC, support_count DESC, spec_name ASC
            """
        ).fetchall()
        metadata: dict[str, dict[str, Any]] = {}
        for row in rows:
            rule_name = f"coach_spec__{row['spec_name']}"
            spec = {
                "rule_name": rule_name,
                "spec_name": str(row["spec_name"]),
                "spec_label": str(row["spec_label"]),
                "family": str(row["family"]),
                "target_scope": str(row["target_scope"]),
                "template_action": str(row["template_action"]),
                "config": json.loads(str(row["config_json"] or "{}")),
            }
            metadata[rule_name] = spec
        return metadata
    finally:
        db.close()


def baseline_current(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    signal = contrarian_signal(candles, regime_label=regime["label"])
    if regime.get("is_mean_reverting"):
        return _skip_record(signal, "regime_filter")
    return signal


def baseline_v2_lvn_alpha2(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    return _baseline_v2_lvn_alpha(candles, regime, min_score=2)


def baseline_v2_lvn_alpha3(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    return _baseline_v2_lvn_alpha(candles, regime, min_score=3)


def spike_reversal_down(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 6:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down")

    last = candles[-1]
    last_direction = _direction(last)
    streak = _streak_length(candles, last_direction)
    if last_direction != "UP":
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down")

    volume_spike = _has_volume_spike(candles[-5:])
    upper_wick = _upper_wick_ratio(last)
    near_high = _near_local_high(candles[-6:], tolerance=0.0025)
    if not (volume_spike and near_high and upper_wick >= 0.0015):
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down")

    if regime["label"] not in {"HIGH_VOL / NEUTRAL", "HIGH_VOL / TRENDING", "MEDIUM_VOL / NEUTRAL"}:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down")

    if streak > 4:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down")

    conviction = 4 if regime["label"] == "HIGH_VOL / TRENDING" else 3
    estimate = _reversal_estimate(
        direction="DOWN",
        streak=streak,
        wick_ratio=upper_wick,
        stronger_regime=regime["label"] == "HIGH_VOL / TRENDING",
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "spike_reversal_down",
                f"streak_{streak}",
                "direction_up",
                "volume_spike",
                "near_local_high",
                "upper_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def flush_bounce_up(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 6:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up")

    last = candles[-1]
    last_direction = _direction(last)
    streak = _streak_length(candles, last_direction)
    if last_direction != "DOWN":
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up")

    volume_spike = _has_volume_spike(candles[-5:])
    lower_wick = _lower_wick_ratio(last)
    near_low = _near_local_low(candles[-6:], tolerance=0.0025)
    if not (volume_spike and near_low and lower_wick >= 0.0015):
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up")

    if regime["label"] not in {"HIGH_VOL / MEAN_REVERTING", "MEDIUM_VOL / MEAN_REVERTING"}:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up")

    if streak > 4:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up")

    conviction = 4 if regime["label"] == "HIGH_VOL / MEAN_REVERTING" else 3
    estimate = _reversal_estimate(
        direction="UP",
        streak=streak,
        wick_ratio=lower_wick,
        stronger_regime=regime["label"] == "HIGH_VOL / MEAN_REVERTING",
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "flush_bounce_up",
                f"streak_{streak}",
                "direction_down",
                "volume_spike",
                "near_local_low",
                "lower_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def baseline_v3_spike_reversal(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    down = spike_reversal_down(candles, regime)
    if down["should_trade"]:
        return down
    up = flush_bounce_up(candles, regime)
    if up["should_trade"]:
        return up
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v3_spike_reversal")


def baseline_v3_momentum_shape(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    continuation = baseline_v2_lvn_alpha2(candles, regime)
    if continuation["should_trade"]:
        return continuation
    reversal = baseline_v3_spike_reversal(candles, regime)
    if reversal["should_trade"]:
        return reversal
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v3_momentum_shape")


def spike_reversal_down_no_hvt(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    signal = spike_reversal_down(candles, regime)
    if not signal["should_trade"]:
        return signal
    if regime["label"] == "HIGH_VOL / TRENDING":
        return _skip_record(signal, "spike_reversal_down_no_hvt")
    return signal


def baseline_v3_reversal_core(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    down = spike_reversal_down_no_hvt(candles, regime)
    if down["should_trade"]:
        return down
    up = flush_bounce_up(candles, regime)
    if up["should_trade"]:
        return up
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v3_reversal_core")


def spike_reversal_down_window10(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window10")

    last = candles[-1]
    if _direction(last) != "UP":
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window10")

    if regime["label"] not in {"HIGH_VOL / NEUTRAL", "MEDIUM_VOL / NEUTRAL"}:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window10")

    volume_spike = _has_volume_spike(candles[-5:])
    upper_wick = _upper_wick_ratio(last)
    state = _window_state(candles, length=10)
    if not volume_spike or upper_wick < 0.0015:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window10")
    if state["ret"] < 0.003 or state["range_pos"] < 0.82 or state["up_ratio"] < 0.6:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window10")

    conviction = 4 if state["ret"] >= 0.006 and state["range_pos"] >= 0.94 else 3
    estimate = _window_state_estimate(
        direction="DOWN",
        ret10=state["ret"],
        range_pos=state["range_pos"],
        wick_ratio=upper_wick,
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "spike_reversal_down_window10",
                "direction_up",
                "volume_spike",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
                "upper_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def spike_reversal_down_window6(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 6:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window6")

    last = candles[-1]
    if _direction(last) != "UP":
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window6")

    if regime["label"] not in {"HIGH_VOL / NEUTRAL", "MEDIUM_VOL / NEUTRAL"}:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window6")

    volume_spike = _has_volume_spike(candles[-5:])
    upper_wick = _upper_wick_ratio(last)
    state = _window_state(candles, length=6)
    if not volume_spike or upper_wick < 0.0015:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window6")
    if state["ret"] < 0.002 or state["range_pos"] < 0.80 or state["up_ratio"] < 0.6:
        return _skip_record(_no_trade_stub(regime["label"]), "spike_reversal_down_window6")

    conviction = 4 if state["ret"] >= 0.0045 and state["range_pos"] >= 0.93 else 3
    estimate = _window_state_estimate(
        direction="DOWN",
        ret10=state["ret"],
        range_pos=state["range_pos"],
        wick_ratio=upper_wick,
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "spike_reversal_down_window6",
                "direction_up",
                "volume_spike",
                f"ret6_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
                "upper_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def flush_bounce_up_window10(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window10")

    last = candles[-1]
    if _direction(last) != "DOWN":
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window10")

    if regime["label"] not in {"HIGH_VOL / MEAN_REVERTING", "MEDIUM_VOL / MEAN_REVERTING"}:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window10")

    volume_spike = _has_volume_spike(candles[-5:])
    lower_wick = _lower_wick_ratio(last)
    state = _window_state(candles, length=10)
    if not volume_spike or lower_wick < 0.0015:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window10")
    if state["ret"] > -0.003 or state["range_pos"] > 0.18 or state["up_ratio"] > 0.4:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window10")

    conviction = 4 if state["ret"] <= -0.006 and state["range_pos"] <= 0.06 else 3
    estimate = _window_state_estimate(
        direction="UP",
        ret10=abs(state["ret"]),
        range_pos=1.0 - state["range_pos"],
        wick_ratio=lower_wick,
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "flush_bounce_up_window10",
                "direction_down",
                "volume_spike",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
                "lower_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def flush_bounce_up_window6(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 6:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window6")

    last = candles[-1]
    if _direction(last) != "DOWN":
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window6")

    if regime["label"] not in {"HIGH_VOL / MEAN_REVERTING", "MEDIUM_VOL / MEAN_REVERTING"}:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window6")

    volume_spike = _has_volume_spike(candles[-5:])
    lower_wick = _lower_wick_ratio(last)
    state = _window_state(candles, length=6)
    if not volume_spike or lower_wick < 0.0015:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window6")
    if state["ret"] > -0.002 or state["range_pos"] > 0.20 or state["up_ratio"] > 0.42:
        return _skip_record(_no_trade_stub(regime["label"]), "flush_bounce_up_window6")

    conviction = 4 if state["ret"] <= -0.0045 and state["range_pos"] <= 0.07 else 3
    estimate = _window_state_estimate(
        direction="UP",
        ret10=abs(state["ret"]),
        range_pos=1.0 - state["range_pos"],
        wick_ratio=lower_wick,
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "flush_bounce_up_window6",
                "direction_down",
                "volume_spike",
                f"ret6_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
                "lower_wick",
            ]
        ),
        "regime_label": regime["label"],
    }


def baseline_v4_window_state(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    down = spike_reversal_down_window10(candles, regime)
    if down["should_trade"]:
        return down
    up = flush_bounce_up_window10(candles, regime)
    if up["should_trade"]:
        return up
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v4_window_state")


def baseline_v4_window_state_w6(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    down = spike_reversal_down_window6(candles, regime)
    if down["should_trade"]:
        return down
    up = flush_bounce_up_window6(candles, regime)
    if up["should_trade"]:
        return up
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v4_window_state_w6")


def clean_continuation_up(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    last = candles[-1]
    if _direction(last) != "UP":
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    if regime["label"] not in {"HIGH_VOL / TRENDING", "MEDIUM_VOL / TRENDING"}:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    streak = _streak_length(candles, "UP")
    if streak < 2:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    if _is_spike_tail_event(candles, direction="UP"):
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    state = _window_state(candles, 10)
    if state["ret"] < 0.002 or state["ret"] > 0.006:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")
    if state["range_pos"] < 0.55 or state["range_pos"] > 0.88:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")
    if state["up_ratio"] < 0.6:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_up")

    conviction = 4 if regime["label"] == "HIGH_VOL / TRENDING" and streak >= 3 else 3
    estimate = _continuation_estimate(
        direction="UP",
        streak=streak,
        ret10=state["ret"],
        range_pos=state["range_pos"],
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "clean_continuation_up",
                f"streak_{streak}",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
            ]
        ),
        "regime_label": regime["label"],
    }


def clean_continuation_down(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    last = candles[-1]
    if _direction(last) != "DOWN":
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    if regime["label"] not in {"LOW_VOL / NEUTRAL", "MEDIUM_VOL / NEUTRAL"}:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    streak = _streak_length(candles, "DOWN")
    if streak < 2:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    if _is_spike_tail_event(candles, direction="DOWN"):
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    state = _window_state(candles, 10)
    if state["ret"] > -0.002 or state["ret"] < -0.006:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")
    if state["range_pos"] < 0.12 or state["range_pos"] > 0.45:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")
    if state["up_ratio"] > 0.4:
        return _skip_record(_no_trade_stub(regime["label"]), "clean_continuation_down")

    conviction = 4 if regime["label"] == "LOW_VOL / NEUTRAL" and streak >= 3 else 3
    estimate = _continuation_estimate(
        direction="DOWN",
        streak=streak,
        ret10=abs(state["ret"]),
        range_pos=1.0 - state["range_pos"],
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "clean_continuation_down",
                f"streak_{streak}",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
            ]
        ),
        "regime_label": regime["label"],
    }


def baseline_v5_clean_continuation(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    up = clean_continuation_up(candles, regime)
    if up["should_trade"]:
        return up
    down = clean_continuation_down(candles, regime)
    if down["should_trade"]:
        return down
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v5_clean_continuation")


def medium_neutral_down_continuation(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")

    last = candles[-1]
    if _direction(last) != "DOWN":
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")

    if regime["label"] != "MEDIUM_VOL / NEUTRAL":
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")

    streak = _streak_length(candles, "DOWN")
    if streak < 2:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")

    state = _window_state(candles, 10)
    lower_wick = _lower_wick_ratio(last)
    volume_spike = _has_volume_spike(candles[-5:])
    if state["ret"] > -0.0015 or state["ret"] < -0.008:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")
    if state["range_pos"] < 0.10 or state["range_pos"] > 0.48:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")
    if state["up_ratio"] > 0.42:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")
    if lower_wick >= 0.0018:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")
    if volume_spike and state["range_pos"] < 0.16:
        return _skip_record(_no_trade_stub(regime["label"]), "medium_neutral_down_continuation")

    conviction = 4 if streak >= 3 and state["range_pos"] <= 0.35 else 3
    estimate = _continuation_estimate(
        direction="DOWN",
        streak=streak,
        ret10=abs(state["ret"]),
        range_pos=1.0 - state["range_pos"],
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "medium_neutral_down_continuation",
                f"streak_{streak}",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
            ]
        ),
        "regime_label": regime["label"],
    }


def medium_neutral_down_continuation_balanced(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    base = medium_neutral_down_continuation(candles, regime)
    if not base["should_trade"]:
        return base

    state = _window_state(candles, 10)
    streak = _streak_length(candles, "DOWN")
    if streak > 5:
        return _skip_record(base, "medium_neutral_down_continuation_balanced")
    if state["ret"] < -0.0045:
        return _skip_record(base, "medium_neutral_down_continuation_balanced")
    if state["range_pos"] < 0.16 or state["range_pos"] > 0.40:
        return _skip_record(base, "medium_neutral_down_continuation_balanced")
    if state["up_ratio"] > 0.35:
        return _skip_record(base, "medium_neutral_down_continuation_balanced")
    return _overlay_record(base, "medium_neutral_down_continuation_balanced")


def medium_neutral_down_continuation_core(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    base = medium_neutral_down_continuation_balanced(candles, regime)
    if not base["should_trade"]:
        return base

    state = _window_state(candles, 10)
    streak = _streak_length(candles, "DOWN")
    if streak < 2 or streak > 5:
        return _skip_record(base, "medium_neutral_down_continuation_core")
    if state["ret"] < -0.0045 or state["ret"] > -0.0018:
        return _skip_record(base, "medium_neutral_down_continuation_core")
    if state["range_pos"] < 0.18 or state["range_pos"] > 0.36:
        return _skip_record(base, "medium_neutral_down_continuation_core")
    return _overlay_record(base, "medium_neutral_down_continuation_core")


def baseline_v6_broad_shape(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    reversal = baseline_v3_reversal_core(candles, regime)
    if reversal["should_trade"]:
        return reversal
    continuation = medium_neutral_down_continuation(candles, regime)
    if continuation["should_trade"]:
        return continuation
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_v6_broad_shape")


def low_vol_branch_v1(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if len(candles) < 10:
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")
    if not regime["label"].startswith("LOW_VOL /"):
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")

    last = candles[-1]
    if _direction(last) != "UP":
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")

    streak = _streak_length(candles, "UP")
    if streak != 1:
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")
    if _is_spike_tail_event(candles, direction="UP"):
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")

    state = _window_state(candles, 10)
    if state["ret"] <= 0.0 or state["ret"] > 0.008:
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")
    if state["range_pos"] < 0.35 or state["range_pos"] > 0.88:
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")
    if state["up_ratio"] < 0.45:
        return _skip_record(_no_trade_stub(regime["label"]), "low_vol_branch_v1")

    conviction = 4 if regime["label"] == "LOW_VOL / TRENDING" and state["up_ratio"] >= 0.55 else 3
    estimate = _continuation_estimate(
        direction="UP",
        streak=streak,
        ret10=max(state["ret"], 0.0),
        range_pos=state["range_pos"],
        stronger_state=conviction >= 4,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(
            [
                "low_vol_branch_v1",
                "new_up_move",
                f"ret10_bp_{int(round(state['ret'] * 10000))}",
                f"range_pos_{state['range_pos']:.2f}",
                f"up_ratio_{state['up_ratio']:.2f}",
            ]
        ),
        "regime_label": regime["label"],
    }


def medium_vol_branch_v1(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if not regime["label"].startswith("MEDIUM_VOL /"):
        return _skip_record(_no_trade_stub(regime["label"]), "medium_vol_branch_v1")

    continuation = medium_neutral_down_continuation(candles, regime)
    if continuation["should_trade"]:
        return continuation

    bounce = flush_bounce_up(candles, regime)
    if bounce["should_trade"]:
        return bounce

    reversal = spike_reversal_down_no_hvt(candles, regime)
    if reversal["should_trade"]:
        return reversal

    return _skip_record(_no_trade_stub(regime["label"]), "medium_vol_branch_v1")


def medium_vol_branch_v2(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    label = regime["label"]
    if not label.startswith("MEDIUM_VOL /"):
        return _skip_record(_no_trade_stub(label), "medium_vol_branch_v2")

    if label == "MEDIUM_VOL / MEAN_REVERTING":
        bounce = flush_bounce_up(candles, regime)
        if not bounce["should_trade"]:
            return _skip_record(_no_trade_stub(label), "medium_vol_branch_v2")
        bounce_score = _medium_bounce_score(candles)
        if bounce_score < 4:
            return _skip_record(bounce, "medium_vol_branch_v2")
        return _overlay_record(bounce, f"medium_vol_branch_v2_score_{bounce_score}")

    if label != "MEDIUM_VOL / NEUTRAL":
        return _skip_record(_no_trade_stub(label), "medium_vol_branch_v2")

    continuation = medium_neutral_down_continuation(candles, regime)
    reversal = spike_reversal_down_no_hvt(candles, regime)
    continuation_score = _medium_continuation_score(candles, continuation)
    reversal_score = _medium_reversal_score(candles, reversal)

    candidates: list[tuple[int, float, dict[str, Any]]] = []
    if continuation["should_trade"] and continuation_score >= 4:
        candidates.append((continuation_score, abs(float(continuation["estimate"]) - 0.5), continuation))
    if reversal["should_trade"] and reversal_score >= 4:
        candidates.append((reversal_score, abs(float(reversal["estimate"]) - 0.5), reversal))
    if not candidates:
        return _skip_record(_no_trade_stub(label), "medium_vol_branch_v2")

    _, _, selected = max(candidates, key=lambda item: (item[0], item[1]))
    selected_score = continuation_score if selected is continuation else reversal_score
    return _overlay_record(selected, f"medium_vol_branch_v2_score_{selected_score}")


def medium_vol_branch_v3(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if not regime["label"].startswith("MEDIUM_VOL /"):
        return _skip_record(_no_trade_stub(regime["label"]), "medium_vol_branch_v3")

    continuation = medium_neutral_down_continuation_balanced(candles, regime)
    if continuation["should_trade"]:
        return continuation

    bounce = flush_bounce_up(candles, regime)
    if bounce["should_trade"]:
        return bounce

    reversal = spike_reversal_down_no_hvt(candles, regime)
    if reversal["should_trade"]:
        return reversal

    return _skip_record(_no_trade_stub(regime["label"]), "medium_vol_branch_v3")


def high_vol_branch_v1(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    if not regime["label"].startswith("HIGH_VOL /"):
        return _skip_record(_no_trade_stub(regime["label"]), "high_vol_branch_v1")

    if regime["label"] == "HIGH_VOL / TRENDING":
        return _skip_record(_no_trade_stub(regime["label"]), "high_vol_branch_v1")

    bounce = flush_bounce_up(candles, regime)
    if bounce["should_trade"]:
        return bounce

    reversal = spike_reversal_down_no_hvt(candles, regime)
    if reversal["should_trade"]:
        return reversal

    return _skip_record(_no_trade_stub(regime["label"]), "high_vol_branch_v1")


def baseline_router_v1(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    label = regime["label"]
    if label.startswith("LOW_VOL /"):
        signal = low_vol_branch_v1(candles, regime)
        if signal["should_trade"]:
            return signal
    elif label.startswith("MEDIUM_VOL /"):
        signal = medium_vol_branch_v1(candles, regime)
        if signal["should_trade"]:
            return signal
    elif label.startswith("HIGH_VOL /"):
        signal = high_vol_branch_v1(candles, regime)
        if signal["should_trade"]:
            return signal
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_router_v1")


def baseline_router_v2(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    label = regime["label"]
    if label.startswith("LOW_VOL /"):
        signal = low_vol_branch_v1(candles, regime)
        if signal["should_trade"]:
            return signal
    elif label.startswith("MEDIUM_VOL /"):
        signal = medium_vol_branch_v3(candles, regime)
        if signal["should_trade"]:
            return signal
    elif label.startswith("HIGH_VOL /"):
        signal = high_vol_branch_v1(candles, regime)
        if signal["should_trade"]:
            return signal
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_router_v2")


def baseline_router_v2_candidate_filter(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    signal = baseline_router_v2(candles, regime)
    if not signal.get("should_trade"):
        return _skip_record(signal, "baseline_router_v2_candidate_filter")
    direction = str(signal.get("direction", "SKIP") or "SKIP")
    confidence = str(signal.get("confidence", "low") or "low")
    original_reason = str(signal.get("reason", "baseline_router_v2") or "baseline_router_v2")
    original_estimate = float(signal.get("estimate", 0.5))
    conviction_score = int(signal.get("conviction_score", 0))
    branch_tokens = [part.strip() for part in original_reason.split("|") if part.strip()]
    branch_name = branch_tokens[0] if branch_tokens else "baseline_router_v2"
    exact_rule_name = next((tag for tag in reversed(branch_tokens) if tag in FOUNDATION_RULE_TAGS), branch_name)
    shape_flags = {
        "has_continuation": "continuation" in original_reason,
        "has_reversal": "reversal" in original_reason or "bounce" in original_reason,
        "has_volume_spike": "volume_spike" in original_reason,
        "has_range_compression": "shrinking_ranges" in original_reason or "compression" in original_reason,
        "has_wick_signal": "upper_wick" in original_reason or "lower_wick" in original_reason,
        "has_near_extreme": "near_local_high" in original_reason or "near_local_low" in original_reason,
    }
    return {
        "estimate": 0.5,
        "should_trade": True,
        "direction": direction,
        "confidence": confidence,
        "conviction_score": conviction_score,
        "reason": f"{original_reason} | baseline_router_v2_candidate_filter",
        "regime_label": signal.get("regime_label", regime["label"]),
        "meta": {
            "candidate_filter": "baseline_router_v2",
            "estimate_ignored": True,
            "original_estimate": original_estimate,
            "original_reason": original_reason,
            "branch_name": branch_name,
            "exact_rule_name": exact_rule_name,
            "direction": direction,
            "confidence": confidence,
            "conviction_score": conviction_score,
            **shape_flags,
        },
    }


def baseline_router_v1_plus_lvn_alpha3(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    router = baseline_router_v1(candles, regime)
    if router["should_trade"]:
        return router
    overlay = baseline_v2_lvn_alpha3(candles, regime)
    if overlay["should_trade"]:
        return _overlay_record(overlay, "baseline_router_v1_plus_lvn_alpha3")
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_router_v1_plus_lvn_alpha3")


def baseline_router_v1_plus_v4(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    router = baseline_router_v1(candles, regime)
    if router["should_trade"]:
        return router
    overlay = baseline_v4_window_state(candles, regime)
    if overlay["should_trade"]:
        return _overlay_record(overlay, "baseline_router_v1_plus_v4")
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_router_v1_plus_v4")


def baseline_router_v1_plus_sparse_combo(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    router = baseline_router_v1(candles, regime)
    if router["should_trade"]:
        return router
    v4_overlay = baseline_v4_window_state(candles, regime)
    if v4_overlay["should_trade"]:
        return _overlay_record(v4_overlay, "baseline_router_v1_plus_sparse_combo")
    alpha_overlay = baseline_v2_lvn_alpha3(candles, regime)
    if alpha_overlay["should_trade"]:
        return _overlay_record(alpha_overlay, "baseline_router_v1_plus_sparse_combo")
    return _skip_record(_no_trade_stub(regime["label"]), "baseline_router_v1_plus_sparse_combo")


def only_low_vol_trending(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] != "LOW_VOL / TRENDING":
        return _skip_record(baseline, "only_low_vol_trending")
    return baseline


def only_low_vol_neutral(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] != "LOW_VOL / NEUTRAL":
        return _skip_record(baseline, "only_low_vol_neutral")
    return baseline


def only_lvn_up(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_low_vol_neutral(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    if baseline.get("direction") != "UP":
        return _skip_record(baseline, "only_lvn_up")
    return baseline


def only_lvn_up_compression(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_lvn_up(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    if "compression" not in str(baseline.get("reason", "")):
        return _skip_record(baseline, "only_lvn_up_compression")
    return baseline


def only_lvn_volume_spike(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_low_vol_neutral(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    if "volume_spike" not in str(baseline.get("reason", "")):
        return _skip_record(baseline, "only_lvn_volume_spike")
    return baseline


def only_lvn_up_volume_spike(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_lvn_up(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    if "volume_spike" not in str(baseline.get("reason", "")):
        return _skip_record(baseline, "only_lvn_up_volume_spike")
    return baseline


def only_lvn_up_volume_spike_streak4p(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_lvn_up_volume_spike(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    reason = str(baseline.get("reason", ""))
    if any(tag in reason for tag in ("streak_4", "streak_5", "streak_6", "streak_7", "streak_8", "streak_9")):
        return baseline
    return _skip_record(baseline, "only_lvn_up_volume_spike_streak4p")


def only_lvn_up_pure_volume_spike(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = only_lvn_up_volume_spike(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    reason = str(baseline.get("reason", ""))
    if "compression" in reason:
        return _skip_record(baseline, "only_lvn_up_pure_volume_spike")
    return baseline


def loosen_streak_low_vol_trending(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if baseline["should_trade"]:
        return baseline
    if regime["label"] != "LOW_VOL / TRENDING":
        return baseline
    if len(candles) < 5:
        return baseline

    last_direction = _direction(candles[-1])
    streak = _streak_length(candles, last_direction)
    if streak != 2:
        return baseline

    recent = candles[-3:]
    compression = _has_shrinking_ranges(recent)
    volume_spike = _has_volume_spike(candles[-5:])
    if not (compression or volume_spike):
        return baseline

    return _trade_record(
        regime_label=regime["label"],
        last_direction=last_direction,
        streak=streak,
        compression=compression,
        volume_spike=volume_spike,
        conviction=3,
        reasons=["relaxed_streak_2", f"direction_{last_direction.lower()}", "low_vol_trending"]
        + (["compression"] if compression else [])
        + (["volume_spike"] if volume_spike else []),
    )


def allow_trending_volume_spike(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if baseline["should_trade"]:
        return baseline
    if regime["label"] not in {"LOW_VOL / TRENDING", "MEDIUM_VOL / TRENDING"}:
        return baseline
    if len(candles) < 5:
        return baseline

    last_direction = _direction(candles[-1])
    streak = _streak_length(candles, last_direction)
    volume_spike = _has_volume_spike(candles[-5:])
    if streak < 2 or not volume_spike:
        return baseline

    return _trade_record(
        regime_label=regime["label"],
        last_direction=last_direction,
        streak=streak,
        compression=False,
        volume_spike=volume_spike,
        conviction=3,
        reasons=["allow_trending_volume_spike", f"streak_{streak}", f"direction_{last_direction.lower()}", "volume_spike"],
    )


def block_high_vol_neutral(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] == "HIGH_VOL / NEUTRAL":
        return _skip_record(baseline, "block_high_vol_neutral")
    return baseline


def block_high_vol_trending(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] == "HIGH_VOL / TRENDING":
        return _skip_record(baseline, "block_high_vol_trending")
    return baseline


def block_medium_vol_trending(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] == "MEDIUM_VOL / TRENDING":
        return _skip_record(baseline, "block_medium_vol_trending")
    return baseline


def only_low_vol(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if not regime["label"].startswith("LOW_VOL /"):
        return _skip_record(baseline, "only_low_vol")
    return baseline


def raise_conviction_low_vol_neutral(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if not baseline["should_trade"]:
        return baseline
    if regime["label"] != "LOW_VOL / NEUTRAL":
        return baseline
    if int(baseline.get("conviction_score", 0)) < 4:
        return _skip_record(baseline, "raise_conviction_low_vol_neutral")
    return baseline


def combo_lvt_relaxed(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
    signal = block_high_vol_neutral(candles, regime)
    if signal["should_trade"] or "block_high_vol_neutral" in str(signal.get("reason", "")):
        return signal
    signal = raise_conviction_low_vol_neutral(candles, regime)
    if signal["should_trade"] or "raise_conviction_low_vol_neutral" in str(signal.get("reason", "")):
        return signal
    signal = loosen_streak_low_vol_trending(candles, regime)
    if signal["should_trade"]:
        return signal
    return allow_trending_volume_spike(candles, regime)


def _build_rule_from_spec(spec: dict[str, Any]) -> RuleFn | None:
    action = str(spec.get("template_action") or "")
    if action == "block_regime":
        return _rule_block_regime(spec)
    if action == "tighten_regime":
        return _rule_tighten_regime(spec)
    if action == "raise_conviction":
        return _rule_raise_conviction(spec)
    if action == "raise_volume_confirmation":
        return _rule_raise_volume_confirmation(spec)
    if action == "allow_volume_spike_continuation":
        return _rule_allow_volume_spike_continuation(spec)
    if action == "allow_compression_continuation":
        return _rule_allow_compression_continuation(spec)
    if action == "loosen_streak_threshold":
        return _rule_loosen_streak_threshold(spec)
    if action == "allow_regime":
        return _rule_allow_regime(spec)
    return None


def _rule_block_regime(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if regime["label"] == target_scope:
            return _skip_record(baseline, rule_name)
        return baseline

    return _rule


def _rule_tighten_regime(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if regime["label"] != target_scope:
            return baseline
        if baseline["should_trade"] and int(baseline.get("conviction_score", 0)) < 4:
            return _skip_record(baseline, rule_name)
        return baseline

    return _rule


def _rule_raise_conviction(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if regime["label"] != target_scope:
            return baseline
        if baseline["should_trade"] and int(baseline.get("conviction_score", 0)) < 4:
            return _skip_record(baseline, rule_name)
        return baseline

    return _rule


def _rule_raise_volume_confirmation(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if regime["label"] != target_scope:
            return baseline
        if baseline["should_trade"] and "volume_spike" not in str(baseline.get("reason", "")):
            return _skip_record(baseline, rule_name)
        return baseline

    return _rule


def _rule_allow_volume_spike_continuation(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if baseline["should_trade"] or regime["label"] != target_scope or len(candles) < 5:
            return baseline
        last_direction = _direction(candles[-1])
        streak = _streak_length(candles, last_direction)
        volume_spike = _has_volume_spike(candles[-5:])
        if streak < 2 or not volume_spike:
            return baseline
        return _trade_record(
            regime_label=regime["label"],
            last_direction=last_direction,
            streak=streak,
            compression=False,
            volume_spike=True,
            conviction=3,
            reasons=[rule_name, f"streak_{streak}", f"direction_{last_direction.lower()}", "volume_spike"],
        )

    return _rule


def _rule_allow_compression_continuation(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if baseline["should_trade"] or regime["label"] != target_scope or len(candles) < 5:
            return baseline
        last_direction = _direction(candles[-1])
        streak = _streak_length(candles, last_direction)
        compression = _has_shrinking_ranges(candles[-3:])
        if streak < 2 or not compression:
            return baseline
        return _trade_record(
            regime_label=regime["label"],
            last_direction=last_direction,
            streak=streak,
            compression=True,
            volume_spike=False,
            conviction=3,
            reasons=[rule_name, f"streak_{streak}", f"direction_{last_direction.lower()}", "compression"],
        )

    return _rule


def _rule_loosen_streak_threshold(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if baseline["should_trade"] or regime["label"] != target_scope or len(candles) < 5:
            return baseline
        last_direction = _direction(candles[-1])
        streak = _streak_length(candles, last_direction)
        compression = _has_shrinking_ranges(candles[-3:])
        volume_spike = _has_volume_spike(candles[-5:])
        if streak != 2 or not (compression or volume_spike):
            return baseline
        return _trade_record(
            regime_label=regime["label"],
            last_direction=last_direction,
            streak=streak,
            compression=compression,
            volume_spike=volume_spike,
            conviction=3,
            reasons=[rule_name, "relaxed_streak_2", f"direction_{last_direction.lower()}"]
            + (["compression"] if compression else [])
            + (["volume_spike"] if volume_spike else []),
        )

    return _rule


def _rule_allow_regime(spec: dict[str, Any]) -> RuleFn:
    rule_name = str(spec["rule_name"])
    target_scope = str(spec["target_scope"])

    def _rule(candles: list[dict[str, Any]], regime: dict[str, Any]) -> dict[str, Any]:
        baseline = baseline_current(candles, regime)
        if baseline["should_trade"] or regime["label"] != target_scope:
            return baseline
        raw = contrarian_signal(candles, regime_label=regime["label"])
        if raw["should_trade"]:
            raw["reason"] = " | ".join([str(raw.get("reason", "")).strip(), rule_name]).strip(" |")
        return raw

    return _rule


def _baseline_v2_lvn_alpha(
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    *,
    min_score: int,
) -> dict[str, Any]:
    baseline = baseline_current(candles, regime)
    if regime["label"] != "LOW_VOL / NEUTRAL":
        return _skip_record(baseline, f"baseline_v2_lvn_score_lt_{min_score}")
    if len(candles) < 5:
        return _skip_record(baseline, f"baseline_v2_lvn_score_lt_{min_score}")

    last_direction = _direction(candles[-1])
    streak = _streak_length(candles, last_direction)
    volume_spike = _has_volume_spike(candles[-5:])
    compression = _has_shrinking_ranges(candles[-3:])

    alpha_up = last_direction == "UP"
    alpha_streak4p = alpha_up and streak >= 4
    alpha_volume_spike = volume_spike
    score = sum([alpha_up, alpha_volume_spike, alpha_streak4p])
    if score < min_score:
        return _skip_record(baseline, f"baseline_v2_lvn_score_lt_{min_score}")

    base_estimate = estimate_from_signal_features(
        last_direction=last_direction,
        streak=streak,
        compression=compression,
        volume_spike=volume_spike,
    )
    alpha_boost = 0.0
    if alpha_up:
        alpha_boost += 0.01
    if alpha_volume_spike:
        alpha_boost += 0.02
    if alpha_streak4p:
        alpha_boost += 0.02
    estimate = min(base_estimate + alpha_boost, 0.74)
    conviction = max(
        conviction_from_signal_features(
            streak=streak,
            compression=compression,
            volume_spike=volume_spike,
        ),
        4 if score >= 3 else 3,
    )
    reasons = [
        f"baseline_v2_lvn_score_{score}",
        f"streak_{streak}",
        f"direction_{last_direction.lower()}",
    ]
    if alpha_volume_spike:
        reasons.append("volume_spike")
    if alpha_streak4p:
        reasons.append("streak_4p")
    if compression:
        reasons.append("compression")

    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": "UP" if estimate > 0.5 else "DOWN",
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(reasons),
        "regime_label": regime["label"],
    }


def _trade_record(
    *,
    regime_label: str,
    last_direction: str,
    streak: int,
    compression: bool,
    volume_spike: bool,
    conviction: int,
    reasons: list[str],
) -> dict[str, Any]:
    estimate = estimate_from_signal_features(
        last_direction=last_direction,
        streak=streak,
        compression=compression,
        volume_spike=volume_spike,
    )
    return {
        "estimate": estimate,
        "should_trade": True,
        "direction": last_direction,
        "confidence": "high" if conviction >= 4 else "medium",
        "conviction_score": conviction,
        "reason": " | ".join(reasons),
        "regime_label": regime_label,
    }


def _overlay_record(signal: dict[str, Any], overlay_name: str) -> dict[str, Any]:
    reason = str(signal.get("reason", "")).strip()
    joined = f"{reason} | {overlay_name}" if reason else overlay_name
    enriched = dict(signal)
    enriched["reason"] = joined
    return enriched


def _skip_record(signal: dict[str, Any], suffix: str) -> dict[str, Any]:
    reason = str(signal.get("reason", "")).strip()
    joined = f"{reason} | {suffix}" if reason else suffix
    return {
        "estimate": 0.5,
        "should_trade": False,
        "direction": None,
        "confidence": "low",
        "conviction_score": 0,
        "reason": joined,
        "regime_label": signal.get("regime_label", "UNKNOWN"),
    }


def _no_trade_stub(regime_label: str) -> dict[str, Any]:
    return {"reason": "", "regime_label": regime_label}


def _upper_wick_ratio(candle: dict[str, Any]) -> float:
    open_ = float(candle["open"])
    close = float(candle["close"])
    high = float(candle["high"])
    if close >= open_:
        ref = close
    else:
        ref = open_
    if ref <= 0:
        return 0.0
    return max(0.0, high - ref) / ref


def _lower_wick_ratio(candle: dict[str, Any]) -> float:
    open_ = float(candle["open"])
    close = float(candle["close"])
    low = float(candle["low"])
    if close <= open_:
        ref = close
    else:
        ref = open_
    if ref <= 0:
        return 0.0
    return max(0.0, ref - low) / ref


def _near_local_high(candles: list[dict[str, Any]], tolerance: float) -> bool:
    if not candles:
        return False
    last = candles[-1]
    local_high = max(float(c["high"]) for c in candles)
    last_high = float(last["high"])
    if local_high <= 0:
        return False
    return (local_high - last_high) / local_high <= tolerance


def _near_local_low(candles: list[dict[str, Any]], tolerance: float) -> bool:
    if not candles:
        return False
    last = candles[-1]
    local_low = min(float(c["low"]) for c in candles)
    last_low = float(last["low"])
    if local_low <= 0:
        return False
    return (last_low - local_low) / local_low <= tolerance


def _reversal_estimate(
    *,
    direction: str,
    streak: int,
    wick_ratio: float,
    stronger_regime: bool,
) -> float:
    edge = 0.06
    edge += min(max(streak - 1, 0), 3) * 0.01
    if wick_ratio >= 0.0025:
        edge += 0.02
    elif wick_ratio >= 0.0015:
        edge += 0.01
    if stronger_regime:
        edge += 0.015
    edge = min(edge, 0.18)
    return 0.5 + edge if direction == "UP" else 0.5 - edge


def _window_state(candles: list[dict[str, Any]], length: int) -> dict[str, float]:
    window = candles[-length:]
    start_close = float(window[0]["close"])
    end_close = float(window[-1]["close"])
    highs = [float(c["high"]) for c in window]
    lows = [float(c["low"]) for c in window]
    up_ratio = sum(1 for c in window if _direction(c) == "UP") / len(window)
    ret = 0.0 if start_close <= 0 else end_close / start_close - 1.0
    window_range = max(highs) - min(lows)
    if window_range <= 0:
        range_pos = 0.5
    else:
        range_pos = (end_close - min(lows)) / window_range
    return {"ret": ret, "range_pos": range_pos, "up_ratio": up_ratio}


def _window_state_estimate(
    *,
    direction: str,
    ret10: float,
    range_pos: float,
    wick_ratio: float,
    stronger_state: bool,
) -> float:
    edge = 0.055
    if ret10 >= 0.003:
        edge += 0.01
    if ret10 >= 0.006:
        edge += 0.01
    if range_pos >= 0.9:
        edge += 0.01
    if range_pos >= 0.95:
        edge += 0.01
    if wick_ratio >= 0.0025:
        edge += 0.015
    elif wick_ratio >= 0.0015:
        edge += 0.008
    if stronger_state:
        edge += 0.01
    edge = min(edge, 0.18)
    return 0.5 + edge if direction == "UP" else 0.5 - edge


def _medium_continuation_score(candles: list[dict[str, Any]], signal: dict[str, Any]) -> int:
    if not signal.get("should_trade"):
        return 0
    state = _window_state(candles, 10)
    streak = _streak_length(candles, "DOWN")
    score = 1
    if streak >= 3:
        score += 1
    if 0.16 <= state["range_pos"] <= 0.36:
        score += 1
    if state["up_ratio"] <= 0.35:
        score += 1
    if 0.002 <= abs(state["ret"]) <= 0.0055:
        score += 1
    if "volume_spike" not in str(signal.get("reason", "")):
        score += 1
    return score


def _medium_reversal_score(candles: list[dict[str, Any]], signal: dict[str, Any]) -> int:
    if not signal.get("should_trade"):
        return 0
    state = _window_state(candles, 10)
    upper_wick = _upper_wick_ratio(candles[-1])
    score = 1
    if _has_volume_spike(candles[-5:]):
        score += 1
    if upper_wick >= 0.002:
        score += 1
    if state["ret"] >= 0.004:
        score += 1
    if state["range_pos"] >= 0.90:
        score += 1
    return score


def _medium_bounce_score(candles: list[dict[str, Any]]) -> int:
    state = _window_state(candles, 10)
    lower_wick = _lower_wick_ratio(candles[-1])
    score = 1
    if _has_volume_spike(candles[-5:]):
        score += 1
    if lower_wick >= 0.002:
        score += 1
    if abs(state["ret"]) >= 0.004:
        score += 1
    if state["range_pos"] <= 0.10:
        score += 1
    return score


def _is_spike_tail_event(candles: list[dict[str, Any]], direction: str) -> bool:
    if len(candles) < 10:
        return True

    last = candles[-1]
    state = _window_state(candles, length=10)
    volume_spike = _has_volume_spike(candles[-5:])
    upper_wick = _upper_wick_ratio(last)
    lower_wick = _lower_wick_ratio(last)

    if direction == "UP":
        if state["ret"] >= 0.0065 or state["range_pos"] >= 0.9:
            return True
        if upper_wick >= 0.0016:
            return True
        if volume_spike and (upper_wick >= 0.001 or state["range_pos"] >= 0.86):
            return True
        return False

    if state["ret"] <= -0.0065 or state["range_pos"] <= 0.1:
        return True
    if lower_wick >= 0.0016:
        return True
    if volume_spike and (lower_wick >= 0.001 or state["range_pos"] <= 0.14):
        return True
    return False


def _continuation_estimate(
    *,
    direction: str,
    streak: int,
    ret10: float,
    range_pos: float,
    stronger_state: bool,
) -> float:
    edge = 0.035
    edge += min(max(streak - 2, 0), 3) * 0.007
    if ret10 >= 0.0025:
        edge += 0.006
    if ret10 >= 0.004:
        edge += 0.006
    if range_pos >= 0.62:
        edge += 0.005
    if range_pos >= 0.74:
        edge += 0.005
    if stronger_state:
        edge += 0.008
    edge = min(edge, 0.12)
    return 0.5 + edge if direction == "UP" else 0.5 - edge
