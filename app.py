import os
import re
import json
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ------------ CONFIG ------------

TP_WEBHOOK_URL = os.getenv("TP_WEBHOOK_URL", "")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Latest EMA state per ticker
EMA_STATE: Dict[str, Dict[str, Any]] = {}

# Last trade / events (optional debugging)
LAST_TRADES: Dict[str, Dict[str, Any]] = {}

# Simple position state per ticker
POSITION_STATE: Dict[str, Dict[str, Any]] = {}

# Titan “New Trade Design” waiting for next EMA update
PENDING_TRADES: Dict[str, Dict[str, Any]] = {}

# ------------ HELPERS ------------

def utc_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def send_to_traderspost(payload: dict) -> dict:
    """Send payload to TradersPost webhook."""
    if not TP_WEBHOOK_URL:
        logger.error("TP_WEBHOOK_URL not set")
        return {"ok": False, "error": "TP_WEBHOOK_URL not set"}

    try:
        logger.info("Sending to TradersPost: %s", payload)
        resp = requests.post(TP_WEBHOOK_URL, json=payload, timeout=5)
        return {"ok": resp.ok, "status_code": resp.status_code, "body": resp.text}
    except Exception as e:
        logger.exception("Error sending to TradersPost: %s", e)
        return {"ok": False, "error": str(e)}


def _parse_boolish(v: Any) -> bool:
    s = str(v).strip().lower()
    return s in ("true", "1", "yes", "y", "t")


def update_ema_state_from_json(data: dict) -> Optional[str]:
    """Update EMA_STATE from JSON and return ticker name."""
    ticker = data.get("ticker")
    if not ticker:
        logger.warning("ema_update without ticker: %s", data)
        return None

    above13 = _parse_boolish(data.get("above13", data.get("above", "")))

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
        "received_at": utc_ts(),
    }

    logger.info("Updated EMA state for %s: %s", ticker, EMA_STATE[ticker])
    return ticker


def desired_direction_from_ema(ticker: str) -> Optional[str]:
    """Return 'buy' or 'sell' based on EMA_STATE above13, or None if missing."""
    state = EMA_STATE.get(ticker)
    if not state:
        return None
    return "buy" if bool(state.get("above13", False)) else "sell"


# ------------ PARSERS (TITAN TEXT) ------------

# Updated ticker regex to support common TradingView formats like:
# ES1! / CME_MINI:MNQZ2025 / BINANCE:BTCUSDT / etc.
TICKER_PATTERN = r"(?P<ticker>[A-Za-z0-9:_\.\-!]+)"

# Example:  MNQZ2025 New Trade Design , Price = 25787.50
TITAN_RE = re.compile(
    TICKER_PATTERN + r"\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)"
)

