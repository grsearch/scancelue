from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


import httpx
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
    startup_entry_price: float | None = None
    startup_last_buy_bucket: int | None = None
    startup_last_cross_bucket: int | None = None


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


class GeckoClient:
    def __init__(self) -> None:
        self.base = os.getenv("COINGECKO_BASE_URL", "https://pro-api.coingecko.com/api/v3/onchain")
        self.api_key = os.getenv("COINGECKO_API_KEY", "")

    async def fetch_ohlcv(self, network: str, token: str, timeframe: str = "minute") -> list[list[float]]:
        # We request up to 200 bars for EMA20/RSI9 stability.
        url = f"{self.base}/networks/{network}/tokens/{token}/ohlcv/{timeframe}"
        params = {"limit": 200}
        headers = {"x-cg-pro-api-key": self.api_key} if self.api_key else {}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        attrs = payload.get("data", {}).get("attributes", {})
        ohlcv = attrs.get("ohlcv_list") or []
        return sorted(ohlcv, key=lambda x: x[0])

    async def fetch_token_meta(self, network: str, token: str) -> dict[str, Any]:
        url = f"{self.base}/networks/{network}/tokens/{token}"
        headers = {"x-cg-pro-api-key": self.api_key} if self.api_key else {}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
        attributes = payload.get("data", {}).get("attributes", {})
        return {
            "fdv": float(attributes.get("fdv_usd") or 0),
            "price": float(attributes.get("price_usd") or 0),
            "pool_created_at": attributes.get("pool_created_at"),
            "created_at": attributes.get("created_at"),
        }


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
    return prev <= level < curr


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
    ) -> tuple[StrategyName, Signal, str]:
        close_1m = closes_1m[-1]
        has_rebound = token.rebound_entry_price is not None
        has_startup = token.startup_entry_price is not None

        # manage existing positions first (sell/add)
        if has_rebound:
            entry = token.rebound_entry_price or close_1m
            if (
                rsi1_now >= 85
                or cross_down(rsi1_prev, rsi1_now, 75)
                or cross_down(rsi1_prev, rsi1_now, 65)
                or (close_5m is not None and ema20_5m is not None and close_5m < ema20_5m)
            ):
                return StrategyName.REBOUND, Signal.SELL, "反弹策略卖出：RSI回落/过热或5分钟close<EMA20"
            if allow_open_rebound and (not token.position.added_once) and cross_up(rsi1_prev, rsi1_now, 30) and close_1m <= entry * 0.90:
                return StrategyName.REBOUND, Signal.ADD, "反弹策略加仓：RSI再次上穿30且价格<=首仓90%"

        if has_startup and None not in (ema9_5m, ema20_5m, ema9_5m_prev, ema20_5m_prev, close_5m):
            ema_cross_down = ema9_5m_prev >= ema20_5m_prev and ema9_5m < ema20_5m
            if ema_cross_down or (close_5m < ema9_5m):
                return StrategyName.STARTUP, Signal.SELL, "启动策略卖出：EMA9下穿EMA20或close<EMA9"

        # new entries
        if allow_open_rebound and (not has_rebound) and cross_up(rsi1_prev, rsi1_now, 30):
            return StrategyName.REBOUND, Signal.BUY, "反弹策略买入：RSI上穿30"

        startup_ready = None not in (close_5m, ema9_5m, ema20_5m, ema9_5m_prev, ema20_5m_prev)
        if startup_ready and startup_bucket is not None:
            ema_cross_up = ema9_5m_prev <= ema20_5m_prev and ema9_5m > ema20_5m
            if ema_cross_up:
                token.startup_last_cross_bucket = startup_bucket

        if startup_ready and (not has_startup) and startup_bucket is not None:
            recent_cross_ok = (
                token.startup_last_cross_bucket is not None
                and 0 <= (startup_bucket - token.startup_last_cross_bucket) <= 2
            )
            startup_state_ok = ema9_5m > ema20_5m and close_5m > ema9_5m and close_5m <= ema9_5m * 1.05
            if recent_cross_ok and startup_state_ok and token.startup_last_buy_bucket != startup_bucket:
                return StrategyName.STARTUP, Signal.BUY, "启动策略买入：上穿后3根5m内且状态保持"

        return StrategyName.REBOUND, Signal.HOLD, "无买卖信号"


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
        self.gecko = GeckoClient()
        self.dispatcher = SignalDispatcher()
        self.lock = asyncio.Lock()
        self.state_path = Path(os.getenv("STATE_FILE", "data/state.json"))

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
            "startup_entry_price": token.startup_entry_price,
            "startup_last_buy_bucket": token.startup_last_buy_bucket,
            "startup_last_cross_bucket": token.startup_last_cross_bucket,
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
            startup_entry_price=payload.get("startup_entry_price"),
            startup_last_buy_bucket=payload.get("startup_last_buy_bucket"),
            startup_last_cross_bucket=payload.get("startup_last_cross_bucket"),
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
            return token

    async def tick(self) -> None:
        async with self.lock:
            tokens = list(self.tokens.values())

        for token in tokens:
            try:
                meta = await self.gecko.fetch_token_meta(token.network, token.address)
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
                    continue

                ohlcv = await self.gecko.fetch_ohlcv(token.network, token.address)
                closes = [float(c[4]) for c in ohlcv]
                if len(closes) < 30:
                    continue
                ema9_vals = ema(closes, 9)
                ema20_vals = ema(closes, 20)
                rsi_vals = rsi(closes, 9)
                if len(rsi_vals) < 2:
                    continue
                ema9_now = ema9_vals[-1]
                ema20_now = ema20_vals[-1]
                close = closes[-1]
                rsi_prev, rsi_now = rsi_vals[-2], rsi_vals[-1]

                # Use 1m OHLCV resampled to closed 5m bars
                bars_5m = resample_5m_closes(ohlcv)
                closes_5m = [x[1] for x in bars_5m]
                startup_bucket = bars_5m[-1][0] if bars_5m else None
                ema9_5m = ema20_5m = ema9_5m_prev = ema20_5m_prev = None
                rsi5_prev = rsi5_now = None
                close_5m = closes_5m[-1] if closes_5m else None
                allow_open_rebound = False
                if len(closes_5m) >= 21:
                    ema9_5m_vals = ema(closes_5m, 9)
                    ema20_5m_vals = ema(closes_5m, 20)
                    ema9_5m = ema9_5m_vals[-1]
                    ema20_5m = ema20_5m_vals[-1]
                    ema9_5m_prev = ema9_5m_vals[-2]
                    ema20_5m_prev = ema20_5m_vals[-2]
                    allow_open_rebound = ema9_5m > ema20_5m
                    rsi5_vals = rsi(closes_5m, 9)
                    if len(rsi5_vals) >= 2:
                        rsi5_prev, rsi5_now = rsi5_vals[-2], rsi5_vals[-1]

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
                )

                if signal == Signal.BUY:
                    if strategy == StrategyName.REBOUND and token.rebound_entry_price is None:
                        token.rebound_entry_price = close
                    elif strategy == StrategyName.STARTUP and token.startup_entry_price is None:
                        token.startup_entry_price = close_5m or close
                        token.startup_last_buy_bucket = startup_bucket
                    token.position.has_position = token.rebound_entry_price is not None or token.startup_entry_price is not None
                elif signal == Signal.ADD:
                    token.position.has_position = True
                    token.position.added_once = True
                elif signal == Signal.SELL:
                    if strategy == StrategyName.REBOUND:
                        token.rebound_entry_price = None
                        token.position.added_once = False
                    elif strategy == StrategyName.STARTUP:
                        token.startup_entry_price = None
                    token.position.has_position = token.rebound_entry_price is not None or token.startup_entry_price is not None

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

    async def _handle_whitelist_exit(self, token: TokenRecord) -> None:
        too_low_fdv = token.fdv is not None and token.fdv < 20000
        too_old = False
        if token.pool_created_at:
            too_old = (datetime.now(timezone.utc) - token.pool_created_at).total_seconds() > 48 * 3600
        if not (too_low_fdv or too_old):
            return
        if token.rebound_entry_price is not None or token.startup_entry_price is not None:
            await self.dispatcher.send(token, Signal.SELL, "移出白名单前先平仓")
            token.position = PositionState()
            token.rebound_entry_price = None
            token.startup_entry_price = None
        self.tokens.pop(token.address, None)
        self.blacklist.add(token.address)
        self.logs.append(
            SignalLog(
                ts=datetime.now(timezone.utc),
                symbol=token.symbol,
                address=token.address,
                strategy=StrategyName.SELL_ONLY,
                signal=Signal.SELL if too_low_fdv or too_old else Signal.HOLD,
                reason="白名单移除并加入黑名单",
            )
        )
        self.save_state()


service = MonitorService()
app = FastAPI(title="SOL New Token Monitor")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup() -> None:
    service.load_state()

    async def loop() -> None:
        while True:
            await service.tick()
            await asyncio.sleep(60)

    asyncio.create_task(loop())


@app.post("/webhook/add-token")
async def add_token(req: AddTokenRequest) -> dict[str, Any]:
    token = await service.add_token(req)
    return {
        "ok": True,
        "token": {"network": token.network, "address": token.address, "symbol": token.symbol},
        "message": "已加入白名单并开始1分钟监控",
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    now = datetime.now(timezone.utc)
    tokens = []
    for t in service.tokens.values():
        tokens.append(
            {
                "symbol": t.symbol,
                "age": format_pool_age(t.pool_created_at, now, t.added_at),
                "fdv": f"{(t.fdv or 0):,.0f}",
                "address": t.address,
                "gmgn": f"https://gmgn.ai/sol/token/{t.address}",
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


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
