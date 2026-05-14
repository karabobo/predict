"""
fetch_markets.py — Pull "Bitcoin Up or Down" 5-minute markets from Polymarket.

Searches the Gamma API for upcoming, unresolved Bitcoin 5-minute interval
markets and stores them in the local SQLite database.
"""

import re
import requests
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

GAMMA_API = "https://gamma-api.polymarket.com"
DB_PATH = Path(__file__).parent.parent / "data" / "predictions.db"
HTTP_TIMEOUT = 15
DB_BUSY_TIMEOUT_MS = 10000

# Regex to capture a hyphenated time range like "11:55AM-12:00PM"
TIME_RANGE_RE = re.compile(r"(\d{1,2}:\d{2}[AP]M)\s*-\s*(\d{1,2}:\d{2}[AP]M)")


def _is_5min_window(title):
    """Check if the title contains a 5-minute time window."""
    match = TIME_RANGE_RE.search(title)
    if not match:
        return False
    try:
        t1 = datetime.strptime(match.group(1), "%I:%M%p")
        t2 = datetime.strptime(match.group(2), "%I:%M%p")
        diff = (t2 - t1).total_seconds()
        if diff < 0:
            diff += 12 * 3600  # handle AM/PM wrap
        return diff == 300  # exactly 5 minutes
    except ValueError:
        return False


def _parse_clob_token_ids(raw_token_ids):
    """Return (yes_token_id, no_token_id) from Gamma's clobTokenIds field."""
    if not raw_token_ids:
        return None, None

    token_ids = raw_token_ids
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids)

    if not isinstance(token_ids, list):
        return None, None

    token_yes = str(token_ids[0]) if len(token_ids) >= 1 and token_ids[0] else None
    token_no = str(token_ids[1]) if len(token_ids) >= 2 and token_ids[1] else None
    return token_yes, token_no


def _ensure_column(db, table, column_definition):
    """Lightweight migration helper for SQLite tables."""
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column_definition}")
        db.commit()
    except sqlite3.OperationalError:
        pass


def ensure_market_schema(db):
    """Ensure shared market/settlement schema exists on any SQLite connection."""
    _ensure_column(db, "markets", "condition_id TEXT")
    _ensure_column(db, "markets", "token_yes TEXT")
    _ensure_column(db, "markets", "token_no TEXT")
    _ensure_column(db, "markets", "provisional_outcome INTEGER DEFAULT NULL")
    _ensure_column(db, "markets", "provisional_resolved_at TEXT")
    _ensure_column(db, "markets", "provisional_source TEXT")
    _ensure_column(db, "markets", "official_resolved_at TEXT")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS market_price_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price_yes REAL,
            price_no REAL,
            closed INTEGER DEFAULT 0,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
        """
    )
    db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_market_price_snapshots_market_time
        ON market_price_snapshots (market_id, observed_at DESC)
        """
    )
    db.commit()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(f"PRAGMA busy_timeout={DB_BUSY_TIMEOUT_MS}")
    db.execute("""
        CREATE TABLE IF NOT EXISTS markets (
            id TEXT PRIMARY KEY,
            question TEXT,
            category TEXT,
            end_date TEXT,
            volume REAL,
            price_yes REAL,
            price_no REAL,
            fetched_at TEXT,
            resolved INTEGER DEFAULT 0,
            outcome INTEGER DEFAULT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            agent TEXT,
            estimate REAL,
            edge REAL,
            confidence TEXT,
            reasoning TEXT,
            predicted_at TEXT,
            cycle INTEGER,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS evolution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle INTEGER,
            agent TEXT,
            change_description TEXT,
            brier_before REAL,
            brier_after REAL,
            kept INTEGER,
            timestamp TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS live_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER,
            market_id TEXT NOT NULL,
            token_id TEXT,
            direction TEXT,
            side TEXT,
            confidence TEXT,
            conviction_score INTEGER,
            order_type TEXT,
            bet_amount_usd REAL,
            predicted_prob REAL,
            market_price REAL,
            expected_edge REAL,
            status TEXT,
            order_id TEXT,
            success INTEGER DEFAULT 0,
            dry_run INTEGER DEFAULT 0,
            response_json TEXT,
            error_text TEXT,
            created_at TEXT,
            FOREIGN KEY (prediction_id) REFERENCES predictions(id),
            FOREIGN KEY (market_id) REFERENCES markets(id)
        )
    """)
    ensure_market_schema(db)
    db.commit()
    return db


