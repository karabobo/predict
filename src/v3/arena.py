"""
v3.arena — Head-to-head research harness for baseline/challenger evaluation.

This module keeps research code aligned with the current production baseline.
Challengers are evaluated out-of-sample on blocked time-series folds before
they are allowed anywhere near production.
"""

from __future__ import annotations

import math
import os
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from src import prompts
from src.ai_client import client as ai_client
from src.btc_data import _compute_summary
from src.btc_data import format_for_prompt
from src.strategies.momentum import contrarian_signal as production_momentum_signal
from src.strategies.momentum import estimate_from_signal_features
from src.strategies.regime import compute_regime_from_candles as production_regime
from src.v3.backtest import build_synthetic_markets, candles_to_btc_format, download_historical_candles
from src.v3.backtest import contrarian_rule_predict as legacy_contrarian_predict
from src.v3.config import (
    MIN_EDGE,
    PROMOTION_MAX_DRAWDOWN_WORSENING,
    PROMOTION_MIN_FOLD_PASS_RATE,
    PROMOTION_MIN_ROI_DELTA,
    PROMOTION_MIN_TRADE_RATIO,
    PROMOTION_MIN_WIN_RATE_DELTA,
    ROUND_TRIP_FEE,
)
from src.v3.features import compute_features
from src.v3.probability_foundation import Paper5MModelService, ProbabilityFoundationService
from src.v3.regime import compute_regime as v3_regime
from src.v3.rule_variants import available_rules as available_rule_variants


@dataclass(frozen=True)
class ResearchContext:
    market: dict[str, Any]
    formatted_candles: list[dict[str, Any]]
    btc_summary: dict[str, Any]
    production_regime: dict[str, Any]
    v3_regime: dict[str, Any]
    features: dict[str, Any]


@dataclass(frozen=True)
class ResearchDecision:
    prob_up: float
    should_trade: bool
    reason: str
    conviction_score: int = 0
    confidence: str = "low"


@dataclass(frozen=True)
class PromotionGate:
    min_total_roi_delta: float = PROMOTION_MIN_ROI_DELTA
    min_total_win_rate_delta: float = PROMOTION_MIN_WIN_RATE_DELTA
    min_trade_ratio: float = PROMOTION_MIN_TRADE_RATIO
    max_drawdown_worsening: float = PROMOTION_MAX_DRAWDOWN_WORSENING
    min_fold_pass_rate: float = PROMOTION_MIN_FOLD_PASS_RATE


class BaseContender:
    name = "base"

    def reset(self) -> None:
        pass

    def bootstrap(self, history: list[ResearchContext]) -> None:
        pass

    def predict(self, context: ResearchContext) -> ResearchDecision:
        raise NotImplementedError

    def observe(self, context: ResearchContext) -> None:
        pass

    def report_metadata(self) -> dict[str, Any]:
        return {}


class ProductionBaselineContender(BaseContender):
    name = "production_baseline"

    def predict(self, context: ResearchContext) -> ResearchDecision:
        signal = production_momentum_signal(
            context.formatted_candles,
            regime_label=context.production_regime["label"],
        )
        if context.production_regime["is_mean_reverting"]:
            signal = _skip_decision(f"{signal.get('reason', 'signal')} | regime_filter")
        return ResearchDecision(
            prob_up=float(signal.get("estimate", 0.5)),
            should_trade=bool(signal.get("should_trade", False)),
            reason=str(signal.get("reason", "")),
            conviction_score=int(signal.get("conviction_score", 0)),
            confidence=str(signal.get("confidence", "low")),
        )


class LegacyContrarianContender(BaseContender):
    name = "legacy_contrarian"

    def predict(self, context: ResearchContext) -> ResearchDecision:
        prob_up, should_trade = legacy_contrarian_predict(context.features)
        return _deterministic_decision(prob_up, should_trade, self.name)


class LegacyRegimeFilteredContender(BaseContender):
    name = "legacy_regime_filtered"

    def predict(self, context: ResearchContext) -> ResearchDecision:
        if context.v3_regime["autocorrelation"] < -0.15:
            return _deterministic_decision(0.5, False, f"{self.name}:regime_skip")
        prob_up, should_trade = legacy_contrarian_predict(context.features)
        return _deterministic_decision(prob_up, should_trade, self.name)


class LegacyEnhancedContender(BaseContender):
    name = "legacy_enhanced"

    def predict(self, context: ResearchContext) -> ResearchDecision:
        if context.v3_regime["autocorrelation"] < -0.15:
            return _deterministic_decision(0.5, False, f"{self.name}:regime_skip")

        streak = context.features.get("consecutive_streak", 0)
        if abs(streak) < 3:
            return _deterministic_decision(0.5, False, f"{self.name}:streak_too_short")

        exhaustion_count = 0
        if context.features.get("compression", 0) > 0 or context.features.get("range_ratio", 1.0) < 0.7:
            exhaustion_count += 1

        wick_upper = context.features.get("wick_upper_ratio", 0.0)
        wick_lower = context.features.get("wick_lower_ratio", 0.0)
        if streak >= 3 and wick_upper > 1.8:
            exhaustion_count += 1
        elif streak <= -3 and wick_lower > 1.8:
            exhaustion_count += 1

        if context.features.get("volume_ratio", 1.0) > 1.8:
            exhaustion_count += 1

        if exhaustion_count < 2:
            return _deterministic_decision(0.5, False, f"{self.name}:need_2_of_3")

        prob_up = estimate_from_signal_features(
            last_direction="DOWN" if streak >= 3 else "UP",
            streak=abs(streak),
            compression=context.features.get("compression", 0) > 0 or context.features.get("range_ratio", 1.0) < 0.7,
            volume_spike=context.features.get("volume_ratio", 1.0) > 1.8,
        )
        return _deterministic_decision(prob_up, True, self.name)


