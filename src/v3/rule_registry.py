"""
Rule registry and profile definitions for deterministic v6 strategies.

This module is metadata-first: production routing can resolve named profiles
without changing the legacy `available_rules()` API used by backtests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from v3.rule_variants import available_rules, load_dynamic_coach_rule_metadata

RuleCategory = Literal["baseline", "branch", "overlay", "atomic_filter", "coach_candidate"]
RuleStatus = Literal["production", "candidate", "research", "shadow"]

PRODUCTION_CURRENT_RULES = [
    "baseline_router_v2",
    "baseline_router_v1_plus_sparse_combo",
    "baseline_current",
]

BASELINE_RULES = {
    "baseline_current",
    "baseline_v2_lvn_alpha2",
    "baseline_v2_lvn_alpha3",
    "baseline_v3_spike_reversal",
    "baseline_v3_momentum_shape",
    "baseline_v3_reversal_core",
    "baseline_v4_window_state",
    "baseline_v4_window_state_w6",
    "baseline_v5_clean_continuation",
    "baseline_v6_broad_shape",
    "baseline_router_v1",
    "baseline_router_v2",
}

BRANCH_RULES = {
    "low_vol_branch_v1",
    "medium_vol_branch_v1",
    "medium_vol_branch_v2",
    "medium_vol_branch_v3",
    "high_vol_branch_v1",
}

OVERLAY_RULES = {
    "baseline_router_v1_plus_lvn_alpha3",
    "baseline_router_v1_plus_v4",
    "baseline_router_v1_plus_sparse_combo",
    "candidate_lvn_up_volume_spike",
    "candidate_lvn_up_volume_spike_streak4p",
    "only_low_vol_trending",
    "only_low_vol_neutral",
    "only_low_vol",
    "only_lvn_up",
    "only_lvn_up_compression",
    "only_lvn_volume_spike",
    "only_lvn_up_volume_spike",
    "only_lvn_up_volume_spike_streak4p",
    "only_lvn_up_pure_volume_spike",
}

VOLUME_REQUIRED_TOKENS = {
    "volume",
    "volume_spike",
    "lvn",
    "baseline_v2",
    "only_lvn",
    "allow_trending_volume_spike",
    "raise_volume_confirmation",
    "allow_volume_spike_continuation",
}


@dataclass(frozen=True)
class RuleSpec:
    name: str
    category: RuleCategory
    status: RuleStatus
    family: str
    source: str
    requires_volume: bool = False
    production_allowed: bool = False
    shadow_allowed: bool = True
    description: str = ""


@dataclass(frozen=True)
class RuleProfile:
    name: str
    rule_names: tuple[str, ...]
    mode: Literal["first_trade", "all_rules"] = "first_trade"
    description: str = ""


@dataclass(frozen=True)
class RuleEvaluation:
    rule_name: str
    signal: dict[str, Any]
    error: str | None = None


@dataclass(frozen=True)
class ProfileResult:
    profile_name: str
    selected: dict[str, Any]
    evaluations: tuple[RuleEvaluation, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)


def get_rule_specs() -> dict[str, RuleSpec]:
    rules = available_rules()
    coach_metadata = load_dynamic_coach_rule_metadata()
    specs: dict[str, RuleSpec] = {}
    for name in sorted(rules):
        if name in coach_metadata:
            spec = coach_metadata[name]
            specs[name] = RuleSpec(
                name=name,
                category="coach_candidate",
                status="shadow",
                family=str(spec.get("family") or "coach"),
                source="coach_rule_candidate_specs",
                requires_volume=_requires_volume(name, spec),
                production_allowed=False,
                shadow_allowed=True,
                description=str(spec.get("spec_label") or name),
            )
            continue

        category = _static_category(name)
        specs[name] = RuleSpec(
            name=name,
            category=category,
            status="production" if name in PRODUCTION_CURRENT_RULES else "candidate",
            family=_static_family(name, category),
            source="static",
            requires_volume=_requires_volume(name, None),
            production_allowed=name in PRODUCTION_CURRENT_RULES,
            shadow_allowed=True,
            description=name.replace("_", " "),
        )
    return specs


def get_profiles() -> dict[str, RuleProfile]:
    specs = get_rule_specs()
    coach_rules = tuple(
        name
        for name, spec in specs.items()
        if spec.category == "coach_candidate" and spec.shadow_allowed
    )
    static_research = tuple(
        name
        for name, spec in specs.items()
        if spec.source == "static" and spec.shadow_allowed
    )
    return {
        "production_current": RuleProfile(
            name="production_current",
            rule_names=tuple(PRODUCTION_CURRENT_RULES),
            mode="first_trade",
            description="Current production alpha router rules.",
        ),
        "shadow_coach_candidates": RuleProfile(
            name="shadow_coach_candidates",
            rule_names=coach_rules,
            mode="all_rules",
            description="Eligible dynamic coach candidates observed in shadow only.",
        ),
        "research_all_static": RuleProfile(
            name="research_all_static",
            rule_names=static_research,
            mode="all_rules",
            description="All static deterministic rules for research dashboards.",
        ),
        "absorption_candidates_live": RuleProfile(
            name="absorption_candidates_live",
            rule_names=tuple(dict.fromkeys((*static_research, *coach_rules))),
            mode="all_rules",
            description="All rule absorption candidates observed against live realtime markets.",
        ),
        "v6_integrated_candidate": RuleProfile(
            name="v6_integrated_candidate",
            rule_names=tuple(PRODUCTION_CURRENT_RULES),
            mode="first_trade",
            description="Reserved integrated v6 candidate profile; starts pinned to current production.",
        ),
    }


def get_profile(name: str) -> RuleProfile:
    profiles = get_profiles()
    if name not in profiles:
        raise KeyError(f"Unknown rule profile: {name}")
    return profiles[name]


def resolve_profile_rules(profile_name: str) -> list[str]:
    return list(get_profile(profile_name).rule_names)


def run_rule_profile(
    candles: list[dict[str, Any]],
    regime: dict[str, Any],
    profile_name: str,
) -> ProfileResult:
    profile = get_profile(profile_name)
    rules = available_rules()
    evaluations: list[RuleEvaluation] = []
    errors: list[str] = []
    selected: dict[str, Any] | None = None

    for rule_name in profile.rule_names:
        rule = rules.get(rule_name)
        if rule is None:
            errors.append(f"{rule_name}:missing")
            evaluations.append(RuleEvaluation(rule_name=rule_name, signal={}, error="missing"))
            continue
        try:
            signal = _normalize_signal(rule(candles, regime), rule_name)
        except Exception as exc:
            error = str(exc)
            errors.append(f"{rule_name}:{error}")
            evaluations.append(RuleEvaluation(rule_name=rule_name, signal={}, error=error))
            continue
        evaluations.append(RuleEvaluation(rule_name=rule_name, signal=signal))
        if selected is None and signal.get("should_trade"):
            selected = signal
            if profile.mode == "first_trade":
                break

    if selected is None:
        selected = _no_trade_signal(profile_name, errors)
    elif errors:
        selected = dict(selected)
        selected["reason"] = f"{selected['reason']} | router_warnings={';'.join(errors)}"

    return ProfileResult(
        profile_name=profile_name,
        selected=selected,
        evaluations=tuple(evaluations),
        errors=tuple(errors),
    )


def _normalize_signal(signal: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    normalized = dict(signal)
    normalized["estimate"] = min(max(float(normalized.get("estimate", 0.5)), 0.0), 1.0)
    normalized["should_trade"] = bool(normalized.get("should_trade", False))
    normalized["conviction_score"] = int(normalized.get("conviction_score", 0) or 0)
    normalized["confidence"] = str(normalized.get("confidence", "low") or "low")
    normalized["reason"] = str(normalized.get("reason", "") or strategy_name)
    normalized["strategy_name"] = strategy_name
    meta = normalized.get("meta")
    normalized["meta"] = dict(meta) if isinstance(meta, dict) else {}
    normalized["meta"]["strategy_name"] = strategy_name
    normalized["meta"]["rule_profile_ready"] = True
    return normalized


def _no_trade_signal(profile_name: str, errors: list[str]) -> dict[str, Any]:
    reason_parts = [f"{profile_name}_no_trade"]
    if errors:
        reason_parts.append(f"router_warnings={';'.join(errors)}")
    return {
        "estimate": 0.5,
        "should_trade": False,
        "direction": None,
        "confidence": "low",
        "conviction_score": 0,
        "reason": " | ".join(reason_parts),
        "strategy_name": f"{profile_name}_no_trade",
        "meta": {"strategy_name": f"{profile_name}_no_trade", "rule_profile": profile_name},
    }


def _static_category(name: str) -> RuleCategory:
    if name in BASELINE_RULES:
        return "baseline"
    if name in BRANCH_RULES:
        return "branch"
    if name in OVERLAY_RULES:
        return "overlay"
    return "atomic_filter"


def _static_family(name: str, category: RuleCategory) -> str:
    if name.startswith("baseline_router"):
        return "router"
    if name.startswith("baseline_v"):
        return name.rsplit("_", 1)[0]
    if "reversal" in name or "bounce" in name:
        return "reversal"
    if "continuation" in name:
        return "continuation"
    if category == "branch":
        return "regime_branch"
    if category == "overlay":
        return "overlay"
    return "filter"


def _requires_volume(name: str, spec: dict[str, Any] | None) -> bool:
    haystack = name
    if spec is not None:
        haystack = " ".join(
            [
                name,
                str(spec.get("family", "")),
                str(spec.get("template_action", "")),
                str(spec.get("config", "")),
            ]
        )
    return any(token in haystack for token in VOLUME_REQUIRED_TOKENS)
