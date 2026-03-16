from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl


import httpx

try:
    import websockets
except Exception:  # pragma: no cover - optional runtime dependency
    websockets = None

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request


def parse_cg_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts = ts / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.isdigit():
            ts = float(raw)
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return None


class MarketState(str, Enum):
    PRE_TREND = "pre_trend"
    TREND = "trend"
    RANGE = "range"
    DOWN = "down"


class StrategyName(str, Enum):
    REBOUND = "rebound_strategy"
    STARTUP = "startup_strategy"
    SELL_ONLY = "sell_only"


class Signal(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    SELL = "SELL"
    HOLD = "HOLD"


STATE_STRATEGY = {
    MarketState.PRE_TREND: StrategyName.STARTUP,
    MarketState.TREND: StrategyName.STARTUP,
    MarketState.RANGE: StrategyName.REBOUND,
    MarketState.DOWN: StrategyName.SELL_ONLY,
}


@dataclass
class PositionState:
    has_position: bool = False
    added_once: bool = False


@dataclass
class TokenRecord:
    network: str
    address: str
    symbol: str
    added_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pool_created_at: datetime | None = None
    fdv: float | None = None
    price: float | None = None
    confirmed_state: MarketState = MarketState.RANGE
    candidate_state: MarketState | None = None
    candidate_count: int = 0
    position: PositionState = field(default_factory=PositionState)
    last_rsi: float | None = None
    rebound_entry_price: float | None = None
    rebound_add_entry_price: float | None = None
    realized_pnl_sol: float = 0.0
    startup_entry_price: float | None = None
    startup_last_buy_bucket: int | None = None
    startup_last_cross_bucket: int | None = None
    startup_last_entry_cross_bucket: int | None = None
    no_open_mode: bool = False
    no_open_reason: str = ""
    no_open_checked_at: datetime | None = None


@dataclass
class SignalLog:
    ts: datetime
    symbol: str
    address: str
    strategy: StrategyName
    signal: Signal
    reason: str


class AddTokenRequest(BaseModel):
    network: str
    address: str
    symbol: str


class BirdeyeClient:
    def __init__(self) -> None:
        self.base = os.getenv("BIRDEYE_BASE_URL", "https://public-api.birdeye.so")
        self.api_key = os.getenv("BIRDEYE_API_KEY", "")

    @staticmethod
    def _network_to_chain(network: str) -> str:
        return "solana" if network.lower() == "solana" else network.lower()

    def _headers(self, network: str) -> dict[str, str]:
        headers = {"x-chain": self._network_to_chain(network)}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    @staticmethod
    def _to_float(v: Any) -> float | None:
        try:
            if v is None:
                return None
            return float(v)
        except (TypeError, ValueError):
            return None

    async def fetch_ohlcv(
        self,
        network: str,
        token: str,
        timeframe: str = "minute",
        limit: int = 200,
        before_timestamp: int | None = None,
    ) -> list[list[float]]:
        # Birdeye OHLCV endpoint
        tf_map = {"minute": "1m", "hour": "1h", "day": "1d"}
        ohlcv_type = tf_map.get(timeframe, timeframe)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        time_to = before_timestamp if before_timestamp is not None else now_ts
        time_from = max(0, time_to - max(1, int(limit)) * 60)
        url = f"{self.base}/defi/ohlcv"
        params: dict[str, Any] = {
            "address": token,
            "type": ohlcv_type,
            "time_from": time_from,
            "time_to": time_to,
        }
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=self._headers(network))
            resp.raise_for_status()
            payload = resp.json()
        data = payload.get("data") or {}
        items = data.get("items") or data.get("candles") or data.get("list") or []
        out: list[list[float]] = []
        for item in items:
            if isinstance(item, dict):
                ts = int(float(item.get("unixTime") or item.get("time") or item.get("t") or 0))
                o = self._to_float(item.get("o") or item.get("open"))
                h = self._to_float(item.get("h") or item.get("high"))
                l = self._to_float(item.get("l") or item.get("low"))
                c = self._to_float(item.get("c") or item.get("close") or item.get("value"))
                v = self._to_float(item.get("v") or item.get("volume") or 0) or 0.0
                if ts > 0 and o is not None and h is not None and l is not None and c is not None:
                    out.append([float(ts), o, h, l, c, v])
            elif isinstance(item, list) and len(item) >= 6:
                out.append([float(item[0]), float(item[1]), float(item[2]), float(item[3]), float(item[4]), float(item[5])])
        return sorted(out, key=lambda x: x[0])

    async def fetch_token_meta(self, network: str, token: str) -> dict[str, Any]:
        price_url = f"{self.base}/defi/price"
        overview_url = f"{self.base}/defi/token_overview"
        price = 0.0
        fdv: float | None = None
        created_at: Any = None

        async with httpx.AsyncClient(timeout=20) as client:
            price_resp = await client.get(price_url, params={"address": token}, headers=self._headers(network))
            price_resp.raise_for_status()
            price_payload = price_resp.json()

            overview_resp = await client.get(overview_url, params={"address": token}, headers=self._headers(network))
            overview_resp.raise_for_status()
            overview_payload = overview_resp.json()

        price_data = price_payload.get("data") or {}
        if isinstance(price_data, dict):
            price = self._to_float(price_data.get("value") or price_data.get("price")) or 0.0

        overview_data = overview_payload.get("data") or {}
        if isinstance(overview_data, dict):
            fdv = self._to_float(
                overview_data.get("fdv")
                or overview_data.get("fullyDilutedValuation")
                or overview_data.get("mc")
                or overview_data.get("marketCap")
            )
            created_at = overview_data.get("createdAt") or overview_data.get("created_at") or overview_data.get("pairCreatedAt")

        return {
            "fdv": float(fdv or 0),
            "price": float(price or 0),
            "pool_created_at": created_at,
            "created_at": created_at,
        }


    async def fetch_ohlcv_24h(self, network: str, token: str) -> list[list[float]]:
        """Fetch up to ~24h minute bars using paginated requests compatible with endpoint limits."""
        now_ts = int(datetime.now(timezone.utc).timestamp())
        start_ts = now_ts - 24 * 3600

        all_rows: list[list[float]] = []
        before_ts: int | None = None
        # try conservative limits first to avoid 400 limit errors
        candidate_limits = [1000, 500, 200]

        for _ in range(4):
            page: list[list[float]] = []
            last_exc: Exception | None = None
            for lim in candidate_limits:
                try:
                    page = await self.fetch_ohlcv(
                        network=network,
                        token=token,
                        timeframe="minute",
                        limit=lim,
                        before_timestamp=before_ts,
                    )
                    last_exc = None
                    break
                except Exception as exc:  # degrade limit and retry
                    last_exc = exc
                    continue

            if last_exc is not None:
                raise last_exc
            if not page:
                break

            all_rows.extend(page)
            oldest_ts = int(float(page[0][0]))
            if oldest_ts <= start_ts:
                break
            before_ts = oldest_ts - 1

        # dedupe by ts and keep last 24h
        dedup: dict[int, list[float]] = {}
        for row in all_rows:
            dedup[int(float(row[0]))] = row
        out = [row for ts, row in sorted(dedup.items()) if ts >= start_ts]
        return out


