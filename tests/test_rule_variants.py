from src.strategies.regime import compute_regime_from_candles
import sqlite3

from src.v3.rule_variants import available_rules, load_dynamic_coach_rule_drafts


def _candles(pattern: list[tuple[float, float, float]]) -> list[dict]:
    candles = []
    price = 100.0
    for delta, rng, vol in pattern:
        open_ = price
        close = price + delta
        high = max(open_, close) + rng / 2
        low = min(open_, close) - rng / 2
        candles.append(
            {
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": vol,
            }
        )
        price = close
    return candles


def test_loosen_streak_low_vol_trending_can_open_trade():
    candles = _candles(
        [
            (0.05, 0.30, 10),
            (0.06, 0.28, 10),
            (0.07, 0.26, 10),
            (0.08, 0.24, 10),
            (0.09, 0.22, 10),
            (0.10, 0.20, 10),
            (0.11, 0.18, 10),
            (0.12, 0.16, 10),
        ]
    )
    regime = compute_regime_from_candles(candles)
    result = available_rules()["loosen_streak_low_vol_trending"](candles, regime)
    assert result["should_trade"] is True
    assert result["estimate"] > 0.5


def test_block_high_vol_neutral_skips_trade():
    candles = _candles(
        [
            (0.40, 1.20, 10),
            (-0.30, 1.25, 10),
            (0.35, 1.30, 10),
            (0.45, 1.10, 12),
            (0.50, 1.00, 25),
        ]
    )
    regime = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["block_high_vol_neutral"](candles, regime)
    assert result["should_trade"] is False
    assert "block_high_vol_neutral" in result["reason"]


def test_strong_filters_skip_target_regimes():
    candles = _candles(
        [
            (0.20, 0.60, 12),
            (0.22, 0.58, 12),
            (0.24, 0.56, 12),
            (0.26, 0.54, 14),
            (0.28, 0.52, 20),
        ]
    )
    high_trending = {"label": "HIGH_VOL / TRENDING", "is_mean_reverting": False}
    medium_trending = {"label": "MEDIUM_VOL / TRENDING", "is_mean_reverting": False}
    medium_neutral = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}

    assert available_rules()["block_high_vol_trending"](candles, high_trending)["should_trade"] is False
    assert available_rules()["block_medium_vol_trending"](candles, medium_trending)["should_trade"] is False
    assert available_rules()["only_low_vol"](candles, medium_neutral)["should_trade"] is False


def test_low_vol_subfilters_only_allow_target_bucket():
    candles = _candles(
        [
            (0.08, 0.24, 9),
            (0.09, 0.22, 9),
            (0.10, 0.20, 9),
            (0.11, 0.18, 11),
            (0.12, 0.16, 16),
        ]
    )
    low_trending = {"label": "LOW_VOL / TRENDING", "is_mean_reverting": False}
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}

    assert "only_low_vol_trending" in available_rules()["only_low_vol_trending"](candles, low_neutral)["reason"]
    assert "only_low_vol_neutral" in available_rules()["only_low_vol_neutral"](candles, low_trending)["reason"]


def test_lvn_specialized_filters_narrow_exposure():
    up_spike = _candles(
        [
            (0.03, 0.20, 8),
            (0.04, 0.19, 8),
            (0.05, 0.18, 8),
            (0.06, 0.17, 8),
            (0.07, 0.16, 20),
        ]
    )
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    down_spike = _candles(
        [
            (-0.03, 0.20, 8),
            (-0.04, 0.19, 8),
            (-0.05, 0.18, 8),
            (-0.06, 0.17, 8),
            (-0.07, 0.16, 20),
        ]
    )

    assert available_rules()["only_lvn_up"](up_spike, low_neutral)["should_trade"] is True
    assert available_rules()["only_lvn_up"](down_spike, low_neutral)["should_trade"] is False
    assert available_rules()["only_lvn_volume_spike"](up_spike, low_neutral)["should_trade"] is True
    assert available_rules()["only_lvn_up_volume_spike"](up_spike, low_neutral)["should_trade"] is True