# Example:  MNQZ2025 Exit Signal , Price = 25787.00
EXIT_RE = re.compile(
    TICKER_PATTERN + r"\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)"
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

def flatten_position(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """Exit 1 contract (single-contract trader)."""
    payload: Dict[str, Any] = {"ticker": ticker, "action": "exit", "quantity": 1}
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": False,
        "direction": None,
        "qty": 0,
        "closed_time": time_str,
        "price": price,
    }

    return {"ok": result.get("ok", False), "tp_result": result, "exited_qty": 1}


def enter_position(ticker: str, direction: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """Enter 1 contract (single-contract trader)."""
    payload: Dict[str, Any] = {"ticker": ticker, "action": direction, "quantity": 1}
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": True,
        "direction": direction,
        "qty": 1,
        "opened_time": time_str,
        "price": price,
    }

    return {"ok": result.get("ok", False), "tp_result": result, "entered_qty": 1}


def handle_new_trade_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """
    Titan New Trade Design behavior (single contract):

    - Determine desired direction from EMA13.
    - If NO open position: enter desired direction (qty=1).
    - If position IS open: flatten first, then enter desired direction (qty=1).
    """
    desired = desired_direction_from_ema(ticker)
    if not desired:
        logger.warning("No EMA state for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "no_ema_state"}

    pos = POSITION_STATE.get(ticker, {})
    if pos.get("open"):
        current_dir = pos.get("direction")
        logger.info("New Trade Design on %s while in %s. Flatten then enter %s.", ticker, current_dir, desired)

        exit_res = flatten_position(ticker, price, time_str)
        entry_res = enter_position(ticker, desired, price, time_str)

        LAST_TRADES[ticker] = {
            "event": "flatten_then_new_entry",
            "from": current_dir,
            "to": desired,
            "price": price,
            "time": time_str,
            "exit": exit_res,
            "entry": entry_res,
            "ema_snapshot": EMA_STATE.get(ticker),
        }

        ok = bool(exit_res.get("ok")) and bool(entry_res.get("ok"))
        return {"ok": ok, "event": "flatten_then_new_entry", "to": desired}

    entry_res = enter_position(ticker, desired, price, time_str)

    LAST_TRADES[ticker] = {
        "event": "new_trade",
        "direction": desired,
        "price": price,
        "time": time_str,
        "ema_snapshot": EMA_STATE.get(ticker),
        "tp_result": entry_res.get("tp_result"),
    }

    return {"ok": entry_res.get("ok", False), "event": "new_trade", "direction": desired}


def handle_exit_for_ticker(ticker: str, price: Optional[float], time_str: Optional[str]) -> dict:
    """
    Exit Signal behavior (single contract):
    - Exit immediately (qty=1) whenever an exit signal arrives.
    """
    payload: Dict[str, Any] = {"ticker": ticker, "action": "exit", "quantity": 1}
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": False,
        "direction": None,
        "qty": 0,
        "closed_time": time_str,
        "price": price,
    }

    LAST_TRADES[ticker] = {
        "event": "exit",
        "price": price,
        "time": time_str,
        "tp_result": result,
    }

    return {"ok": result.get("ok", False), "event": "exit"}


# ------------ ROUTES ------------

@app.route("/webhook", methods=["POST"])
def webhook():
    raw_body = request.get_data(as_text=True) or ""
    logger.info("Incoming body: %r", raw_body)

    # 1) Try JSON (EMA updates etc.)
    data = None
    try:
        data = json.loads(raw_body)
    except Exception:
        data = None

    if isinstance(data, dict) and data:
        if data.get("type") == "ema_update":
            ticker = update_ema_state_from_json(data)

            # If we have a pending Titan trade for this ticker, fire it now
            if ticker and ticker in PENDING_TRADES:
                pending = PENDING_TRADES.pop(ticker)
                logger.info("Consuming pending trade for %s with latest EMA state.", ticker)
                result = handle_new_trade_for_ticker(ticker, pending.get("price"), pending.get("time"))
                return jsonify(result), 200

            return jsonify({"ok": True, "event": "ema_update_only"}), 200

        return jsonify({"ok": False, "error": f"unknown json type {data.get('type')}"}), 400

    # 2) Plain text: Titan / Exit

    titan = parse_titan_new_trade(raw_body)
    if titan:
        ticker = titan["ticker"]
        price = titan.get("price")
        now = utc_ts()

        state = EMA_STATE.get(ticker)
        use_immediate = False
        age = None

        if state and "received_at" in state:
            age = now - state["received_at"]
            if age <= 5.0:
                use_immediate = True

        if use_immediate:
            logger.info("Titan trade for %s with fresh EMA (age=%.2fs). Firing immediately.", ticker, age)
            result = handle_new_trade_for_ticker(ticker, price, None)
            return jsonify(result), 200

        # EMA stale/missing -> queue pending
        PENDING_TRADES[ticker] = {"price": price, "time": None, "created_at": now}
        logger.info("Stored pending trade for %s: %s", ticker, PENDING_TRADES[ticker])
        return jsonify({"ok": True, "event": "pending_trade_stored"}), 200

    exit_info = parse_exit_signal(raw_body)
    if exit_info:
        ticker = exit_info["ticker"]
        price = exit_info.get("price")
        result = handle_exit_for_ticker(ticker, price, None)
        return jsonify(result), 200

    return jsonify({"ok": False, "error": "unrecognized payload"}), 400


@app.route("/", methods=["GET"])
def health():
    return jsonify({"ok": True, "message": "Titan Bot webhook running (EMA13 + pending queue + single contract + updated ticker regex)"})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return jsonify(
        {
            "ema_state": EMA_STATE,
            "last_trades": LAST_TRADES,
            "positions": POSITION_STATE,
            "pending_trades": PENDING_TRADES,
        }
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