class V3MLContender(BaseContender):
    name = "v3_ml"

    def __init__(self, retrain_every: int = 50):
        self.retrain_every = retrain_every
        self._model = None

    def reset(self) -> None:
        from src.v3.model import V3Model

        self._model = V3Model(retrain_every=self.retrain_every)

    def bootstrap(self, history: list[ResearchContext]) -> None:
        if self._model is None:
            self.reset()
        for context in history:
            self._model.add_training_sample(context.features, context.market["outcome"])
        if len(history) >= 100:
            self._model.train()

    def predict(self, context: ResearchContext) -> ResearchDecision:
        if self._model is None:
            self.reset()
        prob_up, should_trade = self._model.predict(context.features)
        return _deterministic_decision(prob_up, should_trade, self.name)

    def observe(self, context: ResearchContext) -> None:
        if self._model is None:
            self.reset()
        self._model.add_training_sample(context.features, context.market["outcome"])
        if self._model.should_retrain():
            self._model.train()


class DeepSeekV3Contender(BaseContender):
    name = "deepseek_v3"

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or os.getenv(
            "SILICON_FLOW_RESEARCH_MODEL",
            "deepseek-ai/DeepSeek-V3",
        )

    def predict(self, context: ResearchContext) -> ResearchDecision:
        user_prompt = _build_llm_user_prompt(context)
        result = ai_client.predict(
            self.model_name,
            prompts.SYSTEM_PROMPT,
            user_prompt,
            coach_mode=False,
        )
        if not result or "error" in result:
            error_text = result.get("error", "unknown_error") if isinstance(result, dict) else "unknown_error"
            return _deterministic_decision(0.5, False, f"{self.name}:error:{error_text}")

        prob_up = _normalize_probability(result.get("estimate", 0.5))
        confidence_raw = _normalize_confidence(result.get("confidence", 0))
        conviction = max(0, min(5, confidence_raw))
        should_trade = conviction >= 3 and abs(prob_up - 0.5) >= 0.03
        reasoning = str(result.get("reasoning") or result.get("reason") or self.name)
        confidence = "high" if conviction >= 4 else "medium" if conviction >= 3 else "low"
        return ResearchDecision(
            prob_up=prob_up,
            should_trade=should_trade,
            reason=reasoning,
            conviction_score=conviction,
            confidence=confidence,
        )


class RuleVariantContender(BaseContender):
    rule_name = "rule_variant"

    def __init__(self) -> None:
        rules = available_rule_variants()
        if self.rule_name not in rules:
            raise ValueError(f"Unknown rule variant contender: {self.rule_name}")
        self._rule = rules[self.rule_name]

    def predict(self, context: ResearchContext) -> ResearchDecision:
        signal = self._rule(context.formatted_candles, context.production_regime)
        return ResearchDecision(
            prob_up=float(signal.get("estimate", 0.5)),
            should_trade=bool(signal.get("should_trade", False)),
            reason=str(signal.get("reason", self.rule_name)),
            conviction_score=int(signal.get("conviction_score", 0)),
            confidence=str(signal.get("confidence", "low")),
        )


class CandidateLVNUpVolumeSpikeStreak4PContender(RuleVariantContender):
    name = "candidate_lvn_up_volume_spike_streak4p"
    rule_name = "only_lvn_up_volume_spike_streak4p"


class BaselineV4WindowStateContender(RuleVariantContender):
    name = "baseline_v4_window_state"
    rule_name = "baseline_v4_window_state"


class BaselineRouterV1Contender(RuleVariantContender):
    name = "baseline_router_v1"
    rule_name = "baseline_router_v1"


class BaselineRouterV2Contender(RuleVariantContender):
    name = "baseline_router_v2"
    rule_name = "baseline_router_v2"


class V6FoundationContender(BaseContender):
    name = "v6_foundation"
    candidate_filter_name = "baseline_router_v2_candidate_filter"
    require_agreement = False

    def __init__(self) -> None:
        rules = available_rule_variants()
        if self.candidate_filter_name not in rules:
            raise ValueError(f"Unknown rule variant contender: {self.candidate_filter_name}")
        self._rule = rules[self.candidate_filter_name]
        self._foundation = ProbabilityFoundationService()

    def reset(self) -> None:
        self._foundation = ProbabilityFoundationService()

    def bootstrap(self, history: list[ResearchContext]) -> None:
        self._foundation.fit(history, signal_provider=self._rule)

    def predict(self, context: ResearchContext) -> ResearchDecision:
        signal = self._rule(context.formatted_candles, context.production_regime)
        if not signal.get("should_trade", False):
            return ResearchDecision(
                prob_up=0.5,
                should_trade=False,
                reason=str(signal.get("reason", self.candidate_filter_name)),
                conviction_score=int(signal.get("conviction_score", 0)),
                confidence=str(signal.get("confidence", "low")),
            )

        prediction = self._foundation.predict(
            context,
            candidate_signal=signal,
            require_agreement=self.require_agreement,
        )
        signal_direction = str(signal.get("direction", "SKIP") or "SKIP")
        predicted_direction = "UP" if prediction.prob_up > 0.5 else "DOWN" if prediction.prob_up < 0.5 else "SKIP"
        direction_match = signal_direction == predicted_direction
        should_trade = bool(signal.get("should_trade", False)) and direction_match
        if self.require_agreement:
            should_trade = should_trade and prediction.agreement_passed

        reason_suffix = (
            f"{self.name} | model={prediction.model_name}"
            if direction_match else
            f"{self.name} | direction_mismatch signal={signal_direction} model={predicted_direction}"
        )
        if self.require_agreement and not prediction.agreement_passed:
            reason_suffix = f"{reason_suffix} | agreement_failed"

        return ResearchDecision(
            prob_up=float(prediction.prob_up),
            should_trade=should_trade,
            reason=f"{signal.get('reason', self.candidate_filter_name)} | {reason_suffix}",
            conviction_score=int(signal.get("conviction_score", 0)),
            confidence=str(signal.get("confidence", "low")),
        )

    def report_metadata(self) -> dict[str, Any]:
        summary = self._foundation.summary
        if summary is None:
            return {"foundation_status": "not_bootstrapped"}
        return {
            "foundation_version": "v6",
            "candidate_filter_name": self.candidate_filter_name,
            "foundation_status": "trained" if self._foundation.is_trained else "untrained",
            "train_samples": summary.train_samples,
            "calibration_samples": summary.calibration_samples,
            "primary_model_name": summary.primary_model_name,
            "secondary_model_name": summary.secondary_model_name,
            "calibrated": summary.calibrated,
            "diagnostics": dict(summary.diagnostics),
        }