class BirdeyeWebSocketConsumer:
    """Realtime trigger channel: consume Birdeye WS events and enqueue token updates."""

    def __init__(self, service: "MonitorService") -> None:
        self.service = service
        self.ws_url = os.getenv("BIRDEYE_WS_URL", "wss://public-api.birdeye.so/socket/solana")
        self.enabled = os.getenv("BIRDEYE_WS_ENABLED", "1") == "1"
        self._last_error_log_ts: float = 0.0

    def _ws_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        api_key = os.getenv("BIRDEYE_API_KEY", "").strip()
        if api_key:
            headers["x-api-key"] = api_key
        extra_raw = os.getenv("BIRDEYE_WS_HEADERS_JSON", "").strip()
        if extra_raw:
            try:
                extra = json.loads(extra_raw)
                if isinstance(extra, dict):
                    for k, v in extra.items():
                        if isinstance(k, str) and isinstance(v, (str, int, float)):
                            headers[k] = str(v)
            except Exception:
                pass
        return headers
    @staticmethod
    def _key_fingerprint(raw_key: str) -> str:
        key = raw_key.strip()
        if not key:
            return "empty"
        prefix = key[:6]
        suffix = key[-4:] if len(key) >= 4 else key
        return f"{prefix}...{suffix} len={len(key)} trim_changed={raw_key != key}"

    def _build_ws_url(self) -> tuple[str, str]:
        raw_key = os.getenv("BIRDEYE_API_KEY", "")
        api_key = raw_key.strip()
        parts = urlsplit(self.ws_url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if api_key:
            query["x-api-key"] = api_key
        new_query = urlencode(query)
        ws_url = urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
        return ws_url, self._key_fingerprint(raw_key)


    @staticmethod
    def _extract_address(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        keys = ["address", "tokenAddress", "mint", "baseAddress", "baseMint", "symbolAddress"]
        for k in keys:
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v
        data = payload.get("data")
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if isinstance(v, str) and v:
                    return v
        return None

    async def _send_subscriptions(self, ws: Any) -> None:
        addresses = await self.service.get_token_addresses()
        if not addresses:
            return
        # Send a few compatible subscription formats for Birdeye WS variants.
        messages = [
            {"type": "SUBSCRIBE_PRICE", "data": {"addresses": addresses}},
            {"type": "SUBSCRIBE_OHLCV", "data": {"type": "1m", "addresses": addresses}},
            {"op": "subscribe", "channel": "price", "data": {"addresses": addresses}},
        ]
        for msg in messages:
            try:
                await ws.send(json.dumps(msg))
            except Exception:
                continue

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        if websockets is None:
            self.service.logs.append(
                SignalLog(
                    ts=datetime.now(timezone.utc),
                    symbol="system",
                    address="-",
                    strategy=StrategyName.SELL_ONLY,
                    signal=Signal.HOLD,
                    reason="Birdeye WS disabled: websockets dependency missing",
                )
            )
            self.service.logs = self.service.logs[-500:]
            self.service.save_state()
            return

        while True:
            try:
                ws_url, fingerprint = self._build_ws_url()
                headers = self._ws_headers()
                now_ts = datetime.now(timezone.utc).timestamp()
                if now_ts - self._last_error_log_ts >= 30:
                    self.service.logs.append(
                        SignalLog(
                            ts=datetime.now(timezone.utc),
                            symbol="system",
                            address="-",
                            strategy=StrategyName.SELL_ONLY,
                            signal=Signal.HOLD,
                            reason=f"Birdeye WS connect: {self.ws_url} key={fingerprint}",
                        )
                    )
                    self.service.logs = self.service.logs[-500:]
                    self.service.save_state()
                async with websockets.connect(  # type: ignore[attr-defined]
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
                    subprotocols=[os.getenv("BIRDEYE_WS_SUBPROTOCOL", "echo-protocol")],
                    origin=os.getenv("BIRDEYE_WS_ORIGIN", "ws://public-api.birdeye.so"),
                    additional_headers=headers,
                ) as ws:
                    await self._send_subscriptions(ws)
                    while True:
                        raw = await ws.recv()
                        payload = json.loads(raw) if isinstance(raw, str) else {}
                        address = self._extract_address(payload)
                        if address:
                            await self.service.notify_realtime_event(address)
            except Exception as exc:
                now_ts = datetime.now(timezone.utc).timestamp()
                if now_ts - self._last_error_log_ts >= 30:
                    self._last_error_log_ts = now_ts
                    self.service.logs.append(
                        SignalLog(
                            ts=datetime.now(timezone.utc),
                            symbol="system",
                            address="-",
                            strategy=StrategyName.SELL_ONLY,
                            signal=Signal.HOLD,
                            reason=f"Birdeye WS reconnect: {exc}",
                        )
                    )
                    self.service.logs = self.service.logs[-500:]
                    self.service.save_state()
                await asyncio.sleep(2)


class SignalDispatcher:
    def __init__(self) -> None:
        self.buy_url = os.getenv("BUY_WEBHOOK_URL", "http://43.162.102.148:3002/webhook/new-token")
        self.sell_url = os.getenv("SELL_WEBHOOK_URL", "http://43.162.102.148:3002/force-sell")

    async def send(self, token: TokenRecord, signal: Signal, reason: str) -> None:
        if signal == Signal.HOLD:
            return
        payload = {
            "mint": token.address,
            "symbol": token.symbol,
            "signal": signal.value,
            "reason": reason,
            "price": token.price,
        }
        url = self.buy_url if signal in {Signal.BUY, Signal.ADD} else self.sell_url
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(url, json=payload)


def ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append((v * k) + (out[-1] * (1 - k)))
    return out


def rsi(values: list[float], period: int = 9) -> list[float]:
    if len(values) < period + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    output = [50.0] * period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            output.append(100.0)
        else:
            rs = avg_gain / avg_loss
            output.append(100 - (100 / (1 + rs)))
    return output


def near(a: float, b: float, tolerance: float = 0.003) -> bool:
    return abs(a - b) / max(abs(b), 1e-9) <= tolerance


def cross_up(prev: float, curr: float, level: float) -> bool:
    return prev < level <= curr


def cross_down(prev: float, curr: float, level: float) -> bool:
    return prev >= level > curr


def recent_pump_ratio(closes: list[float], lookback: int = 3) -> float:
    if len(closes) <= lookback:
        return 0.0
    base = closes[-(lookback + 1)]
    if base <= 0:
        return 0.0
    return (closes[-1] - base) / base


def max_recent_candle_gain(closes: list[float], bars: int = 2) -> float:
    if len(closes) < bars + 1:
        return 0.0
    gains = []
    for i in range(-bars, 0):
        prev = closes[i - 1]
        curr = closes[i]
        if prev <= 0:
            gains.append(0.0)
        else:
            gains.append((curr - prev) / prev)
    return max(gains) if gains else 0.0


def distance_from_ema(close: float, ema_value: float) -> float:
    if ema_value == 0:
        return 0.0
    return abs(close - ema_value) / abs(ema_value)


def resample_5m_closes(ohlcv: list[list[float]]) -> list[tuple[int, float]]:
    buckets: dict[int, list[float]] = {}
    for row in ohlcv:
        ts = int(float(row[0]))
        close = float(row[4])
        bucket = ts // 300
        buckets.setdefault(bucket, []).append(close)
    out: list[tuple[int, float]] = []
    for b in sorted(buckets):
        # liquidity-tolerant mode: keep 5m bucket even if fewer than 5x1m bars arrived
        if buckets[b]:
            out.append((b, buckets[b][-1]))
    return out


def resample_closes(ohlcv: list[list[float]], minutes: int) -> list[tuple[int, float]]:
    if minutes <= 1:
        return [(int(float(row[0])), float(row[4])) for row in ohlcv]
    bucket_sec = minutes * 60
    buckets: dict[int, list[float]] = {}
    for row in ohlcv:
        ts = int(float(row[0]))
        close = float(row[4])
        bucket = ts // bucket_sec
        buckets.setdefault(bucket, []).append(close)
    out: list[tuple[int, float]] = []
    for b in sorted(buckets):
        if buckets[b]:
            out.append((b * bucket_sec, buckets[b][-1]))
    return out


def resample_ohlcv(ohlcv: list[list[float]], minutes: int) -> list[tuple[int, float, float, float, float, float]]:
    if minutes <= 1:
        out: list[tuple[int, float, float, float, float, float]] = []
        for row in ohlcv:
            if len(row) < 6:
                continue
            ts = int(float(row[0]))
            o = float(row[1])
            h = float(row[2])
            l = float(row[3])
            c = float(row[4])
            v = float(row[5])
            out.append((ts, o, h, l, c, v))
        return out

    bucket_sec = minutes * 60
    buckets: dict[int, list[tuple[int, float, float, float, float, float]]] = {}
    for row in ohlcv:
        if len(row) < 6:
            continue
        ts = int(float(row[0]))
        bucket = ts // bucket_sec
        buckets.setdefault(bucket, []).append(
            (ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5]))
        )

    out: list[tuple[int, float, float, float, float, float]] = []
    for bucket in sorted(buckets.keys()):
        rows = sorted(buckets[bucket], key=lambda x: x[0])
        ts = rows[-1][0]
        o = rows[0][1]
        h = max(r[2] for r in rows)
        l = min(r[3] for r in rows)
        c = rows[-1][4]
        v = sum(r[5] for r in rows)
        out.append((ts, o, h, l, c, v))
    return out


