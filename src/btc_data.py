"""
btc_data.py — Fetch recent BTC candlestick data for prediction agents.

Primary: Kraken (US-regulated, no auth, no geo-blocking)
Fallback: Coinbase (US-based, no auth, 5-min candles with volume)

Provides OHLCV candles + micro-TA signals so agents can read price action.
"""

import requests
import statistics
import time
from datetime import datetime, timezone

KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
COINBASE_CANDLES = "https://api.exchange.coinbase.com/products/BTC-USD/candles"


def _load_indicator_stack():
    """Import heavy indicator dependencies lazily."""
    import pandas as pd
    try:
        import indicators
    except ImportError:  # pragma: no cover - package-style import for research entrypoints
        from src import indicators

    return pd, indicators


def fetch_btc_candles(interval="5m", limit=30):
    """
    Fetch recent BTC 5-minute candles.
    Increased limit to 30 to allow for indicator calculation (e.g. RSI 14).
    """
    try:
        return _fetch_kraken(limit)
    except Exception as e:
        print(f"  Kraken API failed ({e}), trying Coinbase fallback...")
        try:
            return _fetch_coinbase(limit)
        except Exception as e2:
            print(f"  Coinbase also failed ({e2}), returning empty data")
            return None


def _fetch_kraken(limit):
    """Fetch from Kraken public OHLC endpoint (no auth needed).

    Returns [time, open, high, low, close, vwap, volume, count] arrays.
    Kraken returns all candles since `since` timestamp — we compute
    the right start time to get approximately `limit` candles.
    """
    # Request candles starting from (limit * 5 minutes) ago
    since = int(time.time()) - (limit + 2) * 5 * 60
    resp = requests.get(KRAKEN_OHLC, params={
        "pair": "XBTUSD",
        "interval": 5,
        "since": since,
    }, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("error") and len(data["error"]) > 0:
        raise Exception(f"Kraken error: {data['error']}")

    # Response has result key with pair name (may vary: XXBTZUSD or XBTUSD)
    result = data.get("result", {})
    pair_key = None
    for key in result:
        if key != "last":
            pair_key = key
            break

    if not pair_key or not result[pair_key]:
        raise Exception("No candle data in Kraken response")

    raw = result[pair_key]
    # Take last `limit` candles
    raw = raw[-limit:] if len(raw) > limit else raw

    candles = []
    for k in raw:
        # Kraken: [time, open, high, low, close, vwap, volume, count]
        open_time = datetime.fromtimestamp(int(k[0]), tz=timezone.utc)
        open_price = float(k[1])
        high = float(k[2])
        low = float(k[3])
        close = float(k[4])
        volume = float(k[6])

        body = abs(close - open_price)
        full_range = high - low
        direction = "UP" if close >= open_price else "DOWN"
        wick_ratio = round(1.0 - (body / full_range), 2) if full_range > 0 else 0.0
        body_pct = round((close - open_price) / open_price * 100, 4) if open_price > 0 else 0.0

        candles.append({
            "time": open_time.strftime("%H:%M"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": round(volume, 2),
            "direction": direction,
            "body_pct": body_pct,
            "wick_ratio": wick_ratio,
        })

    if not candles:
        return None

    return _compute_summary(candles)


def _fetch_coinbase(limit):
    """Fallback: Coinbase Exchange API (no auth needed for market data).

    Returns [time, low, high, open, close, volume] arrays (note different order).
    granularity=300 = 5-minute candles.
    """
    now = int(time.time())
    start = now - (limit + 2) * 5 * 60

    resp = requests.get(COINBASE_CANDLES, params={
        "granularity": 300,
        "start": start,
        "end": now,
    }, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    if not raw:
        raise Exception("Empty response from Coinbase")

    # Coinbase returns newest first — reverse to chronological
    raw.sort(key=lambda x: x[0])
    # Take last `limit`
    raw = raw[-limit:] if len(raw) > limit else raw

    candles = []
    for k in raw:
        # Coinbase: [time, low, high, open, close, volume]
        open_time = datetime.fromtimestamp(int(k[0]), tz=timezone.utc)
        low = float(k[1])
        high = float(k[2])
        open_price = float(k[3])
        close = float(k[4])
        volume = float(k[5])

        body = abs(close - open_price)
        full_range = high - low
        direction = "UP" if close >= open_price else "DOWN"
        wick_ratio = round(1.0 - (body / full_range), 2) if full_range > 0 else 0.0
        body_pct = round((close - open_price) / open_price * 100, 4) if open_price > 0 else 0.0

        candles.append({
            "time": open_time.strftime("%H:%M"),
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "volume": round(volume, 2),
            "direction": direction,
            "body_pct": body_pct,
            "wick_ratio": wick_ratio,
        })

    if not candles:
        return None

    return _compute_summary(candles)


def _compute_summary(candles):
    """Compute derived stats from a list of candles."""
    pd, indicators = _load_indicator_stack()
    df = pd.DataFrame(candles)
    # Ensure float columns for indicators
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    
    # Calculate indicators using our new module
    df = indicators.add_all_indicators(df)
    
    closes = df["close"].tolist()
    opens = df["open"].tolist()
    highs = df["high"].tolist()
    lows = df["low"].tolist()
    volumes = df["volume"].tolist()
    current_price = closes[-1]
    first_open = candles[0]["open"]

    # 1-hour change (from first of our window)
    hour_change_pct = round((current_price - first_open) / first_open * 100, 3)
    if hour_change_pct > 0.15:
        trend = "up"
    elif hour_change_pct < -0.15:
        trend = "down"
    else:
        trend = "neutral"

    # 5-min returns for volatility
    returns = []
    for i in range(1, len(closes)):
        ret = (closes[i] - closes[i - 1]) / closes[i - 1] * 100
        returns.append(ret)
    volatility = round(statistics.stdev(returns), 4) if len(returns) >= 2 else 0.0

    # Trend and patterns
    last_row = df.iloc[-1]
    last_candle = candles[-1]
    range_high = float(df["high"].max())
    range_low = float(df["low"].min())
    full_range = max(range_high - range_low, 1e-9)
    range_position = (current_price - range_low) / full_range
    avg_volume = statistics.mean(volumes) if volumes else 0.0
    last_volume = volumes[-1] if volumes else 0.0
    avg_prior_volume = statistics.mean(volumes[:-1]) if len(volumes) > 1 else avg_volume
    last_volume_ratio = (last_volume / avg_prior_volume) if avg_prior_volume > 0 else 1.0

    up_count = sum(1 for candle in candles if candle["direction"] == "UP")
    down_count = sum(1 for candle in candles if candle["direction"] == "DOWN")

    consecutive_direction = 0
    last_direction = candles[-1]["direction"]
    for candle in reversed(candles):
        if candle["direction"] == last_direction:
            consecutive_direction += 1
        else:
            break
    consecutive_dir_label = last_direction.lower() if consecutive_direction else "neutral"

    candle_ranges = [float(high) - float(low) for high, low in zip(highs, lows)]
    last_range = candle_ranges[-1] if candle_ranges else 0.0
    avg_prior_range = statistics.mean(candle_ranges[:-1]) if len(candle_ranges) > 1 else (last_range or 1.0)
    last_range_ratio = (last_range / avg_prior_range) if avg_prior_range > 0 else 1.0
    last_3_range_shrinking = False
    if len(candle_ranges) >= 3:
        a, b, c = candle_ranges[-3:]
        last_3_range_shrinking = a > b > c

    last_open = opens[-1]
    last_high = highs[-1]
    last_low = lows[-1]
    last_close = closes[-1]
    body = abs(last_close - last_open)
    upper_wick = max(last_high - max(last_open, last_close), 0.0)
    lower_wick = max(min(last_open, last_close) - last_low, 0.0)
    body_for_ratio = body if body > 1e-9 else 1e-9
    last_wick_upper_ratio = upper_wick / body_for_ratio
    last_wick_lower_ratio = lower_wick / body_for_ratio

    last_candle_pattern = "none"
    total_candle_range = last_high - last_low
    body_ratio = (body / total_candle_range) if total_candle_range > 0 else 0.0
    if body_ratio <= 0.1:
        last_candle_pattern = "doji"
    elif lower_wick > body * 2 and upper_wick <= body:
        last_candle_pattern = "hammer"
    elif upper_wick > body * 2 and lower_wick <= body:
        last_candle_pattern = "inv_hammer"
    elif len(candles) >= 2:
        prev = candles[-2]
        prev_open = float(prev["open"])
        prev_close = float(prev["close"])
        if prev_close < prev_open and last_close > last_open and last_close >= prev_open and last_open <= prev_close:
            last_candle_pattern = "engulfing_bull"
        elif prev_close > prev_open and last_close < last_open and last_open >= prev_close and last_close <= prev_open:
            last_candle_pattern = "engulfing_bear"
        elif last_high <= float(prev["high"]) and last_low >= float(prev["low"]):
            last_candle_pattern = "inside_bar"
    
    return {
        "candles": candles[-12:], # Only return last 12 for the prompt table
        "current_price": current_price,
        "1h_change_pct": hour_change_pct,
        "trend": trend,
        "volatility": volatility,
        "last_row": last_row, # Contains all technical indicators
        "consecutive_direction": consecutive_direction,
        "consecutive_dir_label": consecutive_dir_label,
        "up_count": up_count,
        "down_count": down_count,
        "last_candle": last_candle,
        "range_high": range_high,
        "range_low": range_low,
        "range_position": max(0.0, min(1.0, range_position)),
        "avg_volume": avg_volume,
        "last_volume_ratio": last_volume_ratio,
        "last_3_range_shrinking": last_3_range_shrinking,
        "last_range_ratio": last_range_ratio,
        "last_candle_pattern": last_candle_pattern,
        "last_wick_upper_ratio": last_wick_upper_ratio,
        "last_wick_lower_ratio": last_wick_lower_ratio,
    }


def format_for_prompt(data):
    """Format BTC data as a readable string for injection into agent prompts."""
    if data is None:
        return "## Recent BTC Price Action\n(Data unavailable — use market_price as your estimate)\n"

    last = data['last_row']
    
    lines = [
        "## BTC Market Technical Analysis (5m Timeframe)",
        f"- **Current BTC price:** ${data['current_price']:,.2f}",
        f"- **Current Price:** ${data['current_price']:,.2f}",
        f"- **RSI (14):** {last['rsi_14']:.1f} ({'Overbought' if last['rsi_14'] > 70 else 'Oversold' if last['rsi_14'] < 30 else 'Neutral'})",
        f"- **Bollinger Bands:** Upper: {last['bb_upper']:.0f} | Mid: {last['bb_middle']:.0f} | Lower: {last['bb_lower']:.0f}",
        f"- **MACD:** Hist: {last['macd_hist']:.2f} | Line: {last['macd']:.2f} | Signal: {last['macd_signal']:.2f}",
        f"- **KDJ:** K: {last['kdj_k']:.1f} | D: {last['kdj_d']:.1f} | J: {last['kdj_j']:.1f}",
    ]
    
    if 'mfi_14' in last:
        lines.append(f"- **MFI (14):** {last['mfi_14']:.1f} (Money Flow Index)")
    
    lines.extend([
        "",
        "## Recent Price Action (Last 12 Candles)",
        "| Time  | Open     | Close    | Dir  | Body%   | Wick  | Vol    |",
        "|-------|----------|----------|------|---------|-------|--------|",
    ])

    for c in data["candles"]:
        lines.append(
            f"| {c['time']} | {c['open']:>8,.0f} | {c['close']:>8,.0f} | {c['direction']:<4s} | {c['body_pct']:>+6.3f}% | {c['wick_ratio']:.2f}  | {c['volume']:>6.1f} |"
        )

    return "\n".join(lines)


def compute_rolling_bias(intervals=None):
    """
    Compute rolling UP% at multiple timeframes as an automatic sanity check
    against the human macro bias. Uses Kraken with Coinbase fallback.
    Returns dict with per-timeframe UP% and blend.
    """
    if intervals is None:
        intervals = {"7d": 2016, "24h": 288, "1h": 12}

    results = {}
    weights = {"7d": 0.5, "24h": 0.3, "1h": 0.2}
    blended = 0.0
    total_weight = 0.0

    for label, limit in intervals.items():
        try:
            # Kraken caps at 720 candles per request
            fetch_limit = min(limit, 720)
            since = int(time.time()) - (fetch_limit + 2) * 5 * 60
            resp = requests.get(KRAKEN_OHLC, params={
                "pair": "XBTUSD",
                "interval": 5,
                "since": since,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()

            if data.get("error") and len(data["error"]) > 0:
                raise Exception(f"Kraken error: {data['error']}")

            result = data.get("result", {})
            pair_key = None
            for key in result:
                if key != "last":
                    pair_key = key
                    break

            raw = result.get(pair_key, []) if pair_key else []
            ups = sum(1 for k in raw if float(k[4]) >= float(k[1]))  # close >= open
            total = len(raw)
            up_pct = round(ups / total, 4) if total > 0 else 0.5
            results[label] = {"up_pct": up_pct, "candles": total}
            w = weights.get(label, 0)
            blended += up_pct * w
            total_weight += w
        except Exception as e:
            results[label] = {"up_pct": 0.5, "candles": 0, "error": str(e)}
            w = weights.get(label, 0)
            blended += 0.5 * w
            total_weight += w

    results["blended"] = round(blended / total_weight, 4) if total_weight > 0 else 0.5
    return results


if __name__ == "__main__":
    print("Fetching BTC candle data...")
    data = fetch_btc_candles()
    if data:
        print(format_for_prompt(data))
    else:
        print("Failed to fetch data from any source.")
