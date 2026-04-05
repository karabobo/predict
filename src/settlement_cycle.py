"""
settlement_cycle.py — High-frequency settlement parsing cycle.

This loop is intentionally lightweight: it only checks ended markets for
official or provisional outcomes and stores price snapshots for research.
"""

from fetch_markets import DB_PATH, init_db
from settlement import run_settlement_phase


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    try:
        run_settlement_phase(db)
    finally:
        try:
            db.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