class V6FoundationAgreementContender(V6FoundationContender):
    name = "v6_foundation_agreement"
    candidate_filter_name = "baseline_router_v2_candidate_filter"
    require_agreement = True


class FoundationRouterV2Contender(V6FoundationContender):
    name = "foundation_router_v2"
    candidate_filter_name = "baseline_router_v2_candidate_filter"
    require_agreement = False


class FoundationRouterV2AgreementContender(V6FoundationAgreementContender):
    name = "foundation_router_v2_agreement"
    candidate_filter_name = "baseline_router_v2_candidate_filter"


class Paper5MXGBoostContender(BaseContender):
    name = "paper_xgb_5m"
    model_kind = "xgboost"
    feature_set = "derived"
    use_calibration = True

    def __init__(self) -> None:
        self._model = Paper5MModelService(
            model_kind=self.model_kind,
            feature_set=self.feature_set,
            use_calibration=self.use_calibration,
        )

    def reset(self) -> None:
        self._model = Paper5MModelService(
            model_kind=self.model_kind,
            feature_set=self.feature_set,
            use_calibration=self.use_calibration,
        )

    def bootstrap(self, history: list[ResearchContext]) -> None:
        self._model.fit(history)

    def predict(self, context: ResearchContext) -> ResearchDecision:
        prediction = self._model.predict(context)
        return ResearchDecision(
            prob_up=float(prediction.prob_up),
            should_trade=True,
            reason=f"{self.name} | model={prediction.model_name}",
            conviction_score=3 if abs(prediction.prob_up - 0.5) >= 0.03 else 2,
            confidence="high" if abs(prediction.prob_up - 0.5) >= 0.08 else "medium",
        )

    def report_metadata(self) -> dict[str, Any]:
        summary = self._model.summary
        if summary is None:
            return {"paper_5m_status": "not_bootstrapped", "paper_5m_model_kind": self.model_kind}
        return {
            "paper_5m_status": "trained" if self._model.is_trained else "untrained",
            "paper_5m_model_kind": self.model_kind,
            "train_samples": summary.train_samples,
            "calibration_samples": summary.calibration_samples,
            "primary_model_name": summary.primary_model_name,
            "calibrated": summary.calibrated,
            "diagnostics": dict(summary.diagnostics),
        }


class Paper5MLogRegContender(Paper5MXGBoostContender):
    name = "paper_logreg_5m"
    model_kind = "logreg"


class Paper5MRawXGBoostContender(Paper5MXGBoostContender):
    name = "paper_xgb_5m_raw"
    model_kind = "xgboost"
    feature_set = "raw"
    use_calibration = False


class Paper5MRawLogRegContender(Paper5MXGBoostContender):
    name = "paper_logreg_5m_raw"
    model_kind = "logreg"
    feature_set = "raw"
    use_calibration = False


def contender_factories() -> dict[str, Callable[[], BaseContender]]:
    return {
        ProductionBaselineContender.name: ProductionBaselineContender,
        LegacyContrarianContender.name: LegacyContrarianContender,
        LegacyRegimeFilteredContender.name: LegacyRegimeFilteredContender,
        LegacyEnhancedContender.name: LegacyEnhancedContender,
        V3MLContender.name: V3MLContender,
        DeepSeekV3Contender.name: DeepSeekV3Contender,
        CandidateLVNUpVolumeSpikeStreak4PContender.name: CandidateLVNUpVolumeSpikeStreak4PContender,
        BaselineV4WindowStateContender.name: BaselineV4WindowStateContender,
        BaselineRouterV1Contender.name: BaselineRouterV1Contender,
        BaselineRouterV2Contender.name: BaselineRouterV2Contender,
        V6FoundationContender.name: V6FoundationContender,
        V6FoundationAgreementContender.name: V6FoundationAgreementContender,
        FoundationRouterV2Contender.name: FoundationRouterV2Contender,
        FoundationRouterV2AgreementContender.name: FoundationRouterV2AgreementContender,
        Paper5MXGBoostContender.name: Paper5MXGBoostContender,
        Paper5MLogRegContender.name: Paper5MLogRegContender,
        Paper5MRawXGBoostContender.name: Paper5MRawXGBoostContender,
        Paper5MRawLogRegContender.name: Paper5MRawLogRegContender,
    }


def prepare_market_contexts(markets: list[dict[str, Any]]) -> list[ResearchContext]:
    contexts: list[ResearchContext] = []
    for market in markets:
        formatted_candles = candles_to_btc_format(market["context_candles"])
        btc_summary = _compute_summary(formatted_candles)
        prod_regime = production_regime(formatted_candles)
        quant_regime = v3_regime(btc_summary)
        features = compute_features(
            btc_summary,
            {
                "midpoint": market["implied_price_yes"],
                "spread_pct": 0.02,
                "depth_imbalance": 0.0,
                "bid_depth_5pct": 2000,
                "ask_depth_5pct": 2000,
            },
            {
                "end_date": datetime.fromtimestamp(
                    market["timestamp"],
                    tz=timezone.utc,
                ).isoformat(),
                "price_yes": market["implied_price_yes"],
            },
            quant_regime,
        )
        features["autocorrelation"] = quant_regime["autocorrelation"]
        features["volatility_state_val"] = quant_regime["volatility_state"]

        contexts.append(
            ResearchContext(
                market=market,
                formatted_candles=formatted_candles,
                btc_summary=btc_summary,
                production_regime=prod_regime,
                v3_regime=quant_regime,
                features=features,
            )
        )
    return contexts


