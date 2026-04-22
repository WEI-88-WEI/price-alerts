from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from fastapi import FastAPI
from fastapi.responses import FileResponse

TRADE_XYZ_API_URL = "https://api.hyperliquid.xyz/info"
OSTIUM_METADATA_BASE = "https://metadata-backend.ostium.io"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("price-alerts")
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
ALERTS_LOG_PATH = Path(__file__).with_name("alerts_log.jsonl")
SPREAD_REARM_DELTA = 0.2


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
SPREAD_FWALERT_URL = os.getenv("SPREAD_FWALERT_URL", os.getenv("FWALERT_URL", ""))
LIQUIDATION_FWALERT_URL = os.getenv("LIQUIDATION_FWALERT_URL", os.getenv("FWALERT_URL", ""))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
SPREAD_CHANGE_WINDOW_SECONDS = int(os.getenv("SPREAD_CHANGE_WINDOW_SECONDS", "60"))
SPREAD_CHANGE_THRESHOLD = float(os.getenv("SPREAD_CHANGE_THRESHOLD", "0.6"))
SPREAD_BREAKOUT_CONFIRM_SAMPLES = int(os.getenv("SPREAD_BREAKOUT_CONFIRM_SAMPLES", "3"))
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
    ostium_is_market_open: bool | None = None
    ostium_is_day_trading_closed: bool | None = None
    ostium_seconds_to_toggle_day_trading_closed: int | None = None
    timestamp: float | None = None


state: dict[str, Any] = {
    "running": False,
    "last_snapshot": None,
    "last_error": None,
    "last_alert": None,
    "started_at": None,
    "loop_count": 0,
    "last_liquidation_alerts": {},
    "spread_history_size": 0,
    "ostium_is_market_open": None,
    "spread_warmup_until": None,
    "spread_direction_state": "neutral",
    "spread_direction_confirm_counts": {
        "expand": 0,
        "contract": 0,
    },
}

spread_history: deque[dict[str, float]] = deque(maxlen=600)
app = FastAPI(title="price-alerts")


def append_alert_record(record: dict[str, Any]) -> None:
    ALERTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False) + "\n"
    fd = None
    tmp_path = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{ALERTS_LOG_PATH.name}.",
            suffix=".tmp",
            dir=str(ALERTS_LOG_PATH.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            fd = None
            if ALERTS_LOG_PATH.exists():
                with ALERTS_LOG_PATH.open("r", encoding="utf-8", errors="ignore") as src:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        tmp_file.write(chunk)
            tmp_file.write(line)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, ALERTS_LOG_PATH)
    except Exception as exc:
        state["last_error"] = f"append_alert_record_failed: {exc}"
        logger.exception("Failed to append alert record safely")
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass


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


def fetch_ostium_cl() -> tuple[float, float, bool, bool, int]:
    response = requests.get(f"{OSTIUM_METADATA_BASE}/PricePublish/latest-prices", timeout=30)
    response.raise_for_status()
    prices = response.json()

    for item in prices:
        if item.get("from") == SYMBOL and item.get("to") == "USD":
            bid = float(item["bid"])
            ask = float(item["ask"])
            is_market_open = bool(item.get("isMarketOpen"))
            is_day_trading_closed = bool(item.get("isDayTradingClosed"))
            seconds_to_toggle = int(item.get("secondsToToggleIsDayTradingClosed", -1))
            return bid, ask, is_market_open, is_day_trading_closed, seconds_to_toggle

    raise RuntimeError(f"{SYMBOL}/USD not found on ostium")


def trigger_phone_alert(
    event: str,
    snapshot: Snapshot,
    alert_url: str,
    channel: str,
    extra: dict[str, Any] | None = None,
) -> None:
    record = {
        "event": event,
        "channel": channel,
        "timestamp": time.time(),
        "beijing_time": datetime.now(BEIJING_TZ).isoformat(),
        "suppressed": False,
        "snapshot": asdict(snapshot),
    }
    if extra:
        record.update(extra)

    if snapshot.ostium_is_market_open is False:
        logger.info("Suppressed alert for event=%s because ostium market is closed", event)
        record["suppressed"] = True
        record["suppression_reason"] = "ostium_market_closed"
        state["last_alert"] = record
        return

    if not alert_url:
        record["error"] = f"missing_{channel}_fwalert_url"
        state["last_error"] = f"alert_failed: missing_{channel}_fwalert_url"
        state["last_alert"] = record
        append_alert_record(record)
        logger.error("Missing fwalert url for channel=%s event=%s", channel, event)
        return

    try:
        response = requests.get(alert_url, timeout=15)
        response.raise_for_status()
        record["status_code"] = response.status_code
        state["last_alert"] = record
        append_alert_record(record)
        logger.warning("Triggered fwalert for channel=%s event=%s snapshot=%s", channel, event, asdict(snapshot))
    except Exception as exc:
        record["error"] = str(exc)
        state["last_error"] = f"alert_failed: {exc}"
        state["last_alert"] = record
        append_alert_record(record)
        logger.exception("Failed to trigger fwalert for channel=%s", channel)