def test_lvn_up_compression_requires_compression_reason():
    up_spike = _candles(
        [
            (0.03, 0.20, 8),
            (0.04, 0.19, 8),
            (0.05, 0.18, 8),
            (0.06, 0.17, 8),
            (0.07, 0.16, 20),
        ]
    )
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["only_lvn_up_compression"](up_spike, low_neutral)
    assert result["should_trade"] is False
    assert "only_lvn_up_compression" in result["reason"]


def test_lvn_up_volume_spike_specializations_filter_reason():
    up_spike = _candles(
        [
            (0.03, 0.20, 8),
            (0.04, 0.19, 8),
            (0.05, 0.18, 8),
            (0.06, 0.17, 8),
            (0.07, 0.16, 20),
        ]
    )
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    pure = available_rules()["only_lvn_up_pure_volume_spike"](up_spike, low_neutral)
    streak4p = available_rules()["only_lvn_up_volume_spike_streak4p"](up_spike, low_neutral)
    assert pure["should_trade"] is True
    assert streak4p["should_trade"] is True


def test_baseline_v2_alpha_scores_gate_low_vol_neutral_only():
    score2_setup = _candles(
        [
            (0.02, 0.20, 8),
            (0.03, 0.19, 8),
            (0.04, 0.18, 8),
            (0.05, 0.17, 8),
            (0.06, 0.16, 12),
        ]
    )
    score3_setup = _candles(
        [
            (0.02, 0.20, 8),
            (0.03, 0.19, 8),
            (0.04, 0.18, 8),
            (0.05, 0.17, 8),
            (0.06, 0.16, 20),
        ]
    )
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    high_neutral = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}

    score2 = available_rules()["baseline_v2_lvn_alpha2"](score2_setup, low_neutral)
    score3_blocked = available_rules()["baseline_v2_lvn_alpha3"](score2_setup, low_neutral)
    score3 = available_rules()["baseline_v2_lvn_alpha3"](score3_setup, low_neutral)
    blocked = available_rules()["baseline_v2_lvn_alpha2"](score2_setup, high_neutral)

    assert score2["should_trade"] is True
    assert "baseline_v2_lvn_score_2" in score2["reason"]
    assert score3_blocked["should_trade"] is False
    assert "baseline_v2_lvn_score_lt_3" in score3_blocked["reason"]
    assert score3["should_trade"] is True
    assert "baseline_v2_lvn_score_3" in score3["reason"]
    assert blocked["should_trade"] is False
    assert "baseline_v2_lvn_score_lt_2" in blocked["reason"]


def test_router_overlay_lvn_alpha3_can_add_sparse_low_vol_trade():
    score3_setup = _candles(
        [
            (0.02, 0.20, 8),
            (0.03, 0.19, 8),
            (0.04, 0.18, 8),
            (0.05, 0.17, 8),
            (0.06, 0.16, 20),
        ]
    )
    low_neutral = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}

    router = available_rules()["baseline_router_v1"](score3_setup, low_neutral)
    overlay = available_rules()["baseline_router_v1_plus_lvn_alpha3"](score3_setup, low_neutral)

    assert router["should_trade"] is False
    assert overlay["should_trade"] is True
    assert "baseline_router_v1_plus_lvn_alpha3" in overlay["reason"]


