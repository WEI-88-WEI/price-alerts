from __future__ import annotations

import json
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
ALERTS_LOG_PATH = Path(__file__).with_name("alerts_log.jsonl")


def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_dotenv()
FWALERT_URL = os.getenv("FWALERT_URL", "")
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
THRESHOLD = float(os.getenv("THRESHOLD", "3"))
OPEN_ALERT_HIGH_THRESHOLD = float(os.getenv("OPEN_ALERT_HIGH_THRESHOLD", "3.1"))
OPEN_ALERT_LOW_RESET = float(os.getenv("OPEN_ALERT_LOW_RESET", "2.9"))
CLOSE_ALERT_LOW_THRESHOLD = float(os.getenv("CLOSE_ALERT_LOW_THRESHOLD", "2.9"))
CLOSE_ALERT_HIGH_RESET = float(os.getenv("CLOSE_ALERT_HIGH_RESET", "3.1"))
SPREAD_ALERT_COOLDOWN_SECONDS = int(os.getenv("SPREAD_ALERT_COOLDOWN_SECONDS", "600"))
SYMBOL = os.getenv("SYMBOL", "CL")
TRADE_LIQUIDATION_PRICE = float(os.getenv("TRADE_LIQUIDATION_PRICE")) if os.getenv("TRADE_LIQUIDATION_PRICE") else None
OSTIUM_LIQUIDATION_PRICE = float(os.getenv("OSTIUM_LIQUIDATION_PRICE")) if os.getenv("OSTIUM_LIQUIDATION_PRICE") else None
LIQUIDATION_ALERT_DISTANCE = float(os.getenv("LIQUIDATION_ALERT_DISTANCE", "5"))
LIQUIDATION_ALERT_COOLDOWN_SECONDS = int(os.getenv("LIQUIDATION_ALERT_COOLDOWN_SECONDS", "1800"))


@dataclass
class Snapshot:
    trade_bid: float | None = None
    trade_ask: float | None = None
    ostium_bid: float | None = None
    ostium_ask: float | None = None
    trade_mid: float | None = None
    ostium_mid: float | None = None
    trade_liq_distance: float | None = None
    ostium_liq_distance: float | None = None
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
    "last_liquidation_alerts": {},
    "last_spread_alerts": {},
}


app = FastAPI(title="price-alerts")


def append_alert_record(record: dict[str, Any]) -> None:
    ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_recent_alerts(limit: int = 50) -> list[dict[str, Any]]:
    if not ALERTS_LOG_PATH.exists():
        return []
    lines = ALERTS_LOG_PATH.read_text(encoding="utf-8").splitlines()
    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


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

    if now.weekday() == 5 and (now.hour > 7 or (now.hour == 7 and now.minute >= 59)):
        return True
    if now.weekday() == 6:
        return True
    if now.weekday() == 0 and now.hour < 6:
        return True

    minutes = now.hour * 60 + now.minute
    return (4 * 60 + 59) <= minutes <= (6 * 60 + 10)


def trigger_phone_alert(event: str, snapshot: Snapshot, extra: dict[str, Any] | None = None) -> None:
    record = {
        "event": event,
        "timestamp": time.time(),
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
        "suppressed": False,
        "snapshot": asdict(snapshot),
        "open_regime_before": state.get("open_regime"),
        "close_regime_before": state.get("close_regime"),
    }
    if extra:
        record.update(extra)

    if in_suppression_window():
        logger.info("Suppressed alert for event=%s during mute window", event)
        record["suppressed"] = True
        state["last_alert"] = record
        append_alert_record(record)
        return

    try:
        response = requests.get(FWALERT_URL, timeout=15)
        response.raise_for_status()
        record["status_code"] = response.status_code
        state["last_alert"] = record
        append_alert_record(record)
        logger.warning("Triggered fwalert for event=%s snapshot=%s", event, asdict(snapshot))
    except Exception as exc:
        record["error"] = str(exc)
        state["last_error"] = f"alert_failed: {exc}"
        state["last_alert"] = record
        append_alert_record(record)
        logger.exception("Failed to trigger fwalert")


def classify(value: float) -> str:
    return "gt" if value > THRESHOLD else "le"


def spread_alert_allowed(event: str) -> bool:
    last_sent = state["last_spread_alerts"].get(event)
    if last_sent is None:
        return True
    return time.time() - last_sent >= SPREAD_ALERT_COOLDOWN_SECONDS


def mark_spread_alert(event: str) -> None:
    state["last_spread_alerts"][event] = time.time()


