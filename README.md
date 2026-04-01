# Price Alerts

Server-side price alert service for `CL` spread monitoring between `trade.xyz` and `ostium`.

## Rules

This service monitors `CL` and triggers a phone alert through fwalert when either condition crosses the threshold `3`.

Notification suppression windows:
- No alerts from **05:00 to 06:10 Beijing time**
- No alerts on the weekend window from **Saturday 08:00 Beijing time** through **Monday 06:00 Beijing time**

Alert rules:

1. **Open signal**: alert when spread moves from the low-reset zone to the breakout zone
   - Formula: `trade.xyz bid - ostium ask`
   - Trigger when `open_spread > OPEN_ALERT_HIGH_THRESHOLD`
   - Re-arm only after `open_spread < OPEN_ALERT_LOW_RESET`
2. **Close signal**: alert when spread moves from the high-reset zone to the breakdown zone
   - Formula: `trade.xyz ask - ostium bid`
   - Trigger when `close_spread < CLOSE_ALERT_LOW_THRESHOLD`
   - Re-arm only after `close_spread > CLOSE_ALERT_HIGH_RESET`
   - Spread alerts also respect `SPREAD_ALERT_COOLDOWN_SECONDS`
3. **Liquidation proximity alert**: alert when current mid price is within configured absolute distance of the liquidation price
   - `abs(trade_mid - TRADE_LIQUIDATION_PRICE) <= LIQUIDATION_ALERT_DISTANCE`
   - `abs(ostium_mid - OSTIUM_LIQUIDATION_PRICE) <= LIQUIDATION_ALERT_DISTANCE`
   - cooldown is controlled by `LIQUIDATION_ALERT_COOLDOWN_SECONDS`

## Alert Channel

- fwalert URL is loaded from environment variable `FWALERT_URL`
- Copy `.env.example` to `.env` and fill in your local secret value

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8790
```

## Health endpoints

- `/`
- `/health`
- `/alerts` - recent persisted alert records
- Default port: `8790`

## Alert logging

Every attempted notification is persisted to `alerts_log.jsonl`, including:
- trigger time
- event type
- suppression status
- HTTP result
- spread snapshot at trigger time

## Deployment

A sample `systemd` unit file is included as `systemd.price-alerts.service`.
