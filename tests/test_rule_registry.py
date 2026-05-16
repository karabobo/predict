import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_rule_registry_covers_available_rules():
    from v3.rule_registry import get_rule_specs
    from v3.rule_variants import available_rules

    specs = get_rule_specs()
    rules = available_rules()

    assert set(specs) == set(rules)
    assert all(spec.category in {"baseline", "branch", "overlay", "atomic_filter", "coach_candidate"} for spec in specs.values())


def test_production_current_profile_matches_live_default():
    from predict import DEFAULT_ALPHA_RULES, _configured_alpha_rules
    from v3.rule_registry import resolve_profile_rules

    expected = [part.strip() for part in DEFAULT_ALPHA_RULES.split(",") if part.strip()]

    assert resolve_profile_rules("production_current") == expected
    assert _configured_alpha_rules() == expected


def test_shadow_coach_profile_contains_only_coach_candidates():
    from v3.rule_registry import get_rule_specs, resolve_profile_rules

    specs = get_rule_specs()
    rules = resolve_profile_rules("shadow_coach_candidates")

    assert rules
    assert all(specs[name].category == "coach_candidate" for name in rules)
    assert all(specs[name].production_allowed is False for name in rules)


def test_absorption_live_profile_includes_static_and_coach_candidates():
    from v3.rule_registry import get_rule_specs, resolve_profile_rules

    specs = get_rule_specs()
    rules = resolve_profile_rules("absorption_candidates_live")

    assert "baseline_router_v2" in rules
    assert "baseline_router_v1_plus_sparse_combo" in rules
    assert any(specs[name].category == "coach_candidate" for name in rules)
    assert all(specs[name].shadow_allowed for name in rules)


def test_v8_integrated_candidate_profile_uses_l2_promoted_rules():
    from v3.rule_registry import resolve_profile_rules

    assert resolve_profile_rules("v8_integrated_candidate") == [
        "router_overlay_ensemble",
        "lvn_volume_scout",
        "momentum_shape_ensemble",
        "router_core",
        "reversal_shape_ensemble",
    ]


def test_v6_integrated_candidate_profile_is_compatibility_alias():
    from v3.rule_registry import resolve_profile_rules

    assert resolve_profile_rules("v6_integrated_candidate") == resolve_profile_rules("v8_integrated_candidate")


def test_v8_broad_paper_candidate_absorbs_baseline_and_volume_scouts():
    from v3.rule_registry import resolve_profile_rules

    rules = resolve_profile_rules("v8_broad_paper_candidate")

    assert rules[:5] == resolve_profile_rules("v8_integrated_candidate")
    assert "baseline_current" in rules
    assert "baseline_v2_lvn_alpha2" in rules
    assert "baseline_v6_broad_shape" in rules
    assert "baseline_router_v2_candidate_filter" in rules


def test_predict_rule_profile_env_accepts_v8(monkeypatch):
    from predict import _configured_alpha_rules
    from v3.rule_registry import resolve_profile_rules

    monkeypatch.delenv("PREDICT_ALPHA_RULES", raising=False)
    monkeypatch.setenv("PREDICT_RULE_PROFILE", "v8_integrated_candidate")

    assert _configured_alpha_rules() == resolve_profile_rules("v8_integrated_candidate")


def test_predict_alpha_rules_env_overrides_profile(monkeypatch):
    from predict import _configured_alpha_rules

    monkeypatch.setenv("PREDICT_RULE_PROFILE", "production_current")
    monkeypatch.setenv("PREDICT_ALPHA_RULES", "baseline_current")

    assert _configured_alpha_rules() == ["baseline_current"]
