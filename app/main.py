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
    rebound_add_entry_price: float | None = None
    realized_pnl_sol: float = 0.0
    startup_entry_price: float | None = None
    startup_last_buy_bucket: int | None = None
    startup_last_cross_bucket: int | None = None
    startup_last_entry_cross_bucket: int | None = None


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

    async def fetch_ohlcv(
        self,
        network: str,
        token: str,
        timeframe: str = "minute",
        limit: int = 200,
        before_timestamp: int | None = None,
    ) -> list[list[float]]:
        # Default 200 bars for runtime; backtest may request larger window.
        url = f"{self.base}/networks/{network}/tokens/{token}/ohlcv/{timeframe}"
        params: dict[str, Any] = {"limit": limit}
        if before_timestamp is not None:
            params["before_timestamp"] = before_timestamp
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
        close_5m_prev: float | None = None,
        close_5m_prev2: float | None = None,
    ) -> tuple[StrategyName, Signal, str]:
        close_1m = closes_1m[-1]
        has_rebound = token.rebound_entry_price is not None

        if has_rebound:
            entry = token.rebound_entry_price or close_1m
            if (
                rsi1_now >= 85
                or cross_down(rsi1_prev, rsi1_now, 75)
                or cross_down(rsi1_prev, rsi1_now, 70)
                or cross_down(rsi1_prev, rsi1_now, 65)
            ):
                return StrategyName.REBOUND, Signal.SELL, "反弹策略卖出：RSI下穿65/70/75或过热"
            if allow_open_rebound and (not token.position.added_once) and cross_up(rsi1_prev, rsi1_now, 30) and close_1m <= entry * 0.90:
                return StrategyName.REBOUND, Signal.ADD, "反弹策略加仓：满足5m开单条件且RSI再次上穿30"

        if allow_open_rebound and (not has_rebound) and cross_up(rsi1_prev, rsi1_now, 30):
            return StrategyName.REBOUND, Signal.BUY, "反弹策略买入：满足5m开单条件且RSI上穿30"

        if (not allow_open_rebound) and (not has_rebound):
            return StrategyName.REBOUND, Signal.HOLD, "反弹策略HOLD：5分钟EMA9<=EMA20，禁止开单"

        return StrategyName.REBOUND, Signal.HOLD, "无买卖信号"



@dataclass
class BacktestConfig:
    name: str
    require_5m_gate: bool
    use_stop70: bool
    use_ema_cross: bool = False
    take_profit_pct: float | None = None
    overbought_rsi: float = 85.0


@dataclass
class BacktestResult:
    strategy: str
    total_pnl_sol: float
    realized_pnl_sol: float
    unrealized_pnl_sol: float
    trades: int