class StrategyEngine:
    @staticmethod
    def evaluate(
        token: TokenRecord,
        closes_1m: list[float],
        ema9_1m: float,
        ema20_1m: float,
        rsi1_prev: float,
        rsi1_now: float,
        allow_open_rebound: bool,
        close_5m: float | None,
        ema9_5m: float | None,
        ema20_5m: float | None,
        ema9_5m_prev: float | None,
        ema20_5m_prev: float | None,
        rsi5_prev: float | None,
        rsi5_now: float | None,
        startup_bucket: int | None,
        close_5m_prev: float | None = None,
        close_5m_prev2: float | None = None,
        token_age_hours: float | None = None,
        ema9_1m_prev: float | None = None,
        ema20_1m_prev: float | None = None,
    ) -> tuple[StrategyName, Signal, str]:
        close_1m = closes_1m[-1]
        has_rebound = token.rebound_entry_price is not None

        if has_rebound:
            if (
                rsi1_now >= 85
                or cross_down(rsi1_prev, rsi1_now, 75)
                or cross_down(rsi1_prev, rsi1_now, 70)
                or cross_down(rsi1_prev, rsi1_now, 65)
                or (token.rebound_entry_price is not None and close_1m <= token.rebound_entry_price * 0.90)
            ):
                return StrategyName.REBOUND, Signal.SELL, "反弹策略卖出：RSI下穿65/70/75、RSI>=85或跌破首仓90%"

        if (not has_rebound) and cross_up(rsi1_prev, rsi1_now, 30):
            return StrategyName.REBOUND, Signal.BUY, "反弹策略买入：RSI上穿30"

        return StrategyName.REBOUND, Signal.HOLD, "无买卖信号"



