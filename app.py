from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI

TRADE_XYZ_API_URL = "https://api.hyperliquid.xyz/info"
OSTIUM_METADATA_BASE = "https://metadata-backend.ostium.io"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("price-alerts")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def load_dotenv() -> None:
    env_path = Path(__file__).with_name('.env')
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()
FWALERT_URL = os.getenv("FWALERT_URL", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
THRESHOLD = float(os.getenv("THRESHOLD", "3"))
SYMBOL = os.getenv("SYMBOL", "CL")


@dataclass
class Snapshot:
    trade_bid: float | None = None
    trade_ask: float | None = None
    ostium_bid: float | None = None
    ostium_ask: float | None = None
    open_spread: float | None = None
    close_spread: float | None = None
    timestamp: float | None = None


state: dict[str, Any] = {
    "running": False,
    "last_snapshot": None,
    "last_error": None,
    "last_alert": None,
    "open_regime": None,
    "close_regime": None,
    "started_at": None,
    "loop_count": 0,
}

app = FastAPI(title="price-alerts")


def fetch_trade_xyz_cl() -> tuple[float, float]:
    response = requests.post(
        TRADE_XYZ_API_URL,
        headers={"Content-Type": "application/json"},
        json={"type": "metaAndAssetCtxs", "dex": "xyz"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    meta = data[0] if len(data) > 0 else {}
    asset_ctxs = data[1] if len(data) > 1 else []
    universe = meta.get("universe", [])

    for idx, asset in enumerate(universe):
        coin = asset.get("name", "")
        normalized_coin = coin.split(":", 1)[1] if coin.startswith("xyz:") else coin
        if normalized_coin != SYMBOL:
            continue
        ctx = asset_ctxs[idx] if idx < len(asset_ctxs) else {}
        impact_pxs = ctx.get("impactPxs") or []
        if len(impact_pxs) < 2:
            break
        bid = float(impact_pxs[0])
        ask = float(impact_pxs[1])
        return bid, ask

    raise RuntimeError(f"{SYMBOL} not found on trade.xyz")


def fetch_ostium_cl() -> tuple[float, float]:
    response = requests.get(f"{OSTIUM_METADATA_BASE}/PricePublish/latest-prices", timeout=30)
    response.raise_for_status()
    prices = response.json()

    for item in prices:
        if item.get("from") == SYMBOL and item.get("to") == "USD":
            bid = float(item["bid"])
            ask = float(item["ask"])
            return bid, ask

    raise RuntimeError(f"{SYMBOL}/USD not found on ostium")


def in_suppression_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(BEIJING_TZ)

    # Weekend mute window: Saturday 08:00 BJT through all of Sunday.
    if now.weekday() == 5 and (now.hour > 8 or (now.hour == 8 and now.minute >= 0)):
        return True
    if now.weekday() == 6:
        return True

    # Daily mute window: 05:00-06:10 BJT.
    minutes = now.hour * 60 + now.minute
    return 5 * 60 <= minutes <= (6 * 60 + 10)


def trigger_phone_alert(event: str, snapshot: Snapshot) -> None:
    if in_suppression_window():
        logger.info("Suppressed alert for event=%s during mute window", event)
        state["last_alert"] = {
            "event": event,
            "timestamp": time.time(),
            "suppressed": True,
            "snapshot": asdict(snapshot),
        }
        return

    try:
        response = requests.get(FWALERT_URL, timeout=15)
        response.raise_for_status()
        state["last_alert"] = {
            "event": event,
            "timestamp": time.time(),
            "status_code": response.status_code,
            "suppressed": False,
            "snapshot": asdict(snapshot),
        }
        logger.warning("Triggered fwalert for event=%s snapshot=%s", event, asdict(snapshot))
    except Exception as exc:
        state["last_error"] = f"alert_failed: {exc}"
        logger.exception("Failed to trigger fwalert")


def classify(value: float) -> str:
    return "gt" if value > THRESHOLD else "le"


def monitor_loop() -> None:
    state["running"] = True
    state["started_at"] = time.time()
    logger.info("Starting monitor loop for %s", SYMBOL)

    while True:
        try:
            trade_bid, trade_ask = fetch_trade_xyz_cl()
            ostium_bid, ostium_ask = fetch_ostium_cl()

            snapshot = Snapshot(
                trade_bid=trade_bid,
                trade_ask=trade_ask,
                ostium_bid=ostium_bid,
                ostium_ask=ostium_ask,
                open_spread=trade_bid - ostium_ask,
                close_spread=trade_ask - ostium_bid,
                timestamp=time.time(),
            )
            state["last_snapshot"] = asdict(snapshot)
            state["last_error"] = None
            state["loop_count"] += 1

            open_regime = classify(snapshot.open_spread)
            close_regime = classify(snapshot.close_spread)

            if state["open_regime"] is None:
                state["open_regime"] = open_regime
            elif state["open_regime"] == "le" and open_regime == "gt":
                trigger_phone_alert("open_cross_up", snapshot)
                state["open_regime"] = open_regime
            else:
                state["open_regime"] = open_regime

            if state["close_regime"] is None:
                state["close_regime"] = close_regime
            elif state["close_regime"] == "gt" and close_regime == "le":
                trigger_phone_alert("close_cross_down", snapshot)
                state["close_regime"] = close_regime
            else:
                state["close_regime"] = close_regime

        except Exception as exc:
            state["last_error"] = str(exc)
            logger.exception("Monitor loop error")

        time.sleep(POLL_INTERVAL_SECONDS)


@app.on_event("startup")
def startup_event() -> None:
    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "price-alerts",
        "symbol": SYMBOL,
        "threshold": THRESHOLD,
        "suppression_active": in_suppression_window(),
        **state,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": state["last_error"] is None,
        "running": state["running"],
        "loop_count": state["loop_count"],
        "suppression_active": in_suppression_window(),
        "last_error": state["last_error"],
        "last_snapshot": state["last_snapshot"],
        "last_alert": state["last_alert"],
    }
