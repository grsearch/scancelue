from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request


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
    created_at: datetime | None = None
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
            "created_at": attributes.get("pool_created_at") or attributes.get("created_at"),
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


class MonitorService:
    def __init__(self) -> None:
        self.tokens: dict[str, TokenRecord] = {}
        self.blacklist: set[str] = set()
        self.logs: list[SignalLog] = []
        self.engine = StrategyEngine()
        self.gecko = GeckoClient()
        self.dispatcher = SignalDispatcher()
        self.lock = asyncio.Lock()

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
            return token

    async def tick(self) -> None:
        async with self.lock:
            tokens = list(self.tokens.values())

        for token in tokens:
            try:
                meta = await self.gecko.fetch_token_meta(token.network, token.address)
                token.fdv = meta["fdv"]
                token.price = meta["price"]
                created = meta.get("created_at")
                if created and isinstance(created, str):
                    token.created_at = datetime.fromisoformat(created.replace("Z", "+00:00"))

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

    async def _handle_whitelist_exit(self, token: TokenRecord) -> None:
        too_low_fdv = token.fdv is not None and token.fdv < 20000
        too_old = False
        if token.created_at:
            too_old = (datetime.now(timezone.utc) - token.created_at).total_seconds() > 48 * 3600
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


service = MonitorService()
app = FastAPI(title="SOL New Token Monitor")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
async def startup() -> None:
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
        age_h = ((now - (t.created_at or t.added_at)).total_seconds()) / 3600
        tokens.append(
            {
                "symbol": t.symbol,
                "age": f"{age_h:.2f}h",
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
