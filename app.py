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
TP_DEFAULT_QTY = int(os.getenv("TP_DEFAULT_QTY", "1"))

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
        return {
            "ok": resp.ok,
            "status_code": resp.status_code,
            "body": resp.text,
        }
    except Exception as e:
        logger.exception("Error sending to TradersPost: %s", e)
        return {"ok": False, "error": str(e)}


def update_ema_state_from_json(data: dict) -> Optional[str]:
    """Update EMA_STATE from JSON and return ticker name."""
    ticker = data.get("ticker")
    if not ticker:
        logger.warning("ema_update without ticker: %s", data)
        return None

    # Accept either 'above13' or generic 'above'
    above13_raw = str(data.get("above13", data.get("above", ""))).lower()
    above13 = (
        above13_raw == "true"
        or above13_raw == "1"
        or above13_raw == "yes"
    )

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
        "received_at": utc_ts(),  # <-- when this EMA hit the webhook
    }

    logger.info("Updated EMA state for %s: %s", ticker, EMA_STATE[ticker])
    return ticker


# ------------ PARSERS (TITAN TEXT) ------------

# Example:  MNQZ2025 New Trade Design , Price = 25787.50
TITAN_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+New Trade Design\s*,\s*Price\s*=\s*(?P<price>[0-9.]+)"
)

# Example:  MNQZ2025 Exit Signal , Price = 25787.00
EXIT_RE = re.compile(
    r"(?P<ticker>[A-Z0-9_]+)\s+Exit Signal\s*,?\s*Price\s*=\s*(?P<price>[0-9.]+)"
)


def parse_titan_new_trade(text: str) -> Dict[str, Any]:
    m = TITAN_RE.search(text)
    if not m:
        return {}
    return {
        "ticker": m.group("ticker"),
        "price": float(m.group("price")),
    }


def parse_exit_signal(text: str) -> Dict[str, Any]:
    m = EXIT_RE.search(text)
    if not m:
        return {}
    return {
        "ticker": m.group("ticker"),
        "price": float(m.group("price")),
    }


# ------------ TRADE HANDLING ------------

def handle_new_trade_for_ticker(
    ticker: str,
    price: Optional[float],
    time_str: Optional[str],
) -> dict:
    """
    Use current EMA13 state to decide direction, plus redundant trade behavior.

    - If NO open position:
        - Long  if above13 == True
        - Short if above13 == False

    - If position IS already open:
        - Exit current position
        - Then open new position in the opposite direction
    """

    pos = POSITION_STATE.get(ticker, {})

    # If in a position already → EXIT + REVERSE
    if pos.get("open"):
        current_dir = pos.get("direction")  # 'buy' or 'sell'
        logger.info(
            "Redundant signal on %s while %s. Exit + reverse.",
            ticker,
            current_dir,
        )

        exit_payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": "exit",
        }
        if TP_DEFAULT_QTY > 0:
            exit_payload["quantity"] = TP_DEFAULT_QTY
        if price is not None:
            exit_payload["price"] = price

        exit_result = send_to_traderspost(exit_payload)

        new_dir = "sell" if current_dir == "buy" else "buy"

        entry_payload: Dict[str, Any] = {
            "ticker": ticker,
            "action": new_dir,
        }
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

        LAST_TRADES[ticker] = {
            "event": "exit_and_reverse",
            "from": current_dir,
            "to": new_dir,
            "price": price,
            "time": time_str,
            "exit_result": exit_result,
            "entry_result": entry_result,
        }

        ok = exit_result.get("ok", False) and entry_result.get("ok", False)
        return {"ok": ok, "event": "exit_and_reverse"}

    # Fresh entry: use EMA13 direction
    ema_info = EMA_STATE.get(ticker)
    if not ema_info:
        logger.warning("No EMA state for %s, skipping trade", ticker)
        return {"ok": True, "skipped": "no_ema_state"}

    above13 = ema_info.get("above13", False)
    direction = "buy" if above13 else "sell"

    payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": direction,
    }
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

    LAST_TRADES[ticker] = {
        "event": "new_trade",
        "direction": direction,
        "price": price,
        "above13": above13,
        "ema_snapshot": ema_info,
        "time": time_str,
        "tp_result": result,
    }

    return {"ok": result.get("ok", False), "event": "new_trade"}


def handle_exit_for_ticker(
    ticker: str,
    price: Optional[float],
    time_str: Optional[str],
) -> dict:
    payload: Dict[str, Any] = {
        "ticker": ticker,
        "action": "exit",
    }
    if price is not None:
        payload["price"] = price

    result = send_to_traderspost(payload)

    POSITION_STATE[ticker] = {
        "open": False,
        "direction": None,
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
    data = None    # we'll reuse below if json.loads works
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
                logger.info(
                    "Consuming pending trade for %s with latest EMA state.",
                    ticker,
                )
                result = handle_new_trade_for_ticker(
                    ticker,
                    pending.get("price"),
                    pending.get("time"),
                )
                return jsonify(result), 200

            # No pending trade – just an EMA update
            return jsonify({"ok": True, "event": "ema_update_only"}), 200

        # Unknown JSON type
        return (
            jsonify({"ok": False, "error": f"unknown json type {data.get('type')}"}),
            400,
        )

    # 2) Plain text: Titan / Exit

    # Titan “New Trade Design”
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
            # Treat EMA as "fresh" if it hit within the last few seconds
            if age <= 5.0:
                use_immediate = True

        if use_immediate:
            logger.info(
                "Titan trade for %s with fresh EMA (age=%.2fs). Firing immediately.",
                ticker,
                age,
            )
            result = handle_new_trade_for_ticker(ticker, price, None)
            return jsonify(result), 200

        # Otherwise, EMA is stale or missing → queue as pending
        PENDING_TRADES[ticker] = {
            "price": price,
            "time": None,
            "created_at": now,
        }
        logger.info("Stored pending trade for %s: %s", ticker, PENDING_TRADES[ticker])
        return jsonify({"ok": True, "event": "pending_trade_stored"}), 200

    # Exit Signal → exit immediately
    exit_info = parse_exit_signal(raw_body)
    if exit_info:
        ticker = exit_info["ticker"]
        price = exit_info.get("price")
        result = handle_exit_for_ticker(ticker, price, None)
        return jsonify(result), 200

    return jsonify({"ok": False, "error": "unrecognized payload"}), 400


@app.route("/", methods=["GET"])
def health():
    return jsonify(
        {
            "ok": True,
            "message": "Titan Bot webhook running (EMA13 + pending queue + freshness)",
        }
    )


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
