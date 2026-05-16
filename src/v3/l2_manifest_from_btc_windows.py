from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.strategies.regime import compute_regime_from_candles
from src.v3.backtest_rules import Candle, load_candles
from src.v3.l2_candidate_replay import _completed_context
from src.v3.rule_variants import available_rules


DEFAULT_CANDLES = Path("/home/ubuntu/migration/Data/btc_5m.parquet")
DEFAULT_L2_BASE = "https://s.wangshuox.com/poly_l2"
DEFAULT_SNAPSHOT_BASE = "https://s.wangshuox.com/poly_snapshot"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build L2 manifest by scanning BTC windows for rule triggers.")
    parser.add_argument("--btc-candles", type=Path, default=DEFAULT_CANDLES)
    parser.add_argument("--rules", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minutes", type=int, default=15)
    parser.add_argument("--decision-offset-seconds", type=int, default=30)
    parser.add_argument("--slug-ts", choices=("start", "end"), default="start")
    parser.add_argument("--lookback", type=int, default=20)
    parser.add_argument("--limit-per-rule", type=int, default=50)
    parser.add_argument("--from-ts", type=int)
    parser.add_argument("--to-ts", type=int)
    parser.add_argument("--l2-base", default=DEFAULT_L2_BASE)
    parser.add_argument("--snapshot-base", default=DEFAULT_SNAPSHOT_BASE)
    return parser.parse_args()


def parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def run(args: argparse.Namespace) -> dict[str, Any]:
    candles = load_candles(args.btc_candles)
    rules = available_rules()
    rule_names = parse_csv(args.rules)
    for rule_name in rule_names:
        if rule_name not in rules:
            raise ValueError(f"Unknown rule: {rule_name}")
    starts = _window_starts(candles, minutes=args.minutes, from_ts=args.from_ts, to_ts=args.to_ts)
    counts = {rule_name: 0 for rule_name in rule_names}
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for start_ts in starts:
        end_ts = start_ts + args.minutes * 60
        decision_ts = start_ts + args.decision_offset_seconds
        try:
            context = _completed_context(candles, decision_ts=decision_ts, lookback=args.lookback)
        except ValueError:
            continue
        regime = compute_regime_from_candles(context)
        for rule_name in rule_names:
            if counts[rule_name] >= args.limit_per_rule:
                continue
            signal = rules[rule_name](context, regime)
            if not signal.get("should_trade"):
                continue
            slug_timestamp = start_ts if args.slug_ts == "start" else end_ts
            slug = f"btc-updown-{args.minutes}m-{slug_timestamp}"
            key = (rule_name, slug)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "market_slug": slug,
                    "l2": f"{args.l2_base.rstrip('/')}/{slug}.parquet",
                    "snapshot": f"{args.snapshot_base.rstrip('/')}/{slug}.parquet",
                    "rule": rule_name,
                    "window_start_ts": start_ts,
                    "window_end_ts": end_ts,
                    "decision_ts": decision_ts,
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
            "minutes": args.minutes,
            "decision_offset_seconds": args.decision_offset_seconds,
            "slug_ts": args.slug_ts,
        },
        "markets": rows,
    }


def _window_starts(candles: list[Candle], *, minutes: int, from_ts: int | None, to_ts: int | None) -> list[int]:
    step = minutes * 60
    timestamps = {int(c.ts) for c in candles}
    starts = []
    for ts in sorted(timestamps):
        if ts % step != 0:
            continue
        if from_ts is not None and ts < from_ts:
            continue
        if to_ts is not None and ts > to_ts:
            continue
        if ts + step - 300 in timestamps:
            starts.append(ts)
    return starts


def main() -> None:
    args = parse_args()
    manifest = run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote={args.output} markets={len(manifest['markets'])} counts={manifest['metadata']['counts']}")


if __name__ == "__main__":
    main()
