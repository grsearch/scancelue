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
    TREND = "trend"
    RANGE = "range"
    DOWN = "down"


class StrategyName(str, Enum):
    TREND = "trend_strategy"
    RSI = "rsi_strategy"
    SELL_ONLY = "sell_only"


class Signal(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    SELL = "SELL"
    HOLD = "HOLD"


STATE_STRATEGY = {
    MarketState.TREND: StrategyName.TREND,
    MarketState.RANGE: StrategyName.RSI,
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

    async def fetch_ohlcv(self, network: str, token: str) -> list[list[float]]:
        # 1m candles. We request up to 200 bars for EMA20/RSI9 stability.
        url = f"{self.base}/networks/{network}/tokens/{token}/ohlcv/minute"
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


class StrategyEngine:
    @staticmethod
    def raw_state(ema9: float, ema20: float, ema20_prev: float, now_rsi: float) -> MarketState:
        if ema9 > ema20 and ema20 > ema20_prev and now_rsi >= 55:
            return MarketState.TREND
        if ema9 < ema20 and ema20 < ema20_prev and now_rsi <= 40:
            return MarketState.DOWN
        return MarketState.RANGE

    @staticmethod
    def confirm_state(token: TokenRecord, new_state: MarketState) -> MarketState:
        if new_state == token.confirmed_state:
            token.candidate_state = None
            token.candidate_count = 0
            return token.confirmed_state
        if token.candidate_state != new_state:
            token.candidate_state = new_state
            token.candidate_count = 1
            return token.confirmed_state
        token.candidate_count += 1
        if token.candidate_count >= 2:
            token.confirmed_state = new_state
            token.candidate_state = None
            token.candidate_count = 0
        return token.confirmed_state

    @staticmethod
    def evaluate(token: TokenRecord, closes: list[float], ema9: float, ema20: float, rsi_prev: float, rsi_now: float) -> tuple[StrategyName, Signal, str]:
        strategy = STATE_STRATEGY[token.confirmed_state]
        close = closes[-1]

        if strategy == StrategyName.TREND:
            if close < ema9 or rsi_now >= 85:
                return strategy, Signal.SELL, "trend状态触发止盈/止损"
            if (not token.position.has_position and ema9 > ema20 and rsi_now > 55 and near(close, ema9, 0.006) and close >= ema9 * 0.995):
                return strategy, Signal.BUY, "trend状态下价格回踩EMA9，满足趋势买入"
            return strategy, Signal.HOLD, "trend状态无新信号"

        if strategy == StrategyName.RSI:
            if rsi_now >= 85 or cross_down(rsi_prev, rsi_now, 70) or cross_down(rsi_prev, rsi_now, 60):
                return strategy, Signal.SELL, "range状态RSI卖出条件触发"
            if token.position.has_position and (not token.position.added_once) and cross_up(rsi_prev, rsi_now, 25):
                return strategy, Signal.ADD, "range状态RSI二次上穿25，补仓一次"
            if (not token.position.has_position) and cross_up(rsi_prev, rsi_now, 25):
                return strategy, Signal.BUY, "range状态RSI上穿25，反弹买入"
            return strategy, Signal.HOLD, "range状态无新信号"

        if token.position.has_position and (
            cross_down(rsi_prev, rsi_now, 60) or cross_down(rsi_prev, rsi_now, 50) or close < ema20
        ):
            return strategy, Signal.SELL, "down状态仅允许卖出"
        return strategy, Signal.HOLD, "down状态禁止新开仓"


def format_pool_age(pool_created_at: datetime | None, now: datetime | None = None) -> str:
    """Dashboard AGE uses pool creation age from CoinGecko pool_created_at."""
    if pool_created_at is None:
        return "N/A"
    now = now or datetime.now(timezone.utc)
    age_h = (now - pool_created_at).total_seconds() / 3600
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
                    strategy=StrategyName(item.get("strategy", StrategyName.RSI.value)),
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
                ema20_prev = ema20_vals[-2]
                rsi_prev, rsi_now = rsi_vals[-2], rsi_vals[-1]

                raw = self.engine.raw_state(ema9_now, ema20_now, ema20_prev, rsi_now)
                self.engine.confirm_state(token, raw)
                strategy, signal, reason = self.engine.evaluate(token, closes, ema9_now, ema20_now, rsi_prev, rsi_now)

                if signal == Signal.BUY:
                    token.position.has_position = True
                elif signal == Signal.ADD:
                    token.position.has_position = True
                    token.position.added_once = True
                elif signal == Signal.SELL:
                    token.position.has_position = False
                    token.position.added_once = False

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
                        strategy=STATE_STRATEGY[token.confirmed_state],
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
        if token.position.has_position:
            await self.dispatcher.send(token, Signal.SELL, "移出白名单前先平仓")
            token.position = PositionState()
        self.tokens.pop(token.address, None)
        self.blacklist.add(token.address)
        self.logs.append(
            SignalLog(
                ts=datetime.now(timezone.utc),
                symbol=token.symbol,
                address=token.address,
                strategy=STATE_STRATEGY[token.confirmed_state],
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
                "age": format_pool_age(t.pool_created_at, now),
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
