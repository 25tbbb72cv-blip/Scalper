import os
import re
import json
import logging
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---------------- CONFIG ----------------

TP_WEBHOOK_URL = os.getenv("TP_WEBHOOK_URL", "")
TP_DEFAULT_QTY = int(os.getenv("TP_DEFAULT_QTY", "1"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMA_STATE: Dict[str, Dict[str, Any]] = {}
LAST_TRADES: Dict[str, Dict[str, Any]] = {}
POSITION_STATE: Dict[str, Dict[str, Any]] = {}


# ---------------- HELPERS ----------------

def send_to_traderspost(payload: dict) -> dict:
    """Send payload to TradersPost webhook."""
    if not TP_WEBHOOK_URL:
        logger.error("TP_WEBHOOK_URL not set")
        return {"ok": False, "error": "TP_WEBHOOK_URL not set"}

    try:
        logger.info(f"Sending to TradersPost: {payload}")
        resp = requests.post(TP_WEBHOOK_URL, json=payload, timeout=5)
        return {"ok": resp.ok, "status_code": resp.status_code, "body": resp.text}
    except Exception as e:
        logger.exception(f"Error sending to TradersPost: {e}")
        return {"ok": False, "error": str(e)}


def update_ema_state_from_json(data: dict) -> None:
    ticker = data.get("ticker")
    if not ticker:
        return

    above13_raw = str(data.get("above13", data.get("above", ""))).lower()
    above13 = True if above13_raw == "true" else False

    try:
        ema13 = float(data.get("ema13", 0.0))
    except:
        ema13 = 0.0

    try:
        close = float(data.get("close", 0.0))
    except:
        close = 0.0

    EMA_STATE[ticker] = {
        "above13": above13,
        "ema13": ema13,
        "close": close,
        "time": data.get("time", ""),
    }

    logger.info(f"Updated EMA state for {ticker}: {EMA_STATE[ticker]}")


# ---------------- REGEX PARSERS ----------------

TITAN_RE = re.compile(r"(?P<ticker>[A-Z0-9_]+)\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)")
EXIT_RE  = re.compile(r"(?P<ticker>[A-Z0-9_]+)\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)")


def parse_titan_new_trade(text: str):
    m = TITAN_RE.search(text)
    if not m:
        return {}
    return {"ticker": m.group("ticker"), "price": float(m.group("price"))}


def parse_exit_signal(text: str):
    m = EXIT_RE.search(text)
    if not m:
        return {}
    return {"ticker": m.group("ticker"), "price": float(m.group("price"))}


# ---------------- TRADE HANDLING ----------------

def handle_new_trade_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]):
    pos = POSITION_STATE.get(ticker, {})

    # Case 1: Reverse if already in a trade
    if pos.get("open"):
        current_dir = pos["direction"]
        logger.info(f"Redundant signal on {ticker}. Reversing from {current_dir}.")

        # Exit
        exit_payload = {"ticker": ticker, "action": "exit", "quantity": TP_DEFAULT_QTY, "price": price}
        exit_result = send_to_traderspost(exit_payload)

        # Reverse
        new_dir = "sell" if current_dir == "buy" else "buy"
        entry_payload = {"ticker": ticker, "action": new_dir, "quantity": TP_DEFAULT_QTY, "price": price}
        entry_result = send_to_traderspost(entry_payload)

        POSITION_STATE[ticker] = {"open": True, "direction": new_dir, "price": price}

        return {"ok": exit_result["ok"] and entry_result["ok"]}

    # Case 2: New clean entry
    ema = EMA_STATE.get(ticker)
    if not ema:
        return {"ok": True, "skipped": "no EMA"}

    direction = "buy" if ema["above13"] else "sell"
    payload = {"ticker": ticker, "action": direction, "quantity": TP_DEFAULT_QTY, "price": price}
    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {"open": True, "direction": direction, "price": price}

    return {"ok": result["ok"]}


def handle_exit_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]):
    payload = {"ticker": ticker, "action": "exit", "price": price}
    result = send_to_traderspost(payload)
    POSITION_STATE[ticker] = {"open": False, "direction": None}
    return {"ok": result["ok"]}


# ---------------- ROUTES ----------------

@app.route("/webhook", methods=["POST"])
def webhook():
    raw = request.get_data(as_text=True)

    try:
        data = json.loads(raw)
        if data.get("type") == "ema_update":
            update_ema_state_from_json(data)
            return {"ok": True}
    except:
        pass

    t = parse_titan_new_trade(raw)
    if t:
        return handle_new_trade_for_ticker(t["ticker"], t["price"], None)

    e = parse_exit_signal(raw)
    if e:
        return handle_exit_for_ticker(e["ticker"], e["price"], None)

    return {"ok": False, "error": "unrecognized"}


@app.route("/")
def health():
    return {"ok": True, "message": "Titan bot running"}


@app.route("/dashboard")
def dashboard():
    return {
        "ema_state": EMA_STATE,
        "positions": POSITION_STATE,
        "last_trades": LAST_TRADES,
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
