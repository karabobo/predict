"""
coach_cycle.py — Parallel coach-audit cycle for ended markets.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from src.fetch_markets import DB_PATH as PREDICTIONS_DB_PATH, init_db as init_predictions_db
from src.v3.coaches import init_db as init_research_db
from src.v3.coaches import run_coach_audits


def main() -> None:
    PREDICTIONS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    predictions_db = init_predictions_db()
    research_db = init_research_db()
    try:
        counts = run_coach_audits(predictions_db, research_db)
    finally:
        _safe_close(predictions_db)
        _safe_close(research_db)

    print(
        "Coach audits: "
        f"checked={counts['checked']} audited={counts['audited']} "
        f"skip={counts['skip_audits']} toxicity={counts['toxicity_audits']} "
        f"official={counts['official']} provisional={counts['provisional']} "
        f"errors={counts['errors']}"
    )


def _safe_close(db: sqlite3.Connection | None) -> None:
    if db is None:
        return
    try:
        db.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
