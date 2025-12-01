# Repo1 – MNQ EMA13 TradingView → TradersPost Bridge

This is a small Flask app you deploy to Railway (or any Python host) to bridge
TradingView alerts into TradersPost / TopstepX for MNQ using an EMA(13) rule.

## What it does

- Listens for TradingView webhooks at `POST /tv`
- If the alert text contains **"New Trade Design"**:
  - Fetches recent MNQZ futures data from Polygon
  - Computes EMA(13)
  - If price > EMA13 → sends a **buy** signal to TradersPost
  - If price < EMA13 → sends a **sell** signal to TradersPost
- If the alert text contains **"Exit Signal"**:
  - Sends an **exit** signal to TradersPost (to flatten the position)

## Files

- `app.py` – main Flask app
- `requirements.txt` – Python dependencies
- `Procfile` – tells Railway how to run the app

## Environment variables (set in Railway)

- `TRADERSPOST_WEBHOOK_URL` – your TradersPost webhook URL
- `POLYGON_API_KEY` – your Polygon.io API key
- `TRADE_TICKER` – symbol TradersPost should trade (e.g. `MNQ`)
- `POLYGON_FUT_TICKER` – Polygon futures ticker for MNQZ (e.g. `X.MNQZ25`)

## TradingView setup

Create **two** alerts, both pointing to the same webhook URL:

- Webhook URL (example): `https://YOUR-RAILWAY-APP.up.railway.app/tv`

1. **Entry / Reverse**
   - Indicator: Titan GT
   - Condition: `New Trade Design`
   - Once per bar close
   - Webhook URL: `/tv` endpoint above

2. **Exit**
   - Indicator: your Exit indicator
   - Condition: `Exit Signal`
   - Once per bar close
   - Webhook URL: `/tv` endpoint above

The alert **message** stays as defined by the indicators (locked).
The app only checks for the text `New Trade Design` or `Exit Signal`.

## Deploying to Railway (high-level)

1. Create a new project in Railway.
2. Add a new service and connect this repo or upload these files.
3. Set the environment variables listed above.
4. Deploy and grab your public URL.
5. Use `https://YOUR-RAILWAY-APP.up.railway.app/tv` as the webhook
   URL in your TradingView alerts.
