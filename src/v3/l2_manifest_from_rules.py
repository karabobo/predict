from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC_ROOT = ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from src.strategies.regime import compute_regime_from_candles
from src.v3.backtest_rules import build_contexts, load_candles
from src.v3.foundation_shadow_rolling import DEFAULT_CANDLES, DEFAULT_DB
from src.v3.probability_baseline import BaselineProbabilityEnsemble
from src.v3.rule_variants import available_rules


DEFAULT_L2_BASE = "https://s.wangshuox.com/poly_l2"
DEFAULT_SNAPSHOT_BASE = "https://s.wangshuox.com/poly_snapshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build an L2 replay manifest from historical rule trigger windows.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--rules", required=True, help="Comma-separated rule names")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--limit-per-rule", type=int, default=50)
    parser.add_argument("--min-prior-edge", type=float, default=None, help="If set, require ensemble prior to agree with each rule.")
    parser.add_argument("--prior-warm-up", type=int, default=5000)
    parser.add_argument("--slug-ts", choices=("start", "end"), default="start", help="Poly replay slugs normally use market start; use end only for legacy manifests.")
    parser.add_argument("--market-minutes", type=int, default=5)
    parser.add_argument("--l2-base", default=DEFAULT_L2_BASE)
    parser.add_argument("--snapshot-base", default=DEFAULT_SNAPSHOT_BASE)
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    return parser.parse_args()


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def load_markets(db_path: Path, from_date: str | None, to_date: str | None) -> list[sqlite3.Row]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    clauses = ["outcome IS NOT NULL", "window_start_ts IS NOT NULL"]
    params: list[Any] = []
    if from_date:
        clauses.append("end_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("end_date <= ?")
        params.append(to_date)
    try:
        return conn.execute(
            f"""
            SELECT market_id, slug, question, end_date, outcome, window_start_ts
            FROM historical_markets
            WHERE {' AND '.join(clauses)}
            ORDER BY end_date ASC, market_id ASC
            """,
            params,
        ).fetchall()
    finally:
        conn.close()


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    rules = available_rules()
    rule_names = parse_csv(args.rules)
    for rule_name in rule_names:
        if rule_name not in rules:
            raise ValueError(f"Unknown rule: {rule_name}")

    candles = load_candles(args.btc_candles)
    contexts_by_start, _ = build_contexts(candles, args.lookback)
    markets = load_markets(args.db, args.from_date, args.to_date)
    prior = None
    if args.min_prior_edge is not None:
        train_contexts = []
        # Train once on the earliest available history before the selected replay set.
        from src.v3.foundation_shadow_rolling import build_rolling_contexts

        all_contexts, _ = build_rolling_contexts(
            db_path=args.db,
            btc_candles_path=args.btc_candles,
            from_date=None,
            to_date=None,
            lookback=args.lookback,
        )
        train_contexts = all_contexts[: max(args.prior_warm_up, 1)]
        prior = BaselineProbabilityEnsemble()
        prior.fit(train_contexts)

    selected: list[dict[str, Any]] = []
    counts = {rule_name: 0 for rule_name in rule_names}
    seen: set[tuple[str, str]] = set()
    for row in markets:
        start_ts = int(row["window_start_ts"])
        context_pack = contexts_by_start.get(start_ts)
        if context_pack is None:
            continue
        context = context_pack["context_candles"]
        regime = compute_regime_from_candles(context)
        for rule_name in rule_names:
            if counts[rule_name] >= args.limit_per_rule:
                continue
            signal = rules[rule_name](context, regime)
            if not signal.get("should_trade"):
                continue
            if prior is not None and not _prior_agrees(prior, context, regime, signal, args.min_prior_edge):
                continue
            slug = _l2_slug(row["slug"], start_ts, args.market_minutes, args.slug_ts)
            key = (rule_name, slug)
            if key in seen:
                continue
            seen.add(key)
            selected.append(
                {
                    "market_slug": slug,
                    "l2": f"{args.l2_base.rstrip('/')}/{slug}.parquet",
                    "snapshot": f"{args.snapshot_base.rstrip('/')}/{slug}.parquet",
                    "rule": rule_name,
                    "market_id": str(row["market_id"]),
                    "end_date": row["end_date"],
                    "source_slug": row["slug"],
                    "regime": regime["label"],
                    "reason": str(signal.get("reason", "")),
                }
            )
            counts[rule_name] += 1
        if all(count >= args.limit_per_rule for count in counts.values()):
            break
    return {
        "metadata": {
            "rules": rule_names,
            "counts": counts,
            "market_minutes": args.market_minutes,
            "slug_ts": args.slug_ts,
            "min_prior_edge": args.min_prior_edge,
        },
        "markets": selected,
    }


def _prior_agrees(
    prior: BaselineProbabilityEnsemble,
    context: list[dict[str, Any]],
    regime: dict[str, Any],
    signal: dict[str, Any],
    min_prior_edge: float,
) -> bool:
    from types import SimpleNamespace

    prediction = prior.predict(SimpleNamespace(formatted_candles=context, production_regime=regime, market={}))
    prior_direction = "UP" if prediction.prob_up > 0.5 else "DOWN" if prediction.prob_up < 0.5 else "SKIP"
    signal_direction = str(signal.get("direction") or "")
    return prior_direction == signal_direction and abs(float(prediction.prob_up) - 0.5) >= min_prior_edge


def _l2_slug(source_slug: str, start_ts: int, minutes: int, slug_ts: str) -> str:
    if slug_ts == "start":
        return source_slug
    end_ts = start_ts + minutes * 60
    return f"btc-updown-{minutes}m-{end_ts}"


def main() -> None:
    args = parse_args()
    manifest = build_manifest(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    counts = manifest["metadata"]["counts"]
    print(f"wrote={args.output} markets={len(manifest['markets'])} counts={counts}")


if __name__ == "__main__":
    main()