def build_research_dataset(days: int, lookback: int = 20, candles_file: str | None = None) -> dict[str, Any]:
    from datetime import datetime, timedelta, timezone

    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)
    if candles_file:
        candles = _load_local_candles(Path(candles_file), start_date, end_date)
    else:
        candles = download_historical_candles(start_date, end_date)
    markets = build_synthetic_markets(candles, lookback=lookback)
    contexts = prepare_market_contexts(markets)
    return {
        "start_date": start_date,
        "end_date": end_date,
        "candles": candles,
        "markets": markets,
        "contexts": contexts,
    }


def _load_local_candles(path: Path, start_date: datetime, end_date: datetime) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix.lower() == ".csv":
        frame = pd.read_csv(path)
    else:
        raise ValueError(f"Unsupported candles file: {path.suffix}")

    cols = {c.lower(): c for c in frame.columns}
    ts_col = next((cols[c] for c in ("ts", "timestamp", "time", "datetime") if c in cols), None)
    if ts_col is None:
        raise ValueError("Candles file must include ts/timestamp/time column")

    def to_ts(value: Any) -> int:
        if isinstance(value, pd.Timestamp):
            if value.tzinfo is None:
                value = value.tz_localize("UTC")
            return int(value.timestamp())
        text = str(value)
        try:
            return int(float(text))
        except ValueError:
            parsed = pd.Timestamp(text)
            if parsed.tzinfo is None:
                parsed = parsed.tz_localize("UTC")
            return int(parsed.timestamp())

    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())
    candles: list[dict[str, Any]] = []
    for record in frame.to_dict(orient="records"):
        ts = to_ts(record[ts_col])
        if ts < start_ts or ts > end_ts:
            continue
        candles.append(
            {
                "timestamp": ts,
                "open": float(record[cols.get("open", "open")]),
                "high": float(record[cols.get("high", "high")]),
                "low": float(record[cols.get("low", "low")]),
                "close": float(record[cols.get("close", "close")]),
                "volume": float(record.get(cols.get("volume", "volume"), 0.0) or 0.0),
            }
        )
    candles.sort(key=lambda row: row["timestamp"])
    return candles


def evaluate_head_to_head(
    contexts: list[ResearchContext],
    *,
    baseline_name: str,
    challenger_name: str,
    warm_up: int,
    folds: int,
    bet_size: float,
    min_edge: float = MIN_EDGE,
    seed: int = 42,
    max_eval_contexts: int | None = None,
    gate: PromotionGate | None = None,
) -> dict[str, Any]:
    factories = contender_factories()
    if baseline_name not in factories:
        raise ValueError(f"Unknown baseline contender: {baseline_name}")
    if challenger_name not in factories:
        raise ValueError(f"Unknown challenger contender: {challenger_name}")
    if baseline_name == challenger_name:
        raise ValueError("Baseline and challenger must be different")

    fold_ranges = build_blocked_folds(contexts, warm_up=warm_up, folds=folds)
    baseline_folds = []
    challenger_folds = []
    fold_comparisons = []

    for fold_index, (train_contexts, eval_contexts) in enumerate(fold_ranges):
        if max_eval_contexts is not None and max_eval_contexts > 0:
            eval_contexts = eval_contexts[:max_eval_contexts]
        baseline = factories[baseline_name]()
        challenger = factories[challenger_name]()

        baseline_result = evaluate_fold(
            contender=baseline,
            contender_name=baseline_name,
            train_contexts=train_contexts,
            eval_contexts=eval_contexts,
            fold_index=fold_index,
            bet_size=bet_size,
            min_edge=min_edge,
            seed=seed,
        )
        challenger_result = evaluate_fold(
            contender=challenger,
            contender_name=challenger_name,
            train_contexts=train_contexts,
            eval_contexts=eval_contexts,
            fold_index=fold_index,
            bet_size=bet_size,
            min_edge=min_edge,
            seed=seed,
        )

        baseline_folds.append(baseline_result)
        challenger_folds.append(challenger_result)
        fold_comparisons.append(compare_fold_results(baseline_result, challenger_result))

    baseline_summary = aggregate_fold_results(baseline_name, baseline_folds)
    challenger_summary = aggregate_fold_results(challenger_name, challenger_folds)
    gate_result = apply_promotion_gate(
        baseline_summary,
        challenger_summary,
        fold_comparisons,
        gate or PromotionGate(),
    )
    regime_findings = compare_regime_breakdowns(
        baseline_summary.get("regime_breakdown", {}),
        challenger_summary.get("regime_breakdown", {}),
    )

    return {
        "baseline": baseline_summary,
        "challenger": challenger_summary,
        "folds": fold_comparisons,
        "baseline_folds": baseline_folds,
        "challenger_folds": challenger_folds,
        "gate": gate_result,
        "regime_findings": regime_findings,
    }


