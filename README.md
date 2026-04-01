# Price Alerts

Server-side price alert service for `CL` spread monitoring between `trade.xyz` and `ostium`.

## Rules

This service monitors `CL` and triggers a phone alert through fwalert when either condition crosses the threshold `3`:

1. **Open signal**: alert when spread moves from `< 3` to `> 3`
   - Formula: `trade.xyz bid - ostium ask`
2. **Close signal**: alert when spread moves from `> 3` to `< 3`
   - Formula: `trade.xyz ask - ostium bid`

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
- Default port: `8790`

## Deployment

A sample `systemd` unit file is included as `systemd.price-alerts.service`.