@dataclass
class BacktestConfig:
    name: str
    mode: str
    candle_minutes: int = 1
    require_open_gate: bool = True
    buy_rsi_threshold: float = 30.0
    enable_add: bool = False
    add_drop_pct: float = 0.90
    stop_loss_pct: float | None = None
    sell_cross_65: bool = True
    sell_cross_70: bool = True
    sell_cross_75: bool = True
    overbought_rsi: float = 85.0


@dataclass
class BacktestResult:
    strategy: str
    total_pnl_sol: float
    realized_pnl_sol: float
    unrealized_pnl_sol: float
    trades: int


BACKTEST_CONFIGS = [
    BacktestConfig(
        name="反弹策略",
        mode="rebound",
        candle_minutes=1,
        require_open_gate=False,
        buy_rsi_threshold=30.0,
        enable_add=False,
        stop_loss_pct=None,
        sell_cross_65=False,
        sell_cross_70=True,
        sell_cross_75=True,
        overbought_rsi=85.0,
    ),
    BacktestConfig(
        name="反弹策略2",
        mode="rebound",
        candle_minutes=1,
        require_open_gate=False,
        buy_rsi_threshold=30.0,
        enable_add=False,
        stop_loss_pct=0.90,
        sell_cross_65=True,
        sell_cross_70=True,
        sell_cross_75=True,
        overbought_rsi=85.0,
    ),
]