BACKTEST_CONFIGS = [
    BacktestConfig(name="策略1", require_5m_gate=True, use_stop70=False),
    BacktestConfig(name="策略2", require_5m_gate=True, use_stop70=True),
    BacktestConfig(name="策略3", require_5m_gate=False, use_stop70=False),
    BacktestConfig(name="策略4", require_5m_gate=False, use_stop70=True),
    BacktestConfig(name="策略5", require_5m_gate=False, use_stop70=False, use_ema_cross=True),
    BacktestConfig(
        name="策略6",
        require_5m_gate=False,
        use_stop70=False,
        use_ema_cross=True,
        take_profit_pct=0.40,
        overbought_rsi=80.0,
    ),


def run_rebound_backtest_24h(ohlcv: list[list[float]], config: BacktestConfig, now_ts: int | None = None) -> BacktestResult:
    if len(ohlcv) < 30:
        return BacktestResult(config.name, 0.0, 0.0, 0.0, 0)

    now_ts = now_ts or int(datetime.now(timezone.utc).timestamp())
    start_ts = now_ts - 24 * 3600

    closes = [float(c[4]) for c in ohlcv]
    rsi_vals = rsi(closes, 9)
    ema9_vals = ema(closes, 9)
    ema20_vals = ema(closes, 20)
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

    for i in range(2, len(ohlcv)):
        ts = int(float(ohlcv[i][0]))
        if ts < start_ts:
            continue

        close_now = float(ohlcv[i][4])
        rsi_prev = rsi_vals[i - 2]
        rsi_now = rsi_vals[i - 1]
        ema9_prev = ema9_vals[i - 1]
        ema9_now = ema9_vals[i]
        ema20_prev = ema20_vals[i - 1]
        ema20_now = ema20_vals[i]

        allow_open = True
        if config.require_5m_gate:
            bucket = ts // 300
            if bucket not in ema5_map:
                allow_open = False
            else:
                gate_ema9, gate_ema20 = ema5_map[bucket]
                allow_open = gate_ema9 > gate_ema20

        has_pos = first_entry is not None

        if has_pos:
            if config.use_ema_cross:
                profit_hit = (
                    config.take_profit_pct is not None
                    and first_entry is not None
                    and first_entry > 0
                    and close_now >= first_entry * (1.0 + config.take_profit_pct)
                )
                sell = (
                    (ema9_prev >= ema20_prev and ema9_now < ema20_now)
                    or profit_hit
                    or rsi_now >= config.overbought_rsi
                )
            else:
                sell = (
                    rsi_now >= 85
                    or cross_down(rsi_prev, rsi_now, 75)
                    or cross_down(rsi_prev, rsi_now, 70)
                    or cross_down(rsi_prev, rsi_now, 65)
                )
            if config.use_stop70 and first_entry is not None and close_now <= first_entry * 0.70:
                sell = True

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

            if (not config.use_ema_cross) and allow_open and (not added_once) and first_entry is not None and cross_up(rsi_prev, rsi_now, 30) and close_now <= first_entry * 0.90:
                add_entry = close_now
                added_once = True
                continue

        if (not has_pos) and allow_open and (
            cross_up(ema9_prev, ema9_now, ema20_now)
            if config.use_ema_cross
            else cross_up(rsi_prev, rsi_now, 30)
        ):
            first_entry = close_now
            add_entry = None
            added_once = False
            trades += 1

    unrealized = 0.0
    if first_entry is not None and first_entry > 0:
        last_close = float(ohlcv[-1][4])
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
            "rebound_add_entry_price": token.rebound_add_entry_price,
            "realized_pnl_sol": token.realized_pnl_sol,
            "startup_entry_price": token.startup_entry_price,
            "startup_last_buy_bucket": token.startup_last_buy_bucket,
            "startup_last_cross_bucket": token.startup_last_cross_bucket,
            "startup_last_entry_cross_bucket": token.startup_last_entry_cross_bucket,
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
                close_5m_prev = closes_5m[-2] if len(closes_5m) >= 2 else None
                close_5m_prev2 = closes_5m[-3] if len(closes_5m) >= 3 else None
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

                # single-strategy mode: ignore legacy startup position state
                if token.startup_entry_price is not None:
                    token.startup_entry_price = None

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
                )

                if signal == Signal.BUY:
                    if token.rebound_entry_price is None:
                        token.rebound_entry_price = close
                    token.position.has_position = token.rebound_entry_price is not None
                elif signal == Signal.ADD:
                    token.position.has_position = True
                    token.position.added_once = True
                    if token.rebound_add_entry_price is None:
                        token.rebound_add_entry_price = close
                elif signal == Signal.SELL:
                    if close > 0 and token.rebound_entry_price and token.rebound_entry_price > 0:
                        legs = [token.rebound_entry_price]
                        if token.rebound_add_entry_price and token.rebound_add_entry_price > 0:
                            legs.append(token.rebound_add_entry_price)
                        token.realized_pnl_sol += sum((close / entry) - 1.0 for entry in legs)
                    token.rebound_entry_price = None
                    token.rebound_add_entry_price = None
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

    async def _handle_whitelist_exit(self, token: TokenRecord) -> None:
        too_low_fdv = token.fdv is not None and token.fdv < 20000
        # age-based auto-exit is paused
        too_old = False
        if not too_low_fdv:
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
                signal=Signal.SELL if too_low_fdv else Signal.HOLD,
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
        current_pnl_sol = 0.0
        current_pnl_text = "N/A"
        if t.price and t.price > 0 and t.rebound_entry_price and t.rebound_entry_price > 0:
            legs = [t.rebound_entry_price]
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

    for t in service.tokens.values():
        try:
            ohlcv = await service.gecko.fetch_ohlcv_24h(t.network, t.address)
            if len(ohlcv) < 30:
                continue
            results = [run_rebound_backtest_24h(ohlcv, cfg) for cfg in BACKTEST_CONFIGS]
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

    return templates.TemplateResponse("backtest_dashboard.html", {"request": request, "rows": rows})


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}