def get_window_samples(window_seconds: int) -> list[dict[str, float]]:
    if not spread_history:
        return []
    target_ts = time.time() - window_seconds
    return [item for item in spread_history if item["timestamp"] >= target_ts]


def build_spread_window_payload(
    window_samples: list[dict[str, float]],
    snapshot: Snapshot,
) -> list[dict[str, float | str | None]]:
    samples = list(window_samples)
    current_ts = snapshot.timestamp
    if current_ts is not None:
        if not samples or samples[-1].get("timestamp") != current_ts:
            samples.append(
                {
                    "timestamp": current_ts,
                    "open_spread": snapshot.open_spread,
                    "close_spread": snapshot.close_spread,
                }
            )

    payload: list[dict[str, float | str | None]] = []
    for item in samples:
        ts = item.get("timestamp")
        beijing_time = None
        if isinstance(ts, (int, float)):
            beijing_time = datetime.fromtimestamp(ts, tz=BEIJING_TZ).isoformat()
        payload.append(
            {
                "timestamp": ts,
                "beijing_time": beijing_time,
                "open_spread": item.get("open_spread"),
                "close_spread": item.get("close_spread"),
            }
        )
    return payload


def maybe_trigger_spread_change_alerts(snapshot: Snapshot) -> None:
    if snapshot.ostium_is_market_open is False:
        return

    warmup_until = state.get("spread_warmup_until")
    if isinstance(warmup_until, (int, float)) and time.time() < warmup_until:
        return

    if snapshot.open_spread is None:
        return

    window_samples = get_window_samples(SPREAD_CHANGE_WINDOW_SECONDS)
    if not window_samples:
        return

    oldest_sample = window_samples[0]
    oldest_open_spread = oldest_sample.get("open_spread")
    if oldest_open_spread is None:
        return

    current_open_spread = snapshot.open_spread
    delta_60s = current_open_spread - oldest_open_spread

    if -SPREAD_REARM_DELTA < delta_60s < SPREAD_REARM_DELTA:
        state["spread_direction_state"] = "neutral"
        state["spread_direction_confirm_counts"] = {
            "expand": 0,
            "contract": 0,
        }
        return

    if delta_60s >= SPREAD_CHANGE_THRESHOLD:
        direction = "expand"
        event = "open_spread_expand_60s"
        direction_text = "价差放大"
    elif delta_60s <= -SPREAD_CHANGE_THRESHOLD:
        direction = "contract"
        event = "open_spread_contract_60s"
        direction_text = "价差缩小"
    else:
        state["spread_direction_confirm_counts"] = {
            "expand": 0,
            "contract": 0,
        }
        return

    opposite = "contract" if direction == "expand" else "expand"
    state["spread_direction_confirm_counts"][opposite] = 0
    state["spread_direction_confirm_counts"][direction] += 1
    confirm_count = state["spread_direction_confirm_counts"][direction]

    if state.get("spread_direction_state") == direction:
        return

    if confirm_count < SPREAD_BREAKOUT_CONFIRM_SAMPLES:
        return

    trigger_phone_alert(
        event,
        snapshot,
        alert_url=SPREAD_FWALERT_URL,
        channel="spread",
        extra={
            "spread_name": "open_spread",
            "window_seconds": SPREAD_CHANGE_WINDOW_SECONDS,
            "threshold": SPREAD_CHANGE_THRESHOLD,
            "confirm_samples": SPREAD_BREAKOUT_CONFIRM_SAMPLES,
            "confirm_count": confirm_count,
            "rearm_delta": SPREAD_REARM_DELTA,
            "oldest_timestamp": oldest_sample.get("timestamp"),
            "oldest_beijing_time": datetime.fromtimestamp(oldest_sample["timestamp"], tz=BEIJING_TZ).isoformat() if isinstance(oldest_sample.get("timestamp"), (int, float)) else None,
            "oldest_open_spread": oldest_open_spread,
            "current_open_spread": current_open_spread,
            "delta_60s": delta_60s,
            "direction": direction_text,
            "window_samples": build_spread_window_payload(window_samples, snapshot),
        },
    )
    state["spread_direction_state"] = direction


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
            alert_url=LIQUIDATION_FWALERT_URL,
            channel="liquidation",
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
            ostium_bid, ostium_ask, ostium_is_market_open, ostium_is_day_trading_closed, ostium_seconds_to_toggle = fetch_ostium_cl()
            trade_mid = (trade_bid + trade_ask) / 2
            ostium_mid = (ostium_bid + ostium_ask) / 2
            trade_liq_distance = abs(trade_mid - TRADE_LIQUIDATION_PRICE) if TRADE_LIQUIDATION_PRICE is not None else None
            ostium_liq_distance = abs(ostium_mid - OSTIUM_LIQUIDATION_PRICE) if OSTIUM_LIQUIDATION_PRICE is not None else None

            spread_enabled = ostium_is_market_open is not False
            snapshot = Snapshot(
                trade_bid=trade_bid,
                trade_ask=trade_ask,
                ostium_bid=ostium_bid,
                ostium_ask=ostium_ask,
                trade_mid=trade_mid,
                ostium_mid=ostium_mid,
                trade_liq_distance=trade_liq_distance,
                ostium_liq_distance=ostium_liq_distance,
                open_spread=(trade_bid - ostium_ask) if spread_enabled else None,
                close_spread=(trade_ask - ostium_bid) if spread_enabled else None,
                ostium_is_market_open=ostium_is_market_open,
                ostium_is_day_trading_closed=ostium_is_day_trading_closed,
                ostium_seconds_to_toggle_day_trading_closed=ostium_seconds_to_toggle,
                timestamp=time.time(),
            )
            previous_market_open = state.get("ostium_is_market_open")
            state["last_snapshot"] = asdict(snapshot)
            state["ostium_is_market_open"] = ostium_is_market_open
            if ostium_is_market_open is False:
                state["spread_warmup_until"] = None
            elif previous_market_open is False:
                state["spread_warmup_until"] = time.time() + SPREAD_CHANGE_WINDOW_SECONDS
                state["spread_direction_state"] = "neutral"
                state["spread_direction_confirm_counts"] = {
                    "expand": 0,
                    "contract": 0,
                }
                spread_history.clear()
            state["last_error"] = None
            state["loop_count"] += 1

            maybe_trigger_spread_change_alerts(snapshot)
            maybe_trigger_liquidation_alerts(snapshot)

            if spread_enabled:
                spread_history.append(
                    {
                        "timestamp": snapshot.timestamp,
                        "open_spread": snapshot.open_spread,
                        "close_spread": snapshot.close_spread,
                    }
                )
            state["spread_history_size"] = len(spread_history)

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
        "spread_fwalert_configured": bool(SPREAD_FWALERT_URL),
        "liquidation_fwalert_configured": bool(LIQUIDATION_FWALERT_URL),
        "spread_change_window_seconds": SPREAD_CHANGE_WINDOW_SECONDS,
        "spread_change_threshold": SPREAD_CHANGE_THRESHOLD,
        "spread_breakout_confirm_samples": SPREAD_BREAKOUT_CONFIRM_SAMPLES,
        "spread_rearm_delta": SPREAD_REARM_DELTA,
        "spread_direction_state": state["spread_direction_state"],
        "spread_direction_confirm_counts": state["spread_direction_confirm_counts"],
        "trade_liquidation_price": TRADE_LIQUIDATION_PRICE,
        "ostium_liquidation_price": OSTIUM_LIQUIDATION_PRICE,
        "liquidation_alert_distance": LIQUIDATION_ALERT_DISTANCE,
        "liquidation_alert_cooldown_seconds": LIQUIDATION_ALERT_COOLDOWN_SECONDS,
        "suppression_active": state["ostium_is_market_open"] is False,
        "suppression_reason": "ostium_market_closed" if state["ostium_is_market_open"] is False else None,
        **state,
    }


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": state["last_error"] is None,
        "running": state["running"],
        "loop_count": state["loop_count"],
        "spread_direction_state": state["spread_direction_state"],
        "spread_direction_confirm_counts": state["spread_direction_confirm_counts"],
        "suppression_active": state["ostium_is_market_open"] is False,
        "suppression_reason": "ostium_market_closed" if state["ostium_is_market_open"] is False else None,
        "last_error": state["last_error"],
        "last_snapshot": state["last_snapshot"],
        "last_alert": state["last_alert"],
        "last_liquidation_alerts": state["last_liquidation_alerts"],
        "spread_history_size": state["spread_history_size"],
    }


@app.get("/alerts")
def alerts(limit: int = 50) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 500))
    return {
        "count": safe_limit,
        "items": read_recent_alerts(safe_limit),
    }


@app.get("/chart")
def chart() -> FileResponse:
    return FileResponse(Path(__file__).with_name("chart.html"))