def build_blocked_folds(
    contexts: list[ResearchContext],
    *,
    warm_up: int,
    folds: int,
) -> list[tuple[list[ResearchContext], list[ResearchContext]]]:
    if warm_up >= len(contexts):
        raise ValueError("warm_up must be smaller than the context set")

    eval_contexts = contexts[warm_up:]
    if not eval_contexts:
        raise ValueError("no evaluation contexts available after warm-up")

    fold_count = max(1, min(folds, len(eval_contexts)))
    fold_size = max(1, len(eval_contexts) // fold_count)
    ranges = []

    for fold_index in range(fold_count):
        start = warm_up + fold_index * fold_size
        end = warm_up + (fold_index + 1) * fold_size if fold_index < fold_count - 1 else len(contexts)
        train_contexts = contexts[:start]
        fold_eval = contexts[start:end]
        if not fold_eval:
            continue
        ranges.append((train_contexts, fold_eval))
    return ranges


def evaluate_fold(
    *,
    contender: BaseContender,
    contender_name: str,
    train_contexts: list[ResearchContext],
    eval_contexts: list[ResearchContext],
    fold_index: int,
    bet_size: float,
    min_edge: float,
    seed: int,
) -> dict[str, Any]:
    contender.reset()
    contender.bootstrap(train_contexts)

    trade_log = []
    signal_rows = []
    skipped_signals = 0
    edge_filtered = 0
    signal_calls = 0
    correct_calls = 0

    for context in eval_contexts:
        decision = contender.predict(context)
        prob_up = max(0.0, min(1.0, float(decision.prob_up)))
        midpoint = float(context.market["implied_price_yes"])
        outcome = int(context.market["outcome"])
        brier = (prob_up - outcome) ** 2
        market_brier = (midpoint - outcome) ** 2
        called = abs(prob_up - 0.5) > 1e-9
        correct_call = called and ((prob_up >= 0.5 and outcome == 1) or (prob_up < 0.5 and outcome == 0))
        signal_calls += 1 if called else 0
        correct_calls += 1 if correct_call else 0

        signal_rows.append({
            "index": context.market["index"],
            "prob_up": prob_up,
            "midpoint": midpoint,
            "outcome": outcome,
            "brier": brier,
            "market_brier": market_brier,
            "called": called,
            "correct_call": correct_call,
            "should_trade": decision.should_trade,
            "regime": context.production_regime["label"],
            "reason": decision.reason,
        })

        if not decision.should_trade:
            skipped_signals += 1
            contender.observe(context)
            continue

        edge = abs(prob_up - midpoint)
        slippage = deterministic_slippage(seed, fold_index, context.market["index"])
        net_edge = edge - ROUND_TRIP_FEE - slippage
        if net_edge < min_edge:
            edge_filtered += 1
            contender.observe(context)
            continue

        predicted_up = prob_up >= 0.5
        actual_up = outcome == 1
        correct = predicted_up == actual_up
        entry_price = midpoint if predicted_up else (1.0 - midpoint)
        entry_price = min(max(entry_price + slippage, 0.01), 0.99)
        gross_pnl = bet_size * (1.0 / entry_price - 1.0) if correct else -bet_size
        net_pnl = gross_pnl - (bet_size * ROUND_TRIP_FEE)

        trade_log.append({
            "index": context.market["index"],
            "timestamp": context.market["timestamp"],
            "prob_up": prob_up,
            "midpoint": midpoint,
            "edge": edge,
            "net_edge": net_edge,
            "predicted_up": predicted_up,
            "actual_up": actual_up,
            "correct": correct,
            "pnl": net_pnl,
            "entry_price": entry_price,
            "regime": context.production_regime["label"],
            "reason": decision.reason,
        })
        contender.observe(context)

    return summarize_fold_results(
        contender_name=contender_name,
        fold_index=fold_index,
        train_markets=len(train_contexts),
        eval_markets=len(eval_contexts),
        signal_rows=signal_rows,
        trade_log=trade_log,
        skipped_signals=skipped_signals,
        edge_filtered=edge_filtered,
        bet_size=bet_size,
        contender_metadata=contender.report_metadata(),
    )


def summarize_fold_results(
    *,
    contender_name: str,
    fold_index: int,
    train_markets: int,
    eval_markets: int,
    signal_rows: list[dict[str, Any]],
    trade_log: list[dict[str, Any]],
    skipped_signals: int,
    edge_filtered: int,
    bet_size: float,
    contender_metadata: dict[str, Any],
) -> dict[str, Any]:
    avg_brier = sum(row["brier"] for row in signal_rows) / len(signal_rows) if signal_rows else 0.0
    avg_vs_market = (
        sum(row["brier"] - row["market_brier"] for row in signal_rows) / len(signal_rows)
        if signal_rows else 0.0
    )
    called_markets = sum(1 for row in signal_rows if row["called"])
    correct_calls = sum(1 for row in signal_rows if row["correct_call"])

    total_pnl = sum(trade["pnl"] for trade in trade_log)
    total_wagered = len(trade_log) * bet_size
    max_drawdown = _max_drawdown_amount([trade["pnl"] for trade in trade_log])
    regime_breakdown = _summarize_regime_breakdown(signal_rows, trade_log, bet_size)

    return {
        "name": contender_name,
        "fold_index": fold_index,
        "train_markets": train_markets,
        "eval_markets": eval_markets,
        "signal_calls": called_markets,
        "correct_calls": correct_calls,
        "directional_accuracy": correct_calls / called_markets if called_markets else 0.0,
        "avg_brier": avg_brier,
        "avg_vs_market": avg_vs_market,
        "signal_trade_rate": sum(1 for row in signal_rows if row["should_trade"]) / eval_markets if eval_markets else 0.0,
        "executed_trade_rate": len(trade_log) / eval_markets if eval_markets else 0.0,
        "trades": len(trade_log),
        "skipped_signals": skipped_signals,
        "edge_filtered": edge_filtered,
        "wins": sum(1 for trade in trade_log if trade["correct"]),
        "losses": sum(1 for trade in trade_log if not trade["correct"]),
        "win_rate": sum(1 for trade in trade_log if trade["correct"]) / len(trade_log) if trade_log else 0.0,
        "pnl": total_pnl,
        "wagered": total_wagered,
        "roi": total_pnl / total_wagered * 100.0 if total_wagered > 0 else 0.0,
        "max_drawdown": max_drawdown,
        "regime_breakdown": regime_breakdown,
        "trade_log": trade_log,
        "contender_metadata": contender_metadata,
    }


def aggregate_fold_results(name: str, fold_results: list[dict[str, Any]]) -> dict[str, Any]:
    if not fold_results:
        return {
            "name": name,
            "folds": 0,
            "eval_markets": 0,
            "signal_calls": 0,
            "correct_calls": 0,
            "directional_accuracy": 0.0,
            "avg_brier": 0.0,
            "avg_vs_market": 0.0,
            "signal_trade_rate": 0.0,
            "executed_trade_rate": 0.0,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
        "pnl": 0.0,
        "wagered": 0.0,
        "roi": 0.0,
        "max_drawdown": 0.0,
        "regime_breakdown": {},
        "contender_metadata": {},
    }

    eval_markets = sum(result["eval_markets"] for result in fold_results)
    signal_calls = sum(result["signal_calls"] for result in fold_results)
    correct_calls = sum(result["correct_calls"] for result in fold_results)
    trades = sum(result["trades"] for result in fold_results)
    wins = sum(result["wins"] for result in fold_results)
    wagered = sum(result["wagered"] for result in fold_results)
    pnl = sum(result["pnl"] for result in fold_results)

    return {
        "name": name,
        "folds": len(fold_results),
        "eval_markets": eval_markets,
        "signal_calls": signal_calls,
        "correct_calls": correct_calls,
        "directional_accuracy": correct_calls / signal_calls if signal_calls else 0.0,
        "avg_brier": _weighted_mean(fold_results, "avg_brier", "eval_markets"),
        "avg_vs_market": _weighted_mean(fold_results, "avg_vs_market", "eval_markets"),
        "signal_trade_rate": _weighted_mean(fold_results, "signal_trade_rate", "eval_markets"),
        "executed_trade_rate": _weighted_mean(fold_results, "executed_trade_rate", "eval_markets"),
        "trades": trades,
        "wins": wins,
        "losses": trades - wins,
        "win_rate": wins / trades if trades else 0.0,
        "pnl": pnl,
        "wagered": wagered,
        "roi": pnl / wagered * 100.0 if wagered else 0.0,
        "max_drawdown": max((result["max_drawdown"] for result in fold_results), default=0.0),
        "regime_breakdown": _aggregate_regime_breakdowns(fold_results),
        "contender_metadata": _aggregate_contender_metadata(fold_results),
    }


def _aggregate_contender_metadata(fold_results: list[dict[str, Any]]) -> dict[str, Any]:
    for result in fold_results:
        metadata = result.get("contender_metadata", {})
        if metadata:
            return metadata
    return {}


def compare_fold_results(baseline: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    trade_ratio = challenger["trades"] / baseline["trades"] if baseline["trades"] > 0 else (1.0 if challenger["trades"] == 0 else float("inf"))
    drawdown_ratio = challenger["max_drawdown"] / baseline["max_drawdown"] if baseline["max_drawdown"] > 0 else (0.0 if challenger["max_drawdown"] == 0 else float("inf"))
    return {
        "fold_index": baseline["fold_index"],
        "baseline": baseline["name"],
        "challenger": challenger["name"],
        "roi_delta": challenger["roi"] - baseline["roi"],
        "win_rate_delta": (challenger["win_rate"] - baseline["win_rate"]) * 100.0,
        "brier_delta": challenger["avg_brier"] - baseline["avg_brier"],
        "pnl_delta": challenger["pnl"] - baseline["pnl"],
        "trade_ratio": trade_ratio,
        "drawdown_ratio": drawdown_ratio,
        "baseline_trades": baseline["trades"],
        "challenger_trades": challenger["trades"],
    }


def _summarize_regime_breakdown(
    signal_rows: list[dict[str, Any]],
    trade_log: list[dict[str, Any]],
    bet_size: float,
) -> dict[str, dict[str, Any]]:
    regimes = {
        str(row.get("regime") or "UNKNOWN") for row in signal_rows
    } | {
        str(trade.get("regime") or "UNKNOWN") for trade in trade_log
    }
    breakdown: dict[str, dict[str, Any]] = {}

    for regime in regimes:
        regime_signals = [row for row in signal_rows if str(row.get("regime") or "UNKNOWN") == regime]
        regime_trades = [trade for trade in trade_log if str(trade.get("regime") or "UNKNOWN") == regime]
        called = sum(1 for row in regime_signals if row["called"])
        correct_calls = sum(1 for row in regime_signals if row["correct_call"])
        wins = sum(1 for trade in regime_trades if trade["correct"])
        pnl = sum(float(trade["pnl"]) for trade in regime_trades)
        wagered = len(regime_trades) * bet_size

        breakdown[regime] = {
            "regime": regime,
            "eval_markets": len(regime_signals),
            "signal_calls": called,
            "correct_calls": correct_calls,
            "directional_accuracy": correct_calls / called if called else 0.0,
            "avg_brier": (
                sum(float(row["brier"]) for row in regime_signals) / len(regime_signals)
                if regime_signals else 0.0
            ),
            "avg_vs_market": (
                sum(float(row["brier"]) - float(row["market_brier"]) for row in regime_signals) / len(regime_signals)
                if regime_signals else 0.0
            ),
            "signal_trade_rate": (
                sum(1 for row in regime_signals if row["should_trade"]) / len(regime_signals)
                if regime_signals else 0.0
            ),
            "trades": len(regime_trades),
            "wins": wins,
            "losses": len(regime_trades) - wins,
            "win_rate": wins / len(regime_trades) if regime_trades else 0.0,
            "pnl": pnl,
            "wagered": wagered,
            "roi": pnl / wagered * 100.0 if wagered else 0.0,
        }
    return breakdown


def _aggregate_regime_breakdowns(fold_results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fold in fold_results:
        for regime, stats in fold.get("regime_breakdown", {}).items():
            grouped.setdefault(regime, []).append(stats)

    aggregate: dict[str, dict[str, Any]] = {}
    for regime, rows in grouped.items():
        eval_markets = sum(int(row["eval_markets"]) for row in rows)
        signal_calls = sum(int(row["signal_calls"]) for row in rows)
        correct_calls = sum(int(row["correct_calls"]) for row in rows)
        trades = sum(int(row["trades"]) for row in rows)
        wins = sum(int(row["wins"]) for row in rows)
        wagered = sum(float(row["wagered"]) for row in rows)
        pnl = sum(float(row["pnl"]) for row in rows)
        aggregate[regime] = {
            "regime": regime,
            "eval_markets": eval_markets,
            "signal_calls": signal_calls,
            "correct_calls": correct_calls,
            "directional_accuracy": correct_calls / signal_calls if signal_calls else 0.0,
            "avg_brier": _weighted_mean(rows, "avg_brier", "eval_markets"),
            "avg_vs_market": _weighted_mean(rows, "avg_vs_market", "eval_markets"),
            "signal_trade_rate": _weighted_mean(rows, "signal_trade_rate", "eval_markets"),
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": wins / trades if trades else 0.0,
            "pnl": pnl,
            "wagered": wagered,
            "roi": pnl / wagered * 100.0 if wagered else 0.0,
        }
    return aggregate


def compare_regime_breakdowns(
    baseline_breakdown: dict[str, dict[str, Any]],
    challenger_breakdown: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    rows = []
    for regime in sorted(set(baseline_breakdown) | set(challenger_breakdown)):
        base = baseline_breakdown.get(regime, {"trades": 0, "roi": 0.0, "pnl": 0.0, "win_rate": 0.0, "eval_markets": 0})
        chal = challenger_breakdown.get(regime, {"trades": 0, "roi": 0.0, "pnl": 0.0, "win_rate": 0.0, "eval_markets": 0})
        rows.append(
            {
                "regime": regime,
                "baseline_trades": int(base.get("trades", 0)),
                "challenger_trades": int(chal.get("trades", 0)),
                "baseline_roi": float(base.get("roi", 0.0)),
                "challenger_roi": float(chal.get("roi", 0.0)),
                "baseline_pnl": float(base.get("pnl", 0.0)),
                "challenger_pnl": float(chal.get("pnl", 0.0)),
                "baseline_wr": float(base.get("win_rate", 0.0)),
                "challenger_wr": float(chal.get("win_rate", 0.0)),
                "eval_markets": max(int(base.get("eval_markets", 0)), int(chal.get("eval_markets", 0))),
                "roi_delta": float(chal.get("roi", 0.0)) - float(base.get("roi", 0.0)),
                "pnl_delta": float(chal.get("pnl", 0.0)) - float(base.get("pnl", 0.0)),
                "win_rate_delta": (float(chal.get("win_rate", 0.0)) - float(base.get("win_rate", 0.0))) * 100.0,
            }
        )

    helpful = [
        row for row in rows
        if row["challenger_trades"] > 0 and row["roi_delta"] > 0 and row["pnl_delta"] >= 0
    ]
    harmful = [
        row for row in rows
        if row["challenger_trades"] > 0 and row["roi_delta"] < 0 and row["pnl_delta"] <= 0
    ]
    helpful.sort(key=lambda row: (row["roi_delta"], row["pnl_delta"]), reverse=True)
    harmful.sort(key=lambda row: (row["roi_delta"], row["pnl_delta"]))

    takeaways = []
    if helpful:
        top = helpful[0]
        takeaways.append(
            f"Best challenger regime: {top['regime']} ({top['roi_delta']:+.2f}pp ROI, {top['pnl_delta']:+.2f} P&L vs baseline)"
        )
    if harmful:
        worst = harmful[0]
        takeaways.append(
            f"Worst challenger regime: {worst['regime']} ({worst['roi_delta']:+.2f}pp ROI, {worst['pnl_delta']:+.2f} P&L vs baseline)"
        )

    return {
        "rows": rows,
        "helpful": helpful[:5],
        "harmful": harmful[:5],
        "takeaways": takeaways,
    }


def apply_promotion_gate(
    baseline_summary: dict[str, Any],
    challenger_summary: dict[str, Any],
    fold_comparisons: list[dict[str, Any]],
    gate: PromotionGate,
) -> dict[str, Any]:
    required_fold_passes = math.ceil(len(fold_comparisons) * gate.min_fold_pass_rate)
    passing_folds = 0
    fold_checks = []

    for comparison in fold_comparisons:
        drawdown_ok = comparison["drawdown_ratio"] <= (1.0 + gate.max_drawdown_worsening)
        fold_pass = (
            comparison["roi_delta"] >= 0.0
            and comparison["trade_ratio"] >= gate.min_trade_ratio
            and drawdown_ok
        )
        passing_folds += 1 if fold_pass else 0
        fold_checks.append({
            **comparison,
            "drawdown_ok": drawdown_ok,
            "pass": fold_pass,
        })

    trade_ratio = challenger_summary["trades"] / baseline_summary["trades"] if baseline_summary["trades"] > 0 else 1.0
    drawdown_ratio = (
        challenger_summary["max_drawdown"] / baseline_summary["max_drawdown"]
        if baseline_summary["max_drawdown"] > 0 else (0.0 if challenger_summary["max_drawdown"] == 0 else float("inf"))
    )

    reasons = []
    if challenger_summary["roi"] - baseline_summary["roi"] < gate.min_total_roi_delta:
        reasons.append(
            f"aggregate ROI delta {challenger_summary['roi'] - baseline_summary['roi']:+.2f}pp < required {gate.min_total_roi_delta:.2f}pp"
        )
    if (challenger_summary["win_rate"] - baseline_summary["win_rate"]) * 100.0 < gate.min_total_win_rate_delta:
        reasons.append(
            f"aggregate win-rate delta {(challenger_summary['win_rate'] - baseline_summary['win_rate']) * 100.0:+.2f}pp < required {gate.min_total_win_rate_delta:.2f}pp"
        )
    if trade_ratio < gate.min_trade_ratio:
        reasons.append(f"trade ratio {trade_ratio:.2f} < required {gate.min_trade_ratio:.2f}")
    if drawdown_ratio > (1.0 + gate.max_drawdown_worsening):
        reasons.append(
            f"drawdown ratio {drawdown_ratio:.2f} > allowed {1.0 + gate.max_drawdown_worsening:.2f}"
        )
    if passing_folds < required_fold_passes:
        reasons.append(f"passing folds {passing_folds}/{len(fold_comparisons)} < required {required_fold_passes}")

    passed = not reasons
    return {
        "passed": passed,
        "required_fold_passes": required_fold_passes,
        "passing_folds": passing_folds,
        "trade_ratio": trade_ratio,
        "drawdown_ratio": drawdown_ratio,
        "aggregate_roi_delta": challenger_summary["roi"] - baseline_summary["roi"],
        "aggregate_win_rate_delta": (challenger_summary["win_rate"] - baseline_summary["win_rate"]) * 100.0,
        "fold_checks": fold_checks,
        "reasons": reasons,
    }


def print_head_to_head_report(results: dict[str, Any]) -> None:
    baseline = results["baseline"]
    challenger = results["challenger"]
    gate = results["gate"]

    print(f"\n{'=' * 84}")
    print("  HEAD-TO-HEAD SAMPLE-OUT REPORT")
    print(f"{'=' * 84}")
    print(f"  {'Metric':<24} {baseline['name']:>24} {challenger['name']:>24}")
    print(f"  {'-' * 24} {'-' * 24} {'-' * 24}")

    rows = [
        ("Eval markets", baseline["eval_markets"], challenger["eval_markets"], "d"),
        ("Trades", baseline["trades"], challenger["trades"], "d"),
        ("Directional acc", baseline["directional_accuracy"] * 100, challenger["directional_accuracy"] * 100, ".1f%"),
        ("Avg brier", baseline["avg_brier"], challenger["avg_brier"], ".4f"),
        ("ROI", baseline["roi"], challenger["roi"], ".2f%"),
        ("Win rate", baseline["win_rate"] * 100, challenger["win_rate"] * 100, ".1f%"),
        ("P&L", baseline["pnl"], challenger["pnl"], "$.2f"),
        ("Max drawdown", baseline["max_drawdown"], challenger["max_drawdown"], "$.2f"),
    ]

    for label, left, right, fmt in rows:
        print(f"  {label:<24} {_fmt(left, fmt):>24} {_fmt(right, fmt):>24}")

    print(f"\n  Fold checks:")
    for fold in gate["fold_checks"]:
        status = "PASS" if fold["pass"] else "FAIL"
        print(
            f"    Fold {fold['fold_index']}: {status} | ROI {fold['roi_delta']:+.2f}pp | "
            f"WR {fold['win_rate_delta']:+.2f}pp | trade ratio {fold['trade_ratio']:.2f}"
        )

    print(f"\n  Promotion gate: {'PASS' if gate['passed'] else 'FAIL'}")
    if gate["reasons"]:
        for reason in gate["reasons"]:
            print(f"    - {reason}")

    challenger_meta = challenger.get("contender_metadata", {})
    if challenger_meta:
        print("\n  Challenger metadata:")
        for key, value in challenger_meta.items():
            print(f"    - {key}: {value}")


def deterministic_slippage(seed: int, fold_index: int, market_index: int) -> float:
    rng = random.Random(f"{seed}:{fold_index}:{market_index}")
    return rng.uniform(0.01, 0.03)


def _weighted_mean(rows: list[dict[str, Any]], value_key: str, weight_key: str) -> float:
    total_weight = sum(row[weight_key] for row in rows)
    if total_weight <= 0:
        return 0.0
    return sum(row[value_key] * row[weight_key] for row in rows) / total_weight


def _max_drawdown_amount(pnls: list[float]) -> float:
    running = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        running += pnl
        peak = max(peak, running)
        max_drawdown = max(max_drawdown, peak - running)
    return max_drawdown


def _deterministic_decision(prob_up: float, should_trade: bool, reason: str) -> ResearchDecision:
    confidence = "medium" if should_trade else "low"
    conviction = 3 if should_trade else 0
    return ResearchDecision(
        prob_up=float(prob_up),
        should_trade=bool(should_trade),
        reason=reason,
        conviction_score=conviction,
        confidence=confidence,
    )


def _skip_decision(reason: str) -> dict[str, Any]:
    return {
        "estimate": 0.5,
        "should_trade": False,
        "direction": None,
        "confidence": "low",
        "conviction_score": 0,
        "reason": reason,
    }


def _fmt(value: float | int, fmt: str) -> str:
    if fmt == "d":
        return f"{int(value)}"
    if fmt.endswith("%"):
        precision = fmt[:-1]
        return f"{value:{precision}}%"
    if fmt.startswith("$"):
        precision = fmt[1:]
        return f"${value:{precision}}"
    return f"{value:{fmt}}"


def _build_llm_user_prompt(context: ResearchContext) -> str:
    try:
        btc_summary_str = format_for_prompt(context.btc_summary)
    except Exception:
        btc_summary_str = _fallback_prompt_block(context)

    return f"""
### Research Mode
This is a backtest evaluation. You are a challenger to the current production baseline.
Only output valid JSON. Do not add markdown.

### Current Market State
{btc_summary_str}

### Market Environment
- Regime: {context.production_regime['label']}
- Polymarket Price (Yes): {context.market['implied_price_yes']:.2%}
- Research objective: maximize out-of-sample edge after fees, not narrative quality

### Task
Estimate the probability BTC closes UP for this market.
Set confidence from 0 to 5.
Only use confidence >= 3 when you would actually trade.
"""


def _fallback_prompt_block(context: ResearchContext) -> str:
    last = context.formatted_candles[-1]
    return (
        "## BTC Summary\n"
        f"- Last candle close: {last['close']}\n"
        f"- Last candle direction: {last['direction']}\n"
        f"- Regime: {context.production_regime['label']}\n"
        f"- Consecutive streak: {context.features.get('consecutive_streak', 0)}\n"
        f"- Compression: {context.features.get('compression', 0)}\n"
        f"- Volume ratio: {context.features.get('volume_ratio', 1.0):.2f}\n"
    )


def _bounded_probability(value: Any) -> float:
    try:
        return min(max(float(value), 0.0), 1.0)
    except (TypeError, ValueError):
        return 0.5


def _safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _normalize_probability(value: Any) -> float:
    raw = _extract_number(value)
    if raw is None:
        return 0.5
    if 1.0 < raw <= 100.0:
        raw /= 100.0
    return min(max(raw, 0.0), 1.0)


def _normalize_confidence(value: Any) -> int:
    raw = _extract_number(value)
    if raw is None:
        return 0
    if 0.0 <= raw <= 1.0:
        raw *= 5.0
    elif 5.0 < raw <= 100.0:
        raw /= 20.0
    return max(0, min(5, int(round(raw))))


def _extract_number(value: Any) -> float | None:
    if isinstance(value, str):
        cleaned = value.strip().replace("%", "")
        try:
            return float(cleaned)
        except ValueError:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