def run_rebound_backtest_24h(ohlcv: list[list[float]], config: BacktestConfig, now_ts: int | None = None) -> BacktestResult:
    if len(ohlcv) < 30:
        return BacktestResult(config.name, 0.0, 0.0, 0.0, 0)

    now_ts = now_ts or int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - 24 * 3600

    tf_ohlcv = resample_ohlcv(ohlcv, config.candle_minutes)
    if len(tf_ohlcv) < 30:
        return BacktestResult(config.name, 0.0, 0.0, 0.0, 0)
    tf_rows = [(row[0], row[4]) for row in tf_ohlcv]
    closes = [x[1] for x in tf_rows]
    highs = [row[2] for row in tf_ohlcv]
    lows = [row[3] for row in tf_ohlcv]
    vols = [row[5] for row in tf_ohlcv]
    rsi_vals = rsi(closes, 9)
    ema9_vals = ema(closes, 9)
    ema20_vals = ema(closes, 20)
    vwap_vals: list[float] = []
    cum_pv = 0.0
    cum_v = 0.0
    for idx, c in enumerate(closes):
        v = vols[idx]
        if v > 0:
            cum_pv += c * v
            cum_v += v
        vwap_vals.append((cum_pv / cum_v) if cum_v > 0 else c)
    if len(rsi_vals) < 2:
        return BacktestResult(config.name, 0.0, 0.0, 0.0, 0)

    bars_5m = resample_5m_closes(ohlcv)
    ema5_map: dict[int, tuple[float, float]] = {}
    if len(bars_5m) >= 21:
        closes_5m = [x[1] for x in bars_5m]
        ema9_5m_vals = ema(closes_5m, 9)
        ema20_5m_vals = ema(closes_5m, 20)
        for idx, (bucket, _) in enumerate(bars_5m):
            if idx >= 20:
                ema5_map[bucket] = (ema9_5m_vals[idx], ema20_5m_vals[idx])

    first_entry: float | None = None
    add_entry: float | None = None
    added_once = False
    realized = 0.0
    trades = 0

    for i in range(2, len(tf_rows)):
        ts = tf_rows[i][0]
        if ts < start_ts:
            continue

        close_now = tf_rows[i][1]
        rsi_prev = rsi_vals[i - 2]
        rsi_now = rsi_vals[i - 1]
        ema9_prev = ema9_vals[i - 1]
        ema9_now = ema9_vals[i]
        ema20_prev = ema20_vals[i - 1]
        ema20_now = ema20_vals[i]

        allow_open_rebound = True

        has_pos = first_entry is not None

        if config.mode == "rebound":
            if has_pos:
                sell = (
                    rsi_now >= config.overbought_rsi
                    or (config.sell_cross_75 and cross_down(rsi_prev, rsi_now, 75))
                    or (config.sell_cross_70 and cross_down(rsi_prev, rsi_now, 70))
                    or (config.sell_cross_65 and cross_down(rsi_prev, rsi_now, 65))
                    or (config.stop_loss_pct is not None and first_entry is not None and first_entry > 0 and close_now <= first_entry * config.stop_loss_pct)
                )

                if sell and first_entry is not None and first_entry > 0:
                    legs = [first_entry]
                    if add_entry is not None and add_entry > 0:
                        legs.append(add_entry)
                    realized += sum((close_now / entry) - 1.0 for entry in legs)
                    trades += 1
                    first_entry = None
                    add_entry = None
                    added_once = False
                    continue

                if config.enable_add and (not added_once) and first_entry is not None and cross_up(rsi_prev, rsi_now, config.buy_rsi_threshold) and close_now <= first_entry * config.add_drop_pct:
                    add_entry = close_now
                    added_once = True
                    trades += 1
                    continue

            if (not has_pos) and cross_up(rsi_prev, rsi_now, config.buy_rsi_threshold):
                first_entry = close_now
                add_entry = None
                added_once = False
                trades += 1

        elif config.mode == "trend":
            startup_recent = False
            pullback_recent = False
            start_idx = max(5, i - 3)
            for j in range(start_idx, i):
                cross_event = ema9_vals[j - 1] < ema20_vals[j - 1] and ema9_vals[j] >= ema20_vals[j]
                breakout_event = closes[j] > max(highs[j - 5 : j])
                if not (cross_event or breakout_event):
                    continue
                startup_recent = True
                pullback_end = min(i - 1, j + 3)
                if pullback_end <= j:
                    continue
                for k in range(j + 1, pullback_end + 1):
                    if lows[k] >= (ema20_vals[k] * 0.995) and vols[k] < vols[j]:
                        pullback_recent = True
                        break
                if pullback_recent:
                    break

            trend_buy = (
                ema9_now > ema20_now
                and i >= 3
                and ema20_now > ema20_vals[i - 3]
                and close_now > vwap_vals[i]
                and startup_recent
                and pullback_recent
                and close_now > highs[i - 1]
                and close_now > ema9_now
                and rsi_now > 55
                and rsi_now < 78
            )
            trend_sell = (
                (first_entry is not None and close_now <= first_entry * 0.955)
                or close_now < ema20_now
                or (ema9_prev > ema20_prev and ema9_now <= ema20_now)
                or (rsi_prev > 75 and rsi_now < 70)
            )

            if has_pos and trend_sell and first_entry is not None and first_entry > 0:
                realized += (close_now / first_entry) - 1.0
                trades += 1
                first_entry = None
                add_entry = None
                added_once = False
                continue

            if (not has_pos) and trend_buy:
                first_entry = close_now
                add_entry = None
                added_once = False
                trades += 1

    unrealized = 0.0
    if first_entry is not None and first_entry > 0:
        last_close = tf_rows[-1][1]
        legs = [first_entry]
        if add_entry is not None and add_entry > 0:
            legs.append(add_entry)
        unrealized = sum((last_close / entry) - 1.0 for entry in legs)

    total = realized + unrealized
    return BacktestResult(
        strategy=config.name,
        total_pnl_sol=total,
        realized_pnl_sol=realized,
        unrealized_pnl_sol=unrealized,
        trades=trades,
    )

def format_pool_age(pool_created_at: datetime | None, now: datetime | None = None, added_at: datetime | None = None) -> str:
    """Dashboard AGE prefers pool_created_at; falls back to whitelist added_at before metadata is ready."""
    base = pool_created_at or added_at
    if base is None:
        return "N/A"
    now = now or datetime.now(timezone.utc)
    age_h = (now - base).total_seconds() / 3600
    if age_h < 0:
        return "0.00h"
    return f"{age_h:.2f}h"