def test_spike_reversal_down_trades_in_supported_regime():
    candles = [
        {"open": 100.0, "high": 100.2, "low": 99.7, "close": 99.85, "volume": 10},
        {"open": 99.85, "high": 100.05, "low": 99.65, "close": 99.75, "volume": 10},
        {"open": 99.75, "high": 100.15, "low": 99.65, "close": 100.0, "volume": 10},
        {"open": 100.0, "high": 100.45, "low": 99.9, "close": 100.25, "volume": 10},
        {"open": 100.25, "high": 100.78, "low": 100.2, "close": 100.66, "volume": 10},
        {"open": 100.66, "high": 101.05, "low": 100.55, "close": 100.82, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / TRENDING", "is_mean_reverting": False}
    result = available_rules()["spike_reversal_down"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] < 0.5

    narrowed = available_rules()["spike_reversal_down_no_hvt"](candles, regime)
    assert narrowed["should_trade"] is False
    assert "spike_reversal_down_no_hvt" in narrowed["reason"]


def test_flush_bounce_up_trades_in_supported_regime():
    candles = [
        {"open": 100.0, "high": 100.25, "low": 99.85, "close": 100.1, "volume": 10},
        {"open": 100.1, "high": 100.25, "low": 99.95, "close": 100.18, "volume": 10},
        {"open": 100.0, "high": 100.1, "low": 99.65, "close": 99.8, "volume": 10},
        {"open": 99.8, "high": 99.9, "low": 99.35, "close": 99.55, "volume": 10},
        {"open": 99.35, "high": 99.48, "low": 98.95, "close": 99.18, "volume": 10},
        {"open": 99.18, "high": 99.3, "low": 98.7, "close": 98.98, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / MEAN_REVERTING", "is_mean_reverting": True}
    result = available_rules()["flush_bounce_up"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "UP"
    assert result["estimate"] > 0.5


def test_dynamic_coach_rule_drafts_register_supported_specs(tmp_path):
    db_path = tmp_path / "research.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE coach_rule_candidate_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            regime TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            spec_name TEXT NOT NULL,
            spec_label TEXT NOT NULL,
            family TEXT NOT NULL,
            target_scope TEXT NOT NULL,
            template_action TEXT NOT NULL,
            implementation_hint TEXT,
            config_json TEXT NOT NULL,
            support_count INTEGER NOT NULL,
            helpful_count INTEGER NOT NULL,
            harmful_count INTEGER NOT NULL,
            precision REAL NOT NULL,
            net_helpful INTEGER NOT NULL,
            eligible_for_ablation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO coach_rule_candidate_specs (
            coach_model, coach_type, tag, regime, window_start, window_end,
            spec_name, spec_label, family, target_scope, template_action,
            implementation_hint, config_json, support_count, helpful_count,
            harmful_count, precision, net_helpful, eligible_for_ablation, updated_at
        ) VALUES (
            'deepseek-ai/DeepSeek-V3', 'toxicity_coach', 'block_high_vol_neutral',
            'LOW_VOL / NEUTRAL', '2026-03-28T00:00:00+00:00', '2026-04-04T00:00:00+00:00',
            'toxicity_coach__block_high_vol_neutral__low_vol_neutral',
            'Block baseline in observed regime [LOW_VOL / NEUTRAL]',
            'regime_block', 'LOW_VOL / NEUTRAL', 'block_regime',
            'hint', '{}', 15, 11, 4, 0.73, 7, 1, '2026-04-04T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    conn.close()

    drafts = load_dynamic_coach_rule_drafts(db_path)

    assert "coach_spec__toxicity_coach__block_high_vol_neutral__low_vol_neutral" in drafts


def test_dynamic_block_regime_draft_applies_skip(tmp_path):
    db_path = tmp_path / "research.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE coach_rule_candidate_specs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coach_model TEXT NOT NULL,
            coach_type TEXT NOT NULL,
            tag TEXT NOT NULL,
            regime TEXT NOT NULL,
            window_start TEXT NOT NULL,
            window_end TEXT NOT NULL,
            spec_name TEXT NOT NULL,
            spec_label TEXT NOT NULL,
            family TEXT NOT NULL,
            target_scope TEXT NOT NULL,
            template_action TEXT NOT NULL,
            implementation_hint TEXT,
            config_json TEXT NOT NULL,
            support_count INTEGER NOT NULL,
            helpful_count INTEGER NOT NULL,
            harmful_count INTEGER NOT NULL,
            precision REAL NOT NULL,
            net_helpful INTEGER NOT NULL,
            eligible_for_ablation INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO coach_rule_candidate_specs (
            coach_model, coach_type, tag, regime, window_start, window_end,
            spec_name, spec_label, family, target_scope, template_action,
            implementation_hint, config_json, support_count, helpful_count,
            harmful_count, precision, net_helpful, eligible_for_ablation, updated_at
        ) VALUES (
            'deepseek-ai/DeepSeek-V3', 'toxicity_coach', 'block_high_vol_neutral',
            'HIGH_VOL / NEUTRAL', '2026-03-28T00:00:00+00:00', '2026-04-04T00:00:00+00:00',
            'toxicity_coach__block_high_vol_neutral__high_vol_neutral',
            'Block baseline in observed regime [HIGH_VOL / NEUTRAL]',
            'regime_block', 'HIGH_VOL / NEUTRAL', 'block_regime',
            'hint', '{}', 15, 11, 4, 0.73, 7, 1, '2026-04-04T00:00:00+00:00'
        )
        """
    )
    conn.commit()
    conn.close()

    rule = load_dynamic_coach_rule_drafts(db_path)["coach_spec__toxicity_coach__block_high_vol_neutral__high_vol_neutral"]
    candles = _candles(
        [
            (0.40, 1.20, 10),
            (-0.30, 1.25, 10),
            (0.35, 1.30, 10),
            (0.45, 1.10, 12),
            (0.50, 1.00, 25),
        ]
    )
    regime = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}

    result = rule(candles, regime)

    assert result["should_trade"] is False
    assert "coach_spec__toxicity_coach__block_high_vol_neutral__high_vol_neutral" in result["reason"]


def test_spike_reversal_down_window10_trades_on_extended_up_window():
    candles = [
        {"open": 100.0, "high": 100.15, "low": 99.95, "close": 100.08, "volume": 10},
        {"open": 100.08, "high": 100.25, "low": 100.0, "close": 100.16, "volume": 10},
        {"open": 100.16, "high": 100.34, "low": 100.1, "close": 100.24, "volume": 10},
        {"open": 100.24, "high": 100.42, "low": 100.18, "close": 100.31, "volume": 10},
        {"open": 100.31, "high": 100.5, "low": 100.24, "close": 100.39, "volume": 10},
        {"open": 100.39, "high": 100.58, "low": 100.32, "close": 100.47, "volume": 10},
        {"open": 100.47, "high": 100.66, "low": 100.4, "close": 100.55, "volume": 10},
        {"open": 100.55, "high": 100.74, "low": 100.48, "close": 100.63, "volume": 10},
        {"open": 100.63, "high": 100.85, "low": 100.58, "close": 100.74, "volume": 10},
        {"open": 100.74, "high": 101.1, "low": 100.68, "close": 100.9, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["spike_reversal_down_window10"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] < 0.5


def test_flush_bounce_up_window10_trades_on_extended_down_window():
    candles = [
        {"open": 100.0, "high": 100.05, "low": 99.88, "close": 99.95, "volume": 10},
        {"open": 99.95, "high": 100.0, "low": 99.78, "close": 99.86, "volume": 10},
        {"open": 99.86, "high": 99.91, "low": 99.66, "close": 99.76, "volume": 10},
        {"open": 99.76, "high": 99.8, "low": 99.54, "close": 99.65, "volume": 10},
        {"open": 99.65, "high": 99.7, "low": 99.41, "close": 99.53, "volume": 10},
        {"open": 99.53, "high": 99.58, "low": 99.28, "close": 99.41, "volume": 10},
        {"open": 99.41, "high": 99.46, "low": 99.15, "close": 99.29, "volume": 10},
        {"open": 99.29, "high": 99.35, "low": 99.01, "close": 99.16, "volume": 10},
        {"open": 99.16, "high": 99.22, "low": 98.85, "close": 99.02, "volume": 10},
        {"open": 99.02, "high": 99.08, "low": 98.62, "close": 98.84, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / MEAN_REVERTING", "is_mean_reverting": True}
    result = available_rules()["flush_bounce_up_window10"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "UP"
    assert result["estimate"] > 0.5


def test_spike_reversal_down_window6_trades_on_extended_up_window():
    candles = [
        {"open": 100.00, "high": 100.18, "low": 99.98, "close": 100.11, "volume": 10},
        {"open": 100.11, "high": 100.33, "low": 100.07, "close": 100.24, "volume": 10},
        {"open": 100.24, "high": 100.48, "low": 100.20, "close": 100.39, "volume": 10},
        {"open": 100.39, "high": 100.62, "low": 100.34, "close": 100.53, "volume": 10},
        {"open": 100.53, "high": 100.76, "low": 100.48, "close": 100.69, "volume": 10},
        {"open": 100.69, "high": 101.12, "low": 100.64, "close": 100.92, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["spike_reversal_down_window6"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] < 0.5


def test_flush_bounce_up_window6_trades_on_extended_down_window():
    candles = [
        {"open": 100.00, "high": 100.05, "low": 99.80, "close": 99.90, "volume": 10},
        {"open": 99.90, "high": 99.96, "low": 99.64, "close": 99.76, "volume": 10},
        {"open": 99.76, "high": 99.82, "low": 99.46, "close": 99.60, "volume": 10},
        {"open": 99.60, "high": 99.66, "low": 99.25, "close": 99.42, "volume": 10},
        {"open": 99.42, "high": 99.48, "low": 99.02, "close": 99.22, "volume": 10},
        {"open": 99.22, "high": 99.28, "low": 98.74, "close": 99.00, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / MEAN_REVERTING", "is_mean_reverting": True}
    result = available_rules()["flush_bounce_up_window6"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "UP"
    assert result["estimate"] > 0.5


def test_clean_continuation_up_trades_without_spike_tail():
    candles = [
        {"open": 100.00, "high": 100.18, "low": 99.94, "close": 100.05, "volume": 10},
        {"open": 100.05, "high": 100.20, "low": 99.98, "close": 100.09, "volume": 10},
        {"open": 100.09, "high": 100.24, "low": 100.00, "close": 100.12, "volume": 10},
        {"open": 100.12, "high": 100.26, "low": 100.02, "close": 100.14, "volume": 10},
        {"open": 100.14, "high": 100.28, "low": 100.04, "close": 100.17, "volume": 10},
        {"open": 100.17, "high": 100.30, "low": 100.07, "close": 100.19, "volume": 10},
        {"open": 100.19, "high": 100.31, "low": 100.08, "close": 100.21, "volume": 10},
        {"open": 100.21, "high": 100.32, "low": 100.10, "close": 100.23, "volume": 10},
        {"open": 100.23, "high": 100.33, "low": 100.12, "close": 100.25, "volume": 10},
        {"open": 100.25, "high": 100.34, "low": 100.14, "close": 100.27, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / TRENDING", "is_mean_reverting": False}
    result = available_rules()["clean_continuation_up"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "UP"
    assert result["estimate"] > 0.5


def test_clean_continuation_down_trades_without_spike_tail():
    candles = [
        {"open": 100.00, "high": 100.06, "low": 99.82, "close": 99.95, "volume": 10},
        {"open": 99.95, "high": 100.01, "low": 99.78, "close": 99.90, "volume": 10},
        {"open": 99.90, "high": 99.96, "low": 99.74, "close": 99.86, "volume": 10},
        {"open": 99.86, "high": 99.91, "low": 99.70, "close": 99.83, "volume": 10},
        {"open": 99.83, "high": 99.88, "low": 99.67, "close": 99.80, "volume": 10},
        {"open": 99.80, "high": 99.85, "low": 99.64, "close": 99.77, "volume": 10},
        {"open": 99.77, "high": 99.82, "low": 99.61, "close": 99.74, "volume": 10},
        {"open": 99.74, "high": 99.79, "low": 99.58, "close": 99.71, "volume": 10},
        {"open": 99.71, "high": 99.76, "low": 99.55, "close": 99.68, "volume": 10},
        {"open": 99.68, "high": 99.73, "low": 99.52, "close": 99.65, "volume": 10},
    ]
    regime = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["clean_continuation_down"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] < 0.5


def test_router_overlay_v4_can_add_window_state_reversal():
    candles = [
        {"open": 100.0, "high": 100.15, "low": 99.95, "close": 100.08, "volume": 10},
        {"open": 100.08, "high": 100.25, "low": 100.0, "close": 100.16, "volume": 10},
        {"open": 100.16, "high": 100.34, "low": 100.1, "close": 100.24, "volume": 10},
        {"open": 100.24, "high": 100.42, "low": 100.18, "close": 100.31, "volume": 10},
        {"open": 100.31, "high": 100.5, "low": 100.24, "close": 100.39, "volume": 10},
        {"open": 100.39, "high": 100.58, "low": 100.32, "close": 100.47, "volume": 10},
        {"open": 100.47, "high": 100.66, "low": 100.4, "close": 100.55, "volume": 10},
        {"open": 100.55, "high": 100.74, "low": 100.48, "close": 100.63, "volume": 10},
        {"open": 100.63, "high": 100.85, "low": 100.58, "close": 100.74, "volume": 10},
        {"open": 100.74, "high": 101.1, "low": 100.68, "close": 100.9, "volume": 24},
    ]
    regime = {"label": "HIGH_VOL / NEUTRAL", "is_mean_reverting": False}

    router = available_rules()["baseline_router_v1"](candles, regime)
    overlay = available_rules()["baseline_router_v1_plus_v4"](candles, regime)

    assert router["should_trade"] is False
    assert overlay["should_trade"] is True
    assert "baseline_router_v1_plus_v4" in overlay["reason"]


def test_medium_neutral_down_continuation_trades_in_target_bucket():
    candles = [
        {"open": 100.00, "high": 100.06, "low": 99.82, "close": 99.95, "volume": 10},
        {"open": 99.95, "high": 100.01, "low": 99.78, "close": 99.90, "volume": 10},
        {"open": 99.90, "high": 99.96, "low": 99.74, "close": 99.86, "volume": 10},
        {"open": 99.86, "high": 99.91, "low": 99.70, "close": 99.83, "volume": 10},
        {"open": 99.83, "high": 99.88, "low": 99.67, "close": 99.80, "volume": 10},
        {"open": 99.80, "high": 99.85, "low": 99.64, "close": 99.77, "volume": 10},
        {"open": 99.77, "high": 99.82, "low": 99.61, "close": 99.74, "volume": 10},
        {"open": 99.74, "high": 99.79, "low": 99.58, "close": 99.71, "volume": 10},
        {"open": 99.71, "high": 99.76, "low": 99.55, "close": 99.68, "volume": 10},
        {"open": 99.68, "high": 99.73, "low": 99.52, "close": 99.65, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_neutral_down_continuation"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] < 0.5


def test_medium_neutral_down_continuation_balanced_filters_extreme_tail():
    candles = [
        {"open": 100.00, "high": 100.05, "low": 99.70, "close": 99.92, "volume": 10},
        {"open": 99.92, "high": 99.97, "low": 99.55, "close": 99.80, "volume": 10},
        {"open": 99.80, "high": 99.84, "low": 99.40, "close": 99.66, "volume": 10},
        {"open": 99.66, "high": 99.70, "low": 99.20, "close": 99.48, "volume": 10},
        {"open": 99.48, "high": 99.52, "low": 98.95, "close": 99.22, "volume": 10},
        {"open": 99.22, "high": 99.28, "low": 98.68, "close": 99.00, "volume": 10},
        {"open": 99.00, "high": 99.05, "low": 98.55, "close": 98.82, "volume": 10},
        {"open": 98.82, "high": 98.88, "low": 98.40, "close": 98.65, "volume": 10},
        {"open": 98.65, "high": 98.72, "low": 98.28, "close": 98.51, "volume": 10},
        {"open": 98.51, "high": 98.58, "low": 98.16, "close": 98.40, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_neutral_down_continuation_balanced"](candles, regime)
    assert result["should_trade"] is False
    assert "medium_neutral_down_continuation" in result["reason"]


def test_medium_neutral_down_continuation_core_keeps_clean_midrange_case():
    candles = [
        {"open": 100.00, "high": 100.08, "low": 99.94, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.07, "low": 99.95, "close": 100.00, "volume": 10},
        {"open": 100.04, "high": 100.10, "low": 99.98, "close": 100.01, "volume": 10},
        {"open": 100.01, "high": 100.07, "low": 99.96, "close": 100.03, "volume": 10},
        {"open": 100.03, "high": 100.08, "low": 99.97, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.05, "low": 99.95, "close": 100.01, "volume": 10},
        {"open": 99.98, "high": 100.02, "low": 99.88, "close": 99.93, "volume": 10},
        {"open": 99.93, "high": 99.98, "low": 99.80, "close": 99.87, "volume": 10},
        {"open": 99.87, "high": 99.92, "low": 99.73, "close": 99.81, "volume": 10},
        {"open": 99.81, "high": 99.86, "low": 99.67, "close": 99.75, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_neutral_down_continuation_core"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert "medium_neutral_down_continuation_core" in result["reason"]


def test_medium_vol_branch_v2_selects_scored_continuation():
    candles = [
        {"open": 100.00, "high": 100.06, "low": 99.82, "close": 99.95, "volume": 10},
        {"open": 99.95, "high": 100.01, "low": 99.78, "close": 99.90, "volume": 10},
        {"open": 99.90, "high": 99.96, "low": 99.74, "close": 99.86, "volume": 10},
        {"open": 99.86, "high": 99.91, "low": 99.70, "close": 99.83, "volume": 10},
        {"open": 99.83, "high": 99.88, "low": 99.67, "close": 99.80, "volume": 10},
        {"open": 99.80, "high": 99.85, "low": 99.64, "close": 99.77, "volume": 10},
        {"open": 99.77, "high": 99.82, "low": 99.61, "close": 99.74, "volume": 10},
        {"open": 99.74, "high": 99.79, "low": 99.58, "close": 99.71, "volume": 10},
        {"open": 99.71, "high": 99.76, "low": 99.55, "close": 99.68, "volume": 10},
        {"open": 99.68, "high": 99.73, "low": 99.52, "close": 99.65, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_vol_branch_v2"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert "medium_vol_branch_v2_score_" in result["reason"]


def test_medium_vol_branch_v2_skips_non_medium_vol():
    candles = [
        {"open": 100.00, "high": 100.06, "low": 99.82, "close": 99.95, "volume": 10},
        {"open": 99.95, "high": 100.01, "low": 99.78, "close": 99.90, "volume": 10},
        {"open": 99.90, "high": 99.96, "low": 99.74, "close": 99.86, "volume": 10},
        {"open": 99.86, "high": 99.91, "low": 99.70, "close": 99.83, "volume": 10},
        {"open": 99.83, "high": 99.88, "low": 99.67, "close": 99.80, "volume": 10},
        {"open": 99.80, "high": 99.85, "low": 99.64, "close": 99.77, "volume": 10},
        {"open": 99.77, "high": 99.82, "low": 99.61, "close": 99.74, "volume": 10},
        {"open": 99.74, "high": 99.79, "low": 99.58, "close": 99.71, "volume": 10},
        {"open": 99.71, "high": 99.76, "low": 99.55, "close": 99.68, "volume": 10},
        {"open": 99.68, "high": 99.73, "low": 99.52, "close": 99.65, "volume": 10},
    ]
    regime = {"label": "LOW_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_vol_branch_v2"](candles, regime)
    assert result["should_trade"] is False
    assert "medium_vol_branch_v2" in result["reason"]


def test_medium_vol_branch_v3_uses_balanced_continuation_leg():
    candles = [
        {"open": 100.00, "high": 100.08, "low": 99.94, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.07, "low": 99.95, "close": 100.00, "volume": 10},
        {"open": 100.04, "high": 100.10, "low": 99.98, "close": 100.01, "volume": 10},
        {"open": 100.01, "high": 100.07, "low": 99.96, "close": 100.03, "volume": 10},
        {"open": 100.03, "high": 100.08, "low": 99.97, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.05, "low": 99.95, "close": 100.01, "volume": 10},
        {"open": 99.98, "high": 100.02, "low": 99.88, "close": 99.93, "volume": 10},
        {"open": 99.93, "high": 99.98, "low": 99.80, "close": 99.87, "volume": 10},
        {"open": 99.87, "high": 99.92, "low": 99.73, "close": 99.81, "volume": 10},
        {"open": 99.81, "high": 99.86, "low": 99.67, "close": 99.75, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["medium_vol_branch_v3"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert "medium_neutral_down_continuation_balanced" in result["reason"]


def test_baseline_router_v1_routes_by_volatility_branch():
    low_up = [
        {"open": 100.00, "high": 100.08, "low": 99.94, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.07, "low": 99.95, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.09, "low": 99.96, "close": 100.03, "volume": 10},
        {"open": 100.03, "high": 100.08, "low": 99.97, "close": 100.01, "volume": 10},
        {"open": 100.01, "high": 100.10, "low": 99.98, "close": 100.04, "volume": 10},
        {"open": 100.04, "high": 100.09, "low": 99.99, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.11, "low": 99.99, "close": 100.05, "volume": 10},
        {"open": 100.05, "high": 100.08, "low": 99.98, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.03, "low": 99.94, "close": 99.98, "volume": 10},
        {"open": 99.98, "high": 100.10, "low": 99.95, "close": 100.06, "volume": 10},
    ]
    low_regime = {"label": "LOW_VOL / TRENDING", "is_mean_reverting": False}
    high_regime = {"label": "HIGH_VOL / TRENDING", "is_mean_reverting": False}

    low_signal = available_rules()["baseline_router_v1"](low_up, low_regime)
    high_signal = available_rules()["baseline_router_v1"](low_up, high_regime)
    assert low_signal["should_trade"] is True
    assert low_signal["direction"] == "UP"
    assert high_signal["should_trade"] is False


def test_baseline_router_v2_uses_medium_vol_branch_v3():
    candles = [
        {"open": 100.00, "high": 100.08, "low": 99.94, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.07, "low": 99.95, "close": 100.00, "volume": 10},
        {"open": 100.04, "high": 100.10, "low": 99.98, "close": 100.01, "volume": 10},
        {"open": 100.01, "high": 100.07, "low": 99.96, "close": 100.03, "volume": 10},
        {"open": 100.03, "high": 100.08, "low": 99.97, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.05, "low": 99.95, "close": 100.01, "volume": 10},
        {"open": 99.98, "high": 100.02, "low": 99.88, "close": 99.93, "volume": 10},
        {"open": 99.93, "high": 99.98, "low": 99.80, "close": 99.87, "volume": 10},
        {"open": 99.87, "high": 99.92, "low": 99.73, "close": 99.81, "volume": 10},
        {"open": 99.81, "high": 99.86, "low": 99.67, "close": 99.75, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["baseline_router_v2"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert "medium_neutral_down_continuation_balanced" in result["reason"]


def test_baseline_router_v2_candidate_filter_strips_estimate_but_keeps_direction():
    candles = [
        {"open": 100.00, "high": 100.08, "low": 99.94, "close": 100.02, "volume": 10},
        {"open": 100.02, "high": 100.07, "low": 99.95, "close": 100.00, "volume": 10},
        {"open": 100.04, "high": 100.10, "low": 99.98, "close": 100.01, "volume": 10},
        {"open": 100.01, "high": 100.07, "low": 99.96, "close": 100.03, "volume": 10},
        {"open": 100.03, "high": 100.08, "low": 99.97, "close": 100.00, "volume": 10},
        {"open": 100.00, "high": 100.05, "low": 99.95, "close": 100.01, "volume": 10},
        {"open": 99.98, "high": 100.02, "low": 99.88, "close": 99.93, "volume": 10},
        {"open": 99.93, "high": 99.98, "low": 99.80, "close": 99.87, "volume": 10},
        {"open": 99.87, "high": 99.92, "low": 99.73, "close": 99.81, "volume": 10},
        {"open": 99.81, "high": 99.86, "low": 99.67, "close": 99.75, "volume": 10},
    ]
    regime = {"label": "MEDIUM_VOL / NEUTRAL", "is_mean_reverting": False}
    result = available_rules()["baseline_router_v2_candidate_filter"](candles, regime)
    assert result["should_trade"] is True
    assert result["direction"] == "DOWN"
    assert result["estimate"] == 0.5
    assert result["meta"]["estimate_ignored"] is True
    assert result["meta"]["branch_name"] == "medium_neutral_down_continuation"
    assert result["meta"]["exact_rule_name"] == "medium_neutral_down_continuation_balanced"
    assert result["meta"]["has_continuation"] is True
    assert result["meta"]["direction"] == "DOWN"
    assert "baseline_router_v2_candidate_filter" in result["reason"]
