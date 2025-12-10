import os
import re
import json
import logging
from typing import Dict, Any, Optional

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ------------ CONFIG ------------

TP_WEBHOOK_URL = os.getenv("TP_WEBHOOK_URL", "")
TP_DEFAULT_QTY = int(os.getenv("TP_DEFAULT_QTY", "1"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EMA_STATE: Dict[str, Dict[str, Any]] = {}
LAST_TRADES: Dict[str, Dict[str, Any]] = {}
POSITION_STATE: Dict[str, Dict[str, Any]] = {}


# ------------ HELPERS ------------

def send_to_traderspost(payload: dict) -> dict:
    # Simple log + POST, no docstrings, no weird quotes
    if not TP_WEBHOOK_URL:
        logger.error("TP_WEBHOOK_URL not set")
        return {"ok": False, "error": "TP_WEBHOOK_URL not set"}

    try:
        logger.info("Sending to TradersPost: %s", payload)
        resp = requests.post(TP_WEBHOOK_URL, json=payload, timeout=5)
        return {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "body": resp.text,
        }
    except Exception as e:
        logger.exception("Error sending to TradersPost: %s", e)
        return {"ok": False, "error": str(e)}


def update_ema_state_from_json(data: dict) -> None:
    ticker = data.get("ticker")
    if not ticker:
        return

    # Accept either 'above13' or 'above'
    above13_raw = str(data.get("above13", data.get("above", ""))).lower()
    above13 = (above13_raw == "true")

    try:
        ema13 = float(data.get("ema13", 0.0))
    except Exception:
        ema13 = 0.0

    try:
        close = float(data.get("close", 0.0))
    except Exception:
        close = 0.0

    EMA_STATE[ticker] = {
        "above13": above13,
        "ema13": ema13,
        "close": close,
        "time": data.get("time", ""),
    }

    logger.info("Updated EMA state for %s: %s", ticker, EMA_STATE[ticker])


# ------------ PARSERS ------------

TITAN_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)"
)

EXIT_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)"
)


def parse_titan_new_trade(text: str) -> Dict[str, Any]:
    m = TITAN_RE.search(text)
    if not m:
        return {}
    return {"ticker": m.group("ticker"), "price": float(m.group("price"))}


def parse_exit_signal(text: str) -> Dict[str, Any]:
    m = EXIT_RE.search(text)
    if not m:
        return {}
    return {"ticker": m.group("ticker"), "price": float(m.group("price"))}


# ------------ TRADE HANDLING ------------

def handle_new_trade_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    pos = POSITION_STATE.get(ticker, {})

    # Case 1: already in a trade -> exit + reverse
    if pos.get("open"):
        current_dir = pos.get("direction")
        logger.info("Redundant signal on %s while %s. Exit + reverse.", ticker, current_dir)

        exit_payload = {"ticker": ticker, "action": "exit"}
        if TP_DEFAULT_QTY > 0:
            exit_payload["quantity"] = TP_DEFAULT_QTY
        if price is not None:
            exit_payload["price"] = price

        exit_result = send_to_traderspost(exit_payload)

        new_dir = "sell" if current_dir == "buy" else "buy"
        entry_payload = {"ticker": ticker, "action": new_dir}
        if TP_DEFAULT_QTY > 0:
            entry_payload["quantity"] = TP_DEFAULT_QTY
        if price is not None:
            entry_payload["price"] = price

        entry_result = send_to_traderspost(entry_payload)

        POSITION_STATE[ticker] = {
            "open": True,
            "direction": new_dir,
            "opened_time": time_str,
            "price": price,
        }

        ok = exit_result.get("ok", False) and entry_result.get("ok", False)
        return {"ok": ok, "event": "exit_and_reverse"}

    # Case 2: fresh entry, use EMA13 direction
    ema_info = EMA_STATE.get(ticker)
    if not ema_info:
        logger.warning("No EMA state for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "no_ema_state"}

    above13 = ema_info.get("above13", False)
    direction = "buy" if above13 else "sell"

    payload = {"ticker": ticker, "action": direction}
    if TP_DEFAULT_QTY > 0:
        payload["quantity"] = TP_DEFAULT_QTY
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": True,
        "direction": direction,
        "opened_time": time_str,
        "price": price,
    }

    return {"ok": result.get("ok", False), "event": "new_trade"}


def handle_exit_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    payload = {"ticker": ticker, "action": "exit"}
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": False,
        "direction": None,
        "closed_time": time_str,
        "price": price,
    }

    return {"ok": result.get("ok", False), "event": "exit"}


# ------------ ROUTES ------------

@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(as_text=True) or ""
    logger.info("Incoming body: %r", raw_body)

    # Try JSON first
    try:
        data = json.loads(raw_body)
    except Exception:
        data = None

    if isinstance(data, dict) and data:
        if data.get("type") == "ema_update":
            update_ema_state_from_json(data)
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "unknown json type"}), 400

    # Titan new trade (plain text)
    titan = parse_titan_new_trade(raw_body)
    if titan:
        res = handle_new_trade_for_ticker(titan["ticker"], titan["price"], None)
        return jsonify(res), (200 if res.get("ok") else 500)

    # Exit signal
    exit_info = parse_exit_signal(raw_body)
    if exit_info:
        res = handle_exit_for_ticker(exit_info["ticker"], exit_info["price"], None)
        return jsonify(res), (200 if res.get("ok") else 500)

    return jsonify({"ok": False, "error": "unrecognized payload"}), 400


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "Titan Bot webhook running"})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return jsonify({
        "ema_state": EMA_STATE,
        "last_trades": LAST_TRADES,
        "positions": POSITION_STATE,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