def maybe_trigger_liquidation_alerts(snapshot: Snapshot) -> None:
    now_ts = time.time()
    venues = [
        ("trade", snapshot.trade_mid, TRADE_LIQUIDATION_PRICE, snapshot.trade_liq_distance),
        ("ostium", snapshot.ostium_mid, OSTIUM_LIQUIDATION_PRICE, snapshot.ostium_liq_distance),
    ]

    for venue, mid_price, liq_price, distance in venues:
        if liq_price is None or mid_price is None or distance is None:
            continue
        if distance > LIQUIDATION_ALERT_DISTANCE:
            continue

        last_sent = state["last_liquidation_alerts"].get(venue)
        if last_sent is not None and now_ts - last_sent < LIQUIDATION_ALERT_COOLDOWN_SECONDS:
            continue

        trigger_phone_alert(
            f"{venue}_liquidation_near",
            snapshot,
            extra={
                "venue": venue,
                "mid_price": mid_price,
                "liquidation_price": liq_price,
                "distance": distance,
                "cooldown_seconds": LIQUIDATION_ALERT_COOLDOWN_SECONDS,
            },
        )
        state["last_liquidation_alerts"][venue] = now_ts


def monitor_loop() -> None:
    state["running"] = True
    state["started_at"] = time.time()
    logger.info("Starting monitor loop for %s", SYMBOL)

    while True:
        try:
            trade_bid, trade_ask = fetch_trade_xyz_cl()
            ostium_bid, ostium_ask = fetch_ostium_cl()
            trade_mid = (trade_bid + trade_ask) / 2
            ostium_mid = (ostium_bid + ostium_ask) / 2
            trade_liq_distance = abs(trade_mid - TRADE_LIQUIDATION_PRICE) if TRADE_LIQUIDATION_PRICE is not None else None
            ostium_liq_distance = abs(ostium_mid - OSTIUM_LIQUIDATION_PRICE) if OSTIUM_LIQUIDATION_PRICE is not None else None

            snapshot = Snapshot(
                trade_bid=trade_bid,
                trade_ask=trade_ask,
                ostium_bid=ostium_bid,
                ostium_ask=ostium_ask,
                trade_mid=trade_mid,
                ostium_mid=ostium_mid,
                trade_liq_distance=trade_liq_distance,
                ostium_liq_distance=ostium_liq_distance,
                open_spread=trade_bid - ostium_ask,
                close_spread=trade_ask - ostium_bid,
                timestamp=time.time(),
            )
            state["last_snapshot"] = asdict(snapshot)
            state["last_error"] = None
            state["loop_count"] += 1

            open_spread = snapshot.open_spread
            close_spread = snapshot.close_spread

            if state["open_regime"] is None:
                state["open_regime"] = "armed" if open_spread < OPEN_ALERT_LOW_RESET else "cooling"
            elif state["open_regime"] == "armed" and open_spread > OPEN_ALERT_HIGH_THRESHOLD:
                if spread_alert_allowed("open_cross_up"):
                    trigger_phone_alert("open_cross_up", snapshot)
                    mark_spread_alert("open_cross_up")
                state["open_regime"] = "cooling"
            elif open_spread < OPEN_ALERT_LOW_RESET:
                state["open_regime"] = "armed"

            if state["close_regime"] is None:
                state["close_regime"] = "armed" if close_spread > CLOSE_ALERT_HIGH_RESET else "cooling"
            elif state["close_regime"] == "armed" and close_spread < CLOSE_ALERT_LOW_THRESHOLD:
                if spread_alert_allowed("close_cross_down"):
                    trigger_phone_alert("close_cross_down", snapshot)
                    mark_spread_alert("close_cross_down")
                state["close_regime"] = "cooling"
            elif close_spread > CLOSE_ALERT_HIGH_RESET:
                state["close_regime"] = "armed"

            maybe_trigger_liquidation_alerts(snapshot)

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
        "open_alert_high_threshold": OPEN_ALERT_HIGH_THRESHOLD,
        "open_alert_low_reset": OPEN_ALERT_LOW_RESET,
        "close_alert_low_threshold": CLOSE_ALERT_LOW_THRESHOLD,
        "close_alert_high_reset": CLOSE_ALERT_HIGH_RESET,
        "spread_alert_cooldown_seconds": SPREAD_ALERT_COOLDOWN_SECONDS,
        "trade_liquidation_price": TRADE_LIQUIDATION_PRICE,
        "ostium_liquidation_price": OSTIUM_LIQUIDATION_PRICE,
        "liquidation_alert_distance": LIQUIDATION_ALERT_DISTANCE,
        "liquidation_alert_cooldown_seconds": LIQUIDATION_ALERT_COOLDOWN_SECONDS,
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
        "last_liquidation_alerts": state["last_liquidation_alerts"],
        "last_spread_alerts": state["last_spread_alerts"],
    }


@app.get("/alerts")
def alerts(limit: int = 50) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 500))
    return {
        "count": safe_limit,
        "items": read_recent_alerts(safe_limit),
    }