class MonitorService:
    def __init__(self) -> None:
        self.tokens: dict[str, TokenRecord] = {}
        self.blacklist: set[str] = set()
        self.logs: list[SignalLog] = []
        self.engine = StrategyEngine()
        self.market_data = BirdeyeClient()
        self.dispatcher = SignalDispatcher()
        self.lock = asyncio.Lock()
        self.state_path = Path(os.getenv("STATE_FILE", "data/state.json"))
        self.realtime_queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_realtime_process_ts: dict[str, float] = {}

    @staticmethod
    def _dt_to_str(value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.astimezone(timezone.utc).isoformat()

    def _token_to_dict(self, token: TokenRecord) -> dict[str, Any]:
        return {
            "network": token.network,
            "address": token.address,
            "symbol": token.symbol,
            "added_at": self._dt_to_str(token.added_at),
            "pool_created_at": self._dt_to_str(token.pool_created_at),
            "fdv": token.fdv,
            "price": token.price,
            "confirmed_state": token.confirmed_state.value,
            "candidate_state": token.candidate_state.value if token.candidate_state else None,
            "candidate_count": token.candidate_count,
            "position": {
                "has_position": token.position.has_position,
                "added_once": token.position.added_once,
            },
            "last_rsi": token.last_rsi,
            "rebound_entry_price": token.rebound_entry_price,
            "rebound_add_entry_price": token.rebound_add_entry_price,
            "realized_pnl_sol": token.realized_pnl_sol,
            "startup_entry_price": token.startup_entry_price,
            "startup_last_buy_bucket": token.startup_last_buy_bucket,
            "startup_last_cross_bucket": token.startup_last_cross_bucket,
            "startup_last_entry_cross_bucket": token.startup_last_entry_cross_bucket,
            "no_open_mode": token.no_open_mode,
            "no_open_reason": token.no_open_reason,
            "no_open_checked_at": self._dt_to_str(token.no_open_checked_at),
        }

    def _log_to_dict(self, log: SignalLog) -> dict[str, Any]:
        return {
            "ts": self._dt_to_str(log.ts),
            "symbol": log.symbol,
            "address": log.address,
            "strategy": log.strategy.value,
            "signal": log.signal.value,
            "reason": log.reason,
        }

    def _token_from_dict(self, payload: dict[str, Any]) -> TokenRecord:
        return TokenRecord(
            network=str(payload.get("network", "solana")),
            address=str(payload["address"]),
            symbol=str(payload.get("symbol", "")),
            added_at=parse_cg_datetime(payload.get("added_at")) or datetime.now(timezone.utc),
            pool_created_at=parse_cg_datetime(payload.get("pool_created_at")),
            fdv=payload.get("fdv"),
            price=payload.get("price"),
            confirmed_state=MarketState(payload.get("confirmed_state", MarketState.RANGE.value)),
            candidate_state=MarketState(payload["candidate_state"]) if payload.get("candidate_state") else None,
            candidate_count=int(payload.get("candidate_count", 0)),
            position=PositionState(
                has_position=bool(payload.get("position", {}).get("has_position", False)),
                added_once=bool(payload.get("position", {}).get("added_once", False)),
            ),
            last_rsi=payload.get("last_rsi"),
            rebound_entry_price=payload.get("rebound_entry_price"),
            rebound_add_entry_price=payload.get("rebound_add_entry_price"),
            realized_pnl_sol=float(payload.get("realized_pnl_sol", 0.0) or 0.0),
            startup_entry_price=payload.get("startup_entry_price"),
            startup_last_buy_bucket=payload.get("startup_last_buy_bucket"),
            startup_last_cross_bucket=payload.get("startup_last_cross_bucket"),
            startup_last_entry_cross_bucket=payload.get("startup_last_entry_cross_bucket"),
            no_open_mode=bool(payload.get("no_open_mode", False)),
            no_open_reason=str(payload.get("no_open_reason", "")),
            no_open_checked_at=parse_cg_datetime(payload.get("no_open_checked_at")),
        )

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "tokens": [self._token_to_dict(t) for t in self.tokens.values()],
            "blacklist": sorted(self.blacklist),
            "logs": [self._log_to_dict(l) for l in self.logs[-500:]],
        }
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    def load_state(self) -> None:
        if not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text())
        tokens: dict[str, TokenRecord] = {}
        for item in payload.get("tokens", []):
            token = self._token_from_dict(item)
            tokens[token.address] = token
        logs: list[SignalLog] = []
        for item in payload.get("logs", []):
            ts = parse_cg_datetime(item.get("ts")) or datetime.now(timezone.utc)
            logs.append(
                SignalLog(
                    ts=ts,
                    symbol=str(item.get("symbol", "")),
                    address=str(item.get("address", "")),
                    strategy=StrategyName(item.get("strategy", StrategyName.REBOUND.value)) if item.get("strategy") in {x.value for x in StrategyName} else StrategyName.REBOUND,
                    signal=Signal(item.get("signal", Signal.HOLD.value)),
                    reason=str(item.get("reason", "")),
                )
            )
        self.tokens = tokens
        self.blacklist = set(str(x) for x in payload.get("blacklist", []))
        self.logs = logs[-500:]

    async def add_token(self, req: AddTokenRequest) -> TokenRecord:
        if req.network.lower() != "solana":
            raise HTTPException(400, "仅支持 solana")
        async with self.lock:
            if req.address in self.blacklist:
                raise HTTPException(400, "该代币在黑名单中")
            token = self.tokens.get(req.address)
            if token:
                return token
            token = TokenRecord(network=req.network.lower(), address=req.address, symbol=req.symbol)
            self.tokens[req.address] = token
            self.save_state()
            await self.notify_realtime_event(req.address)
            return token


    async def get_token_addresses(self) -> list[str]:
        async with self.lock:
            return list(self.tokens.keys())

    async def notify_realtime_event(self, address: str) -> None:
        await self.realtime_queue.put(address)

    async def process_realtime_event(self, address: str) -> None:
        now_ts = datetime.now(timezone.utc).timestamp()
        last = self._last_realtime_process_ts.get(address, 0.0)
        if now_ts - last < 5.0:
            return
        self._last_realtime_process_ts[address] = now_ts
        async with self.lock:
            token = self.tokens.get(address)
        if token is None:
            return
        await self._process_token(token)

    async def _process_token(self, token: TokenRecord) -> None:
        try:
            meta = await self.market_data.fetch_token_meta(token.network, token.address)
            token.fdv = meta["fdv"]
            token.price = meta["price"]
            pool_created = meta.get("pool_created_at")
            parsed_pool_created = parse_cg_datetime(pool_created)
            if parsed_pool_created:
                token.pool_created_at = parsed_pool_created
            elif token.pool_created_at is None:
                parsed_created = parse_cg_datetime(meta.get("created_at"))
                if parsed_created:
                    token.pool_created_at = parsed_created

            await self._handle_whitelist_exit(token)
            if token.address in self.blacklist:
                return

            # runtime now monitors on 1m candles
            ohlcv = await self.market_data.fetch_ohlcv(token.network, token.address)
            closes = [float(row[4]) for row in ohlcv if len(row) >= 5]
            if len(closes) < 11:
                return
            ema9_vals = ema(closes, 9)
            ema20_vals = ema(closes, 20)
            rsi_vals = rsi(closes, 9)
            if len(rsi_vals) < 2:
                return
            ema9_now = ema9_vals[-1]
            ema20_now = ema20_vals[-1]
            ema9_prev_1m = ema9_vals[-2] if len(ema9_vals) >= 2 else None
            ema20_prev_1m = ema20_vals[-2] if len(ema20_vals) >= 2 else None
            close = closes[-1]
            rsi_prev, rsi_now = rsi_vals[-2], rsi_vals[-1]
            base_age = token.pool_created_at or token.added_at
            token_age_hours = max(0.0, (datetime.now(timezone.utc) - base_age).total_seconds() / 3600)

            startup_bucket = int(float(ohlcv[-1][0])) // 60 if ohlcv else None
            ema9_5m = ema20_5m = ema9_5m_prev = ema20_5m_prev = None
            rsi5_prev = rsi5_now = None
            close_5m = close
            close_5m_prev = closes[-2] if len(closes) >= 2 else None
            close_5m_prev2 = closes[-3] if len(closes) >= 3 else None
            allow_open_rebound = True

            strategy, signal, reason = self.engine.evaluate(
                token,
                closes,
                ema9_now,
                ema20_now,
                rsi_prev,
                rsi_now,
                allow_open_rebound,
                close_5m,
                ema9_5m,
                ema20_5m,
                ema9_5m_prev,
                ema20_5m_prev,
                rsi5_prev,
                rsi5_now,
                startup_bucket,
                close_5m_prev,
                close_5m_prev2,
                token_age_hours,
                ema9_prev_1m,
                ema20_prev_1m,
            )
            if signal == Signal.BUY:
                if strategy == StrategyName.STARTUP:
                    if token.startup_entry_price is None:
                        token.startup_entry_price = close
                    token.position.has_position = token.startup_entry_price is not None
                else:
                    if token.rebound_entry_price is None:
                        token.rebound_entry_price = close
                    token.position.has_position = token.rebound_entry_price is not None
            elif signal == Signal.ADD:
                token.position.has_position = True
                token.position.added_once = True
                if token.rebound_add_entry_price is None:
                    token.rebound_add_entry_price = close
            elif signal == Signal.SELL:
                if close > 0 and strategy == StrategyName.STARTUP and token.startup_entry_price and token.startup_entry_price > 0:
                    token.realized_pnl_sol += (close / token.startup_entry_price) - 1.0
                elif close > 0 and token.rebound_entry_price and token.rebound_entry_price > 0:
                    legs = [token.rebound_entry_price]
                    if token.rebound_add_entry_price and token.rebound_add_entry_price > 0:
                        legs.append(token.rebound_add_entry_price)
                    token.realized_pnl_sol += sum((close / entry) - 1.0 for entry in legs)
                token.rebound_entry_price = None
                token.rebound_add_entry_price = None
                token.startup_entry_price = None
                token.startup_last_cross_bucket = 0
                token.startup_last_entry_cross_bucket = 0
                token.position.added_once = False
                token.position.has_position = False

            await self.dispatcher.send(token, signal, reason)
            self.logs.append(
                SignalLog(
                    ts=datetime.now(timezone.utc),
                    symbol=token.symbol,
                    address=token.address,
                    strategy=strategy,
                    signal=signal,
                    reason=reason,
                )
            )
            self.logs = self.logs[-500:]
            self.save_state()
        except Exception as exc:
            self.logs.append(
                SignalLog(
                    ts=datetime.now(timezone.utc),
                    symbol=token.symbol,
                    address=token.address,
                    strategy=StrategyName.SELL_ONLY,
                    signal=Signal.HOLD,
                    reason=f"tick error: {exc}",
                )
            )
            self.save_state()

    async def tick(self) -> None:
        async with self.lock:
            tokens = list(self.tokens.values())

        for token in tokens:
            await self._process_token(token)

    async def _handle_whitelist_exit(self, token: TokenRecord) -> None:
        too_low_fdv = token.fdv is not None and token.fdv < 20000
        base_age = token.pool_created_at or token.added_at
        age_hours = max(0.0, (datetime.now(timezone.utc) - base_age).total_seconds() / 3600)
        too_old = age_hours > 6
        if not (too_low_fdv or too_old):
            return
        if token.rebound_entry_price is not None or token.startup_entry_price is not None:
            await self.dispatcher.send(token, Signal.SELL, "移出白名单前先平仓")
            if token.price and token.price > 0 and token.rebound_entry_price and token.rebound_entry_price > 0:
                legs = [token.rebound_entry_price]
                if token.rebound_add_entry_price and token.rebound_add_entry_price > 0:
                    legs.append(token.rebound_add_entry_price)
                token.realized_pnl_sol += sum((token.price / entry) - 1.0 for entry in legs)
            token.position = PositionState()
            token.rebound_entry_price = None
            token.rebound_add_entry_price = None
            token.startup_entry_price = None
        self.tokens.pop(token.address, None)
        self.blacklist.add(token.address)
        self.logs.append(
            SignalLog(
                ts=datetime.now(timezone.utc),
                symbol=token.symbol,
                address=token.address,
                strategy=StrategyName.SELL_ONLY,
                signal=Signal.SELL if (too_low_fdv or too_old) else Signal.HOLD,
                reason="白名单移除并加入黑名单",
            )
        )
        self.save_state()


