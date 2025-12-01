import os
import re
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────
# CONFIG – provided via env vars (set these in Railway)
# ─────────────────────────────
TRADERSPOST_WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL")
POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY")

# Symbol your TradersPost bot trades (TopstepX side)
TICKER = os.environ.get("TRADE_TICKER", "MNQ")

# Polygon futures ticker for the MNQ contract you trade.
# For MNQZ2025 this is typically "X.MNQZ25".
POLYGON_FUT_TICKER = os.environ.get("POLYGON_FUT_TICKER", "X.MNQZ25")

if not TRADERSPOST_WEBHOOK_URL:
    raise RuntimeError("TRADERSPOST_WEBHOOK_URL env var is required")
if not POLYGON_API_KEY:
    raise RuntimeError("POLYGON_API_KEY env var is required")


# ─────────────────────────────
# Helpers
# ─────────────────────────────

def get_price_and_ema13():
    """
    Fetch recent 1-minute candles for the MNQ future from Polygon
    and compute EMA(13). Returns (last_price, ema13).
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(minutes=90)

    url = (
        f"https://api.polygon.io/v2/aggs/ticker/{POLYGON_FUT_TICKER}"
        f"/range/1/minute/{start.strftime('%Y-%m-%d')}/{now.strftime('%Y-%m-%d')}"
        f"?adjusted=true&sort=asc&limit=5000&apiKey={POLYGON_API_KEY}"
    )

    resp = requests.get(url, timeout=5)
    if resp.status_code != 200:
        raise RuntimeError(f"Polygon error {resp.status_code}: {resp.text}")

    data = resp.json()
    results = data.get("results", [])
    if len(results) < 14:
        raise RuntimeError("Not enough candles from Polygon to compute EMA13")

    closes = [bar["c"] for bar in results]

    period = 13
    k = 2 / (period + 1)

    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = (price - ema) * k + ema

    last_price = closes[-1]
    return last_price, ema


def extract_price_from_alert(text: str):
    """
    Optionally pull 'Price = 25269.25' out of the alert text.
    Used for exit messages if available.
    """
    m = re.search(r"price\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def send_to_traderspost(action: str, price: float | None = None):
    """
    Sends JSON to TradersPost in the format you showed:

    {
      "ticker": "MNQ",
      "action": "buy" / "sell" / "exit",
      "price": 25269.25,
      "time": "...",
      "interval": "5m"
    }
    """
    payload = {
        "ticker": TICKER,
        "action": action,
        "time": datetime.now(timezone.utc).isoformat(),
        "interval": "5m",
    }
    if price is not None:
        payload["price"] = price

    print("Sending to TradersPost:", payload)
    resp = requests.post(TRADERSPOST_WEBHOOK_URL, json=payload, timeout=5)
    print("TradersPost response:", resp.status_code, resp.text)
    resp.raise_for_status()


# ─────────────────────────────
# Main webhook – entry + exit
# ─────────────────────────────

@app.post("/tv")
def tv_webhook():
    """
    Handles both Titan GT entry and Exit indicator alerts:

    - If message contains 'Exit Signal'  -> send 'exit'
    - If message contains 'New Trade Design' -> EMA(13) rule decides buy/sell
    """
    raw = request.data.decode("utf-8", errors="ignore")
    low = raw.lower()
    print("Received TradingView alert:", raw)

    try:
        # EXIT
        if "exit signal" in low:
            price = extract_price_from_alert(raw)
            send_to_traderspost("exit", price)
            return jsonify(ok=True, type="exit", price=price)

        # ENTRY (New Trade Design)
        if "new trade design" in low:
            price, ema13 = get_price_and_ema13()
            action = "buy" if price > ema13 else "sell"
            print(f"EMA13 rule: price={price}, ema13={ema13}, action={action}")
            send_to_traderspost(action, price)
            return jsonify(ok=True, type="entry", action=action, price=price, ema13=ema13)

        # Anything else: ignore
        print("Alert ignored (no Exit Signal or New Trade Design text).")
        return jsonify(ok=False, error="unknown alert type"), 400

    except Exception as e:
        print("Error in webhook:", repr(e))
        return jsonify(ok=False, error=str(e)), 500


@app.get("/")
def health():
    return "MNQ EMA13 entry/exit bridge is running.\n"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=True)