def fetch_active_markets():
    """Fetch upcoming, unresolved 'Bitcoin Up or Down' 5-minute markets."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=24)

    params = {
        "limit": 200,
        "order": "endDate",
        "ascending": "true",
        "end_date_min": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    resp = requests.get(f"{GAMMA_API}/events", params=params, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    events = resp.json()

    markets = []
    for event in events:
        title = event.get("title", "")

        # Must contain "Bitcoin Up or Down" and a hyphenated time range
        if "Bitcoin Up or Down" not in title:
            continue
        if not _is_5min_window(title):
            continue

        for market in event.get("markets", []):
            try:
                end_date = market.get("endDate") or market.get("end_date_iso")
                if not end_date:
                    continue

                end_dt = datetime.fromisoformat(
                    end_date.replace("Z", "+00:00")
                )

                # Skip already-resolved markets
                if market.get("resolved", False):
                    continue

                # Only keep markets ending within the next few hours
                if end_dt <= now or end_dt > cutoff:
                    continue

                # Verify outcomes are ["Up", "Down"]
                outcomes = market.get("outcomes", "[]")
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                if outcomes != ["Up", "Down"]:
                    continue

                # Parse outcome prices — index 0 is "Up"
                raw_prices = market.get("outcomePrices", '["0","0"]')
                if isinstance(raw_prices, str):
                    prices = json.loads(raw_prices)
                else:
                    prices = raw_prices
                price_up = float(prices[0])
                price_down = float(prices[1]) if len(prices) > 1 else round(1 - price_up, 4)
                token_yes, token_no = _parse_clob_token_ids(market.get("clobTokenIds"))

                volume = float(market.get("volume", 0) or 0)

                markets.append({
                    "id": market["id"],
                    "question": market.get("question", title),
                    "category": event.get("category", "crypto"),
                    "end_date": end_date,
                    "volume": volume,
                    "price_yes": price_up,       # "Up" price
                    "price_no": price_down,      # "Down" price
                    "condition_id": market.get("conditionId"),
                    "token_yes": token_yes,
                    "token_no": token_no,
                })
            except (ValueError, KeyError, IndexError, json.JSONDecodeError):
                continue

    # Sort by soonest end_date first
    markets.sort(key=lambda m: m["end_date"])
    return markets


def store_markets(db, markets):
    """Upsert markets into the database."""
    for m in markets:
        db.execute("""
            INSERT INTO markets (
                id, question, category, end_date, volume, price_yes, price_no,
                fetched_at, condition_id, token_yes, token_no
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                volume = excluded.volume,
                price_yes = excluded.price_yes,
                price_no = excluded.price_no,
                fetched_at = excluded.fetched_at,
                condition_id = COALESCE(excluded.condition_id, condition_id),
                token_yes = COALESCE(excluded.token_yes, token_yes),
                token_no = COALESCE(excluded.token_no, token_no)
        """, (
            m["id"], m["question"], m["category"], m["end_date"],
            m["volume"], m["price_yes"], m["price_no"],
            datetime.now(timezone.utc).isoformat(),
            m.get("condition_id"), m.get("token_yes"), m.get("token_no"),
        ))
    db.commit()


def get_unresolved_markets(db, limit=5):
    """Get markets that haven't resolved yet, ordered by soonest resolution."""
    cursor = db.execute("""
        SELECT id, question, category, end_date, volume, price_yes
        FROM markets
        WHERE resolved = 0
        ORDER BY end_date ASC
        LIMIT ?
    """, (limit,))
    return [dict(zip(["id", "question", "category", "end_date", "volume", "price_yes"], row))
            for row in cursor.fetchall()]


if __name__ == "__main__":
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = init_db()
    markets = fetch_active_markets()
    store_markets(db, markets)
    print(f"Fetched and stored {len(markets)} Bitcoin 5-min markets")
    for m in markets:
        print(f"  Up {m['price_yes']:.1%} / Down {m['price_no']:.1%} | {m['question'][:80]}")
    db.close()