def seconds_until_next_tick(now_ts: int | None = None, interval_seconds: int = 300, buffer_seconds: int = 5) -> int:
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be > 0")
    if now_ts is None:
        now_ts = int(datetime.now(timezone.utc).timestamp())
    buffer = max(0, int(buffer_seconds))
    next_boundary = ((int(now_ts) // interval_seconds) + 1) * interval_seconds
    target_ts = next_boundary + buffer
    return max(0, target_ts - int(now_ts))


service = MonitorService()
ws_consumer = BirdeyeWebSocketConsumer(service)
app = FastAPI(title="SOL New Token Monitor")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup() -> None:
    service.load_state()

    async def periodic_reconcile_loop() -> None:
        buffer_raw = os.getenv("MONITOR_TICK_BUFFER_SECONDS", "5")
        try:
            buffer_seconds = max(0, int(buffer_raw))
        except ValueError:
            buffer_seconds = 5

        while True:
            await service.tick()
            sleep_seconds = seconds_until_next_tick(interval_seconds=60, buffer_seconds=buffer_seconds)
            await asyncio.sleep(sleep_seconds)

    async def realtime_signal_loop() -> None:
        # WS drives the trading-signal main path; periodic_reconcile_loop handles补数/对账.
        while True:
            address = await service.realtime_queue.get()
            await service.process_realtime_event(address)

    asyncio.create_task(periodic_reconcile_loop())
    asyncio.create_task(realtime_signal_loop())
    asyncio.create_task(ws_consumer.run_forever())


@app.post("/webhook/add-token")
async def add_token(req: AddTokenRequest) -> dict[str, Any]:
    token = await service.add_token(req)
    return {
        "ok": True,
        "token": {"network": token.network, "address": token.address, "symbol": token.symbol},
        "message": "已加入白名单并开始1分钟K线监控",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    tokens = []
    for t in service.tokens.values():
        current_pnl_sol = 0.0
        current_pnl_text = "N/A"
        if t.price and t.price > 0 and ((t.rebound_entry_price and t.rebound_entry_price > 0) or (t.startup_entry_price and t.startup_entry_price > 0)):
            legs = [t.rebound_entry_price]
            if t.rebound_entry_price is None and t.startup_entry_price:
                legs = [t.startup_entry_price]
            if t.rebound_add_entry_price and t.rebound_add_entry_price > 0:
                legs.append(t.rebound_add_entry_price)
            current_pnl_sol = sum((t.price / entry) - 1.0 for entry in legs)
            invested_sol = float(len(legs))
            pnl_pct = (current_pnl_sol / invested_sol) * 100 if invested_sol > 0 else 0.0
            current_pnl_text = f"{current_pnl_sol:+.3f} SOL ({pnl_pct:+.2f}%)"

        total_pnl_sol = t.realized_pnl_sol + current_pnl_sol
        total_pnl_text = f"{total_pnl_sol:+.3f} SOL"

        tokens.append(
            {
                "symbol": t.symbol,
                "age": format_pool_age(t.pool_created_at, now, t.added_at),
                "fdv": f"{(t.fdv or 0):,.0f}",
                "address": t.address,
                "gmgn": f"https://gmgn.ai/sol/token/{t.address}",
                "pnl": current_pnl_text,
                "total_pnl": total_pnl_text,
            }
        )
    logs = [
        {
            "ts": l.ts.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "symbol": l.symbol,
            "strategy": l.strategy.value,
            "signal": l.signal.value,
            "reason": l.reason,
        }
        for l in reversed(service.logs[-200:])
    ]
    return templates.TemplateResponse("dashboard.html", {"request": request, "tokens": tokens, "logs": logs})


@app.get("/dashboard/backtest", response_class=HTMLResponse)
async def dashboard_backtest(request: Request) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    summary_map: dict[str, dict[str, Any]] = {
        cfg.name: {"strategy": cfg.name, "total_pnl_sol": 0.0, "trades": 0} for cfg in BACKTEST_CONFIGS
    }

    for t in service.tokens.values():
        try:
            ohlcv = await service.market_data.fetch_ohlcv_24h(t.network, t.address)
            if len(ohlcv) < 30:
                continue
            results = [run_rebound_backtest_24h(ohlcv, cfg) for cfg in BACKTEST_CONFIGS]
            for r in results:
                summary_map[r.strategy]["total_pnl_sol"] += r.total_pnl_sol
                summary_map[r.strategy]["trades"] += r.trades
            rows.append(
                {
                    "symbol": t.symbol,
                    "age": format_pool_age(t.pool_created_at, now, t.added_at),
                    "address": t.address,
                    "gmgn": f"https://gmgn.ai/sol/token/{t.address}",
                    "results": [
                        {
                            "strategy": r.strategy,
                            "total": f"{r.total_pnl_sol:+.3f} SOL",
                            "realized": f"{r.realized_pnl_sol:+.3f} SOL",
                            "unrealized": f"{r.unrealized_pnl_sol:+.3f} SOL",
                            "trades": r.trades,
                        }
                        for r in results
                    ],
                }
            )
        except Exception as exc:
            rows.append(
                {
                    "symbol": t.symbol,
                    "age": format_pool_age(t.pool_created_at, now, t.added_at),
                    "address": t.address,
                    "gmgn": f"https://gmgn.ai/sol/token/{t.address}",
                    "error": str(exc),
                    "results": [],
                }
            )

    summary = [
        {
            "strategy": x["strategy"],
            "total": f"{x['total_pnl_sol']:+.3f} SOL",
            "trades": x["trades"],
        }
        for x in summary_map.values()
    ]
    return templates.TemplateResponse(
        "backtest_dashboard.html",
        {"request": request, "rows": rows, "summary": summary},
    )


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
