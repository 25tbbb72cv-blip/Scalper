import os
import re
import json
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ─────────────────────────────
# Config from environment
# ─────────────────────────────

TRADERSPOST_WEBHOOK_URL = os.environ.get("TRADERSPOST_WEBHOOK_URL")
TICKER = os.environ.get("TRADE_TICKER", "MNQ")  # Symbol that TradersPost trades

if not TRADERSPOST_WEBHOOK_URL:
    raise RuntimeError("TRADERSPOST_WEBHOOK_URL env var is required")

# Latest EMA state per TradingView ticker
# Example:
# EMA_STATE["MNQZ2025"] = {
#     "above": True,
#     "ema13": 25310.25,
#     "close": 25316.00,
#     "time": "2025-01-01T12:34:00Z",
# }
EMA_STATE: dict = {}


# ─────────────────────────────
# Helper functions
# ─────────────────────────────

def extract_price_from_alert(text: str):
    """Extract 'Price = 25302.00' from alert text."""
    m = re.search(r"price\s*=\s*([0-9]+(?:\.[0-9]+)?)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def extract_ticker_from_alert(text: str):
    """
    Extract TradingView ticker from the start of the alert text.

    Example:
        'MNQZ2025 New Trade Design , Price = 25302.00'
        -> 'MNQZ2025'
    """
    text = text.strip()
    if not text:
        return None
    token = re.split(r"[\s,]+", text)[0]
    return token or None


def send_to_traderspost(action: str, price: float | None = None):
    """
    Send an order instruction to TradersPost.

    action: "buy", "sell", or "exit"
    price: optional, last known price
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
    resp = requests.post(TRADERSPOST_WEBHOOK_URL, json=payload, timeout=8)
    print("TradersPost response:", resp.status_code, resp.text)
    resp.raise_for_status()


# ─────────────────────────────
# Webhook endpoint
# ─────────────────────────────

@app.post("/tv")
def tv_webhook():
    raw = request.data.decode("utf-8", errors="ignore")
    print("Webhook raw body:", raw)

    # Try to parse JSON (EMA updates)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = None

    # 1) EMA updates from EMA13 State Broadcaster
    if isinstance(data, dict) and data.get("type") == "ema_update":
        tv_ticker = data.get("ticker")
        if not tv_ticker:
            return jsonify(ok=False, error="ema_update missing ticker"), 400

        try:
            above = bool(data.get("above"))
            ema_val = float(data.get("ema13"))
            close_val = float(data.get("close"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="invalid EMA payload"), 400

        t = data.get("time")
        EMA_STATE[tv_ticker] = {
            "above": above,
            "ema13": ema_val,
            "close": close_val,
            "time": t,
        }

        print(f"Updated EMA state for {tv_ticker}: {EMA_STATE[tv_ticker]}")
        return jsonify(ok=True, type="ema_update")

    # 2) Text alerts from proprietary indicator
    low = raw.lower()

    # Exit signal -> exit position
    if "exit signal" in low:
        price = extract_price_from_alert(raw)
        print("Exit signal received. Price:", price)
        send_to_traderspost("exit", price)
        return jsonify(ok=True, type="exit", price=price)

    # New Trade Design -> decide buy/sell based on EMA
    if "new trade design" in low:
        tv_ticker = extract_ticker_from_alert(raw)
        ema_state = EMA_STATE.get(tv_ticker)

        if ema_state is None:
            # No EMA update yet; choose a safe default behavior
            print(f"No EMA state for {tv_ticker}. Defaulting to 'buy'.")
            action = "buy"
            price = extract_price_from_alert(raw)
        else:
            above = ema_state["above"]
            action = "buy" if above else "sell"
            price = extract_price_from_alert(raw) or ema_state["close"]

            print(
                f"New Trade Design for {tv_ticker}: "
                f"price={price}, ema13={ema_state['ema13']}, "
                f"above={above}, action={action}"
            )

        send_to_traderspost(action, price)
        return jsonify(ok=True, type="entry", action=action, price=price)

    # Unknown alert type
    print("Alert ignored (no ema_update, Exit Signal, or New Trade Design).")
    return jsonify(ok=False, error="unknown alert format"), 400


@app.get("/")
def health():
    return "EMA13 TradingView → Railway → TradersPost bridge is running.\n"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "3000"))
    app.run(host="0.0.0.0", port=port, debug=True)
