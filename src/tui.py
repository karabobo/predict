"""
tui.py - lightweight terminal monitor for prediction and paper trading state.

It intentionally avoids frontend dependencies. Use --once for scripts/tests, or
let it refresh in-place for an operator console.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from fetch_markets import DB_PATH


@dataclass(frozen=True)
class MonitorRow:
    market_id: str
    question: str
    end_date: str
    price_yes: float | None
    price_no: float | None
    model_version: str | None
    estimate: float | None
    confidence: str | None
    conviction_score: int | None
    should_trade: bool | None
    paper_status: str | None
    paper_direction: str | None
    paper_edge: float | None
    entry_offset_seconds: int | None
    best_bid: float | None
    best_ask: float | None
    spread: float | None
    fill_source: str | None
    settlement_source: str | None
    won: bool | None
    pnl_usd: float | None


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def fetch_monitor_rows(db: sqlite3.Connection, *, limit: int = 12) -> list[MonitorRow]:
    has_paper = _table_exists(db, "paper_orders")
    paper_join = (
        """
        LEFT JOIN (
            SELECT po.*
            FROM paper_orders po
            JOIN (
                SELECT market_id, MAX(id) AS max_id
                FROM paper_orders
                GROUP BY market_id
            ) latest_po ON latest_po.max_id = po.id
        ) po ON po.market_id = m.id
        """
        if has_paper
        else ""
    )
    paper_fields = (
        """
        po.status AS paper_status,
        po.direction AS paper_direction,
        po.expected_edge AS paper_edge,
        po.entry_offset_seconds,
        po.best_bid,
        po.best_ask,
        po.spread,
        po.fill_source,
        po.settlement_source,
        po.won,
        po.pnl_usd
        """
        if has_paper
        else """
        NULL AS paper_status,
        NULL AS paper_direction,
        NULL AS paper_edge,
        NULL AS entry_offset_seconds,
        NULL AS best_bid,
        NULL AS best_ask,
        NULL AS spread,
        NULL AS fill_source,
        NULL AS settlement_source,
        NULL AS won,
        NULL AS pnl_usd
        """
    )
    cursor = db.execute(
        f"""
        SELECT
            m.id AS market_id,
            m.question,
            m.end_date,
            m.price_yes,
            m.price_no,
            p.model_version,
            p.estimate,
            p.confidence,
            p.conviction_score,
            p.should_trade,
            {paper_fields}
        FROM markets m
        LEFT JOIN (
            SELECT p1.*
            FROM predictions p1
            JOIN (
                SELECT market_id, MAX(id) AS max_id
                FROM predictions
                GROUP BY market_id
            ) latest_p ON latest_p.max_id = p1.id
        ) p ON p.market_id = m.id
        {paper_join}
        WHERE COALESCE(m.resolved, 0) = 0
        ORDER BY m.end_date ASC
        LIMIT ?
        """,
        (limit,),
    )
    rows = []
    for raw in cursor.fetchall():
        rows.append(
            MonitorRow(
                market_id=str(raw[0]),
                question=str(raw[1] or ""),
                end_date=str(raw[2] or ""),
                price_yes=float(raw[3]) if raw[3] is not None else None,
                price_no=float(raw[4]) if raw[4] is not None else None,
                model_version=str(raw[5]) if raw[5] is not None else None,
                estimate=float(raw[6]) if raw[6] is not None else None,
                confidence=str(raw[7]) if raw[7] is not None else None,
                conviction_score=int(raw[8]) if raw[8] is not None else None,
                should_trade=bool(raw[9]) if raw[9] is not None else None,
                paper_status=str(raw[10]) if raw[10] is not None else None,
                paper_direction=str(raw[11]) if raw[11] is not None else None,
                paper_edge=float(raw[12]) if raw[12] is not None else None,
                entry_offset_seconds=int(raw[13]) if raw[13] is not None else None,
                best_bid=float(raw[14]) if raw[14] is not None else None,
                best_ask=float(raw[15]) if raw[15] is not None else None,
                spread=float(raw[16]) if raw[16] is not None else None,
                fill_source=str(raw[17]) if raw[17] is not None else None,
                settlement_source=str(raw[18]) if raw[18] is not None else None,
                won=bool(raw[19]) if raw[19] is not None else None,
                pnl_usd=float(raw[20]) if raw[20] is not None else None,
            )
        )
    return rows


def render_monitor(rows: Iterable[MonitorRow], *, now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    lines = [
        f"Polymarket Monitor | {current.isoformat(timespec='seconds')}",
        "",
        "Market     End UTC   YES   NO    Model                      Est   Cvx Trade Paper                                           Book",
        "---------  --------  ----  ----  -------------------------  ----  --- ----- ------------------------------------------------ ----------------",
    ]
    count = 0
    for row in rows:
        count += 1
        end_label = _end_label(row.end_date)
        model = _clip(row.model_version or "-", 25)
        estimate = "-" if row.estimate is None else f"{row.estimate:.2f}"
        cvx = "-" if row.conviction_score is None else str(row.conviction_score)
        trade = "-" if row.should_trade is None else ("yes" if row.should_trade else "no")
        paper = _paper_label(row)
        book = _book_label(row)
        lines.append(
            f"{_clip(row.market_id, 9):9}  {end_label:8}  "
            f"{_fmt_price(row.price_yes):>4}  {_fmt_price(row.price_no):>4}  "
            f"{model:25}  {estimate:>4}  {cvx:>3} {trade:>5} {paper:48} {book}"
        )
    if count == 0:
        lines.append("No active markets.")
    return "\n".join(lines) + "\n"


def _end_label(value: str) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).strftime("%H:%M:%S")
    except ValueError:
        return _clip(value, 8)


def _fmt_price(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def _paper_label(row: MonitorRow) -> str:
    if not row.paper_status:
        return "-"
    direction = row.paper_direction or "-"
    edge = "" if row.paper_edge is None else f" edge={row.paper_edge:+.3f}"
    offset = "" if row.entry_offset_seconds is None else f" @{row.entry_offset_seconds}s"
    settled = ""
    if row.won is not None:
        result = "W" if row.won else "L"
        pnl = "" if row.pnl_usd is None else f" pnl={row.pnl_usd:+.2f}"
        source = "" if not row.settlement_source else f" {row.settlement_source}"
        settled = f" {result}{pnl}{source}"
    return _clip(f"{row.paper_status} {direction}{offset}{edge}{settled}", 48)


def _book_label(row: MonitorRow) -> str:
    if row.best_bid is None and row.best_ask is None:
        return "-"
    source = "" if not row.fill_source else f" {row.fill_source}"
    spread = "" if row.spread is None else f" spr={row.spread:.3f}"
    return _clip(f"b={_fmt_price(row.best_bid)} a={_fmt_price(row.best_ask)}{spread}{source}", 32)


def _clip(value: str, width: int) -> str:
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "."


def main() -> None:
    parser = argparse.ArgumentParser(description="Terminal monitor for predictions and paper orders.")
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--refresh", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    while True:
        db = sqlite3.connect(args.db)
        try:
            rendered = render_monitor(fetch_monitor_rows(db, limit=args.limit))
        finally:
            db.close()
        if args.once:
            print(rendered, end="")
            return
        print("\033[2J\033[H", end="")
        print(rendered, end="")
        time.sleep(max(args.refresh, 0.5))


if __name__ == "__main__":
    main()
