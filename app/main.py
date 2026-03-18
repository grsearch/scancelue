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
    liquidity: float | None = None
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
    last_sell_at: datetime | None = None  # 卖出后冷静期：20分钟内禁止开单
    add_source: str = "manual"  # 加入来源：manual/webhook/auto_scan
    exit_check_fail_count: int = 0  # 连续低于退出阈值的次数，需连续2次才真正移除
    exit_check_fail_at: datetime | None = None  # 第一次检测失败的时间
    buy_tax: float | None = None   # 买入税率
    sell_tax: float | None = None  # 卖出税率
    rebound_peak_price: float | None = None  # 持仓后最高价，用于移动止盈
    rugcheck_checked: bool = False  # 是否已做过 RugCheck 检测（只检测一次）
    rugcheck_retry_count: int = 0  # RugCheck 重试次数，超过3次放弃
    jupiter_checked: bool = False  # 是否已做过 Jupiter 税率检测（只检测一次）
    lp_locked_pct: float | None = None  # LP 锁定比例


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
        # FIX #1: 默认拉500根K线，减少新币早期RSI噪声
        limit: int = 500,
        before_timestamp: int | None = None,
    ) -> list[list[float]]:
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

        liquidity: float | None = None
        overview_data = overview_payload.get("data") or {}
        if isinstance(overview_data, dict):
            fdv = self._to_float(
                overview_data.get("fdv")
                or overview_data.get("fullyDilutedValuation")
                or overview_data.get("mc")
                or overview_data.get("marketCap")
            )
            created_at = overview_data.get("createdAt") or overview_data.get("created_at") or overview_data.get("pairCreatedAt")
            buy_tax = self._to_float(
                overview_data.get("buy_tax") or overview_data.get("buyTax") or overview_data.get("buy_fee")
            )
            sell_tax = self._to_float(
                overview_data.get("sell_tax") or overview_data.get("sellTax") or overview_data.get("sell_fee")
            )
            liquidity = self._to_float(
                overview_data.get("liquidity")
                or overview_data.get("liquidityUsd")
                or overview_data.get("lp")
            )

        return {
            "fdv": float(fdv or 0),
            "price": float(price or 0),
            "liquidity": float(liquidity or 0),
            "buy_tax": buy_tax,
            "sell_tax": sell_tax,
            "pool_created_at": created_at,
            "created_at": created_at,
        }

    async def fetch_rugcheck_tax(self, token: str) -> dict[str, Any]:
        """
        从 RugCheck API 获取代币税率信息。
        无需 API Key，免费调用。
        返回：{buy_tax, sell_tax, score, rugged, risks}
        """
        url = f"https://api.rugcheck.xyz/v1/tokens/{token}/report"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return {}
                payload = resp.json()
        except Exception:
            return {}

        buy_tax: float | None = None
        sell_tax: float | None = None

        # 方式1：transferFee 字段（新版 RugCheck）
        transfer_fee = payload.get("transferFee") or {}
        if isinstance(transfer_fee, dict):
            fee_pct = self._to_float(transfer_fee.get("pct") or transfer_fee.get("percent") or transfer_fee.get("rate"))
            if fee_pct is not None:
                # RugCheck 的 transferFee 是买卖都收的统一费率
                if fee_pct > 1:
                    fee_pct = fee_pct / 100  # 百分比转小数
                buy_tax = fee_pct
                sell_tax = fee_pct

        # 方式2：从 risks 列表里找税率关键词
        if buy_tax is None:
            risks = payload.get("risks") or []
            for risk in risks:
                if not isinstance(risk, dict):
                    continue
                name = str(risk.get("name") or "").lower()
                desc = str(risk.get("description") or "").lower()
                value = str(risk.get("value") or "")
                if "tax" in name or "fee" in name or "tax" in desc or "transfer fee" in desc:
                    extracted = self._to_float(value.replace("%", "").strip())
                    if extracted is not None:
                        if extracted > 1:
                            extracted = extracted / 100
                        buy_tax = extracted
                        sell_tax = extracted
                        break

        # 方式3：token_extensions 里的 transferFeeConfig
        token_extensions = payload.get("token_extensions") or {}
        if buy_tax is None and isinstance(token_extensions, dict):
            tfc = token_extensions.get("transferFeeConfig") or {}
            if isinstance(tfc, dict):
                basis_points = self._to_float(tfc.get("newerTransferFee", {}).get("transferFeeBasisPoints") or
                                               tfc.get("olderTransferFee", {}).get("transferFeeBasisPoints"))
                if basis_points is not None:
                    buy_tax = basis_points / 10000  # basis points 转小数
                    sell_tax = basis_points / 10000

        # LP Locked 检测
        lp_locked_pct: float | None = None
        # 方式1：markets 里的 lp_locked_pct
        markets = payload.get("markets") or []
        for market in markets:
            if not isinstance(market, dict):
                continue
            lp = self._to_float(
                market.get("lp_locked_pct")
                or market.get("lpLockedPct")
                or market.get("lp_lock_pct")
            )
            if lp is not None:
                # 取最大值（最主要的池子）
                lp_locked_pct = max(lp_locked_pct or 0.0, lp)

        # 方式2：顶层 lp_locked_pct 字段
        if lp_locked_pct is None:
            lp_locked_pct = self._to_float(
                payload.get("lp_locked_pct")
                or payload.get("lpLockedPct")
                or payload.get("lp_lock")
            )

        # 统一转换为小数（RugCheck 有时返回百分比如 99.97，有时返回小数如 0.9997）
        if lp_locked_pct is not None and lp_locked_pct > 1:
            lp_locked_pct = lp_locked_pct / 100

        return {
            "buy_tax": buy_tax,
            "sell_tax": sell_tax,
            "lp_locked_pct": lp_locked_pct,
            "score": payload.get("score"),
            "rugged": payload.get("rugged", False),
            "risks": [r.get("name") for r in (payload.get("risks") or []) if isinstance(r, dict)],
        }

    async def fetch_jupiter_tax(self, token: str, amount_lamports: int = 1_000_000_000) -> dict[str, Any]:
        """
        用 Jupiter Quote API 模拟一买一卖，检测合约 transfer tax。
        原理：
          1. 用 1 SOL 买入 token，得到 out_amount
          2. 用 out_amount 卖回 SOL，得到 sol_back
          3. 总损耗 = (1 SOL - sol_back) / 1 SOL
          4. 减去两次 price_impact，剩余的就是 transfer tax
        注意：只检测合约层面的 transfer tax，不检测 LP fee（LP fee 由 Jupiter 选最优路径绕开）
        """
        SOL_MINT = "So11111111111111111111111111111111111111112"
        base_url = os.getenv("JUPITER_BASE_URL", "https://api.jup.ag")
        api_key = os.getenv("JUPITER_API_KEY", "").strip()
        headers: dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # 第一步：SOL -> Token 买入报价
                buy_resp = await client.get(
                    f"{base_url}/v6/quote",
                    params={
                        "inputMint": SOL_MINT,
                        "outputMint": token,
                        "amount": str(amount_lamports),
                        "slippageBps": "0",
                        "platformFeeBps": "0",
                    },
                    headers=headers,
                )
                if buy_resp.status_code != 200:
                    return {"error": f"buy quote failed: {buy_resp.status_code}"}
                buy_data = buy_resp.json()

                tokens_out = int(buy_data.get("outAmount") or 0)
                buy_price_impact = float(buy_data.get("priceImpactPct") or 0)

                if tokens_out <= 0:
                    return {"error": "buy quote returned 0 tokens"}

                # 第二步：Token -> SOL 卖出报价
                sell_resp = await client.get(
                    f"{base_url}/v6/quote",
                    params={
                        "inputMint": token,
                        "outputMint": SOL_MINT,
                        "amount": str(tokens_out),
                        "slippageBps": "0",
                        "platformFeeBps": "0",
                    },
                    headers=headers,
                )
                if sell_resp.status_code != 200:
                    return {"error": f"sell quote failed: {sell_resp.status_code}"}
                sell_data = sell_resp.json()

                sol_back = int(sell_data.get("outAmount") or 0)
                sell_price_impact = float(sell_data.get("priceImpactPct") or 0)

            if amount_lamports <= 0:
                return {"error": "invalid amount"}

            total_loss_pct = (amount_lamports - sol_back) / amount_lamports
            price_impact_total = (buy_price_impact + sell_price_impact) / 100
            # transfer tax = 总损耗 - 正常价格影响
            estimated_tax = max(0.0, total_loss_pct - price_impact_total)

            return {
                "buy_tax": round(estimated_tax / 2, 4),
                "sell_tax": round(estimated_tax / 2, 4),
                "total_tax": round(estimated_tax, 4),
                "total_loss_pct": round(total_loss_pct, 4),
                "price_impact_total": round(price_impact_total, 4),
                "sol_in": amount_lamports,
                "sol_back": sol_back,
                "tokens_out": tokens_out,
            }

        except Exception as exc:
            return {"error": str(exc)}

    async def fetch_trending(self, network: str = "solana", limit: int = 20) -> list[dict[str, Any]]:
        """拉取 trending 代币列表，返回基础信息供扫描筛选。"""
        url = f"{self.base}/defi/token_trending"
        params: dict[str, Any] = {"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": limit}
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url, params=params, headers=self._headers(network))
            resp.raise_for_status()
            payload = resp.json()
        items = (payload.get("data") or {}).get("tokens") or (payload.get("data") or [])
        if isinstance(items, dict):
            items = items.get("items") or items.get("list") or []
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            address = item.get("address") or item.get("mint") or ""
            symbol = item.get("symbol") or item.get("name") or ""
            fdv = self._to_float(item.get("fdv") or item.get("mc") or item.get("marketCap")) or 0.0
            liquidity = self._to_float(item.get("liquidity") or item.get("liquidityUsd")) or 0.0
            created_at = item.get("createdAt") or item.get("created_at") or item.get("pairCreatedAt")
            if address:
                out.append({
                    "address": address,
                    "symbol": symbol,
                    "fdv": fdv,
                    "liquidity": liquidity,
                    "created_at": created_at,
                })
        return out

    async def fetch_ohlcv_2h(self, network: str, token: str) -> list[list[float]]:
        """拉取最近2小时1分钟K线，用于计算近期振幅。"""
        return await self.fetch_ohlcv(network=network, token=token, timeframe="minute", limit=120)

    async def fetch_ohlcv_24h(self, network: str, token: str) -> list[list[float]]:
        """Fetch up to ~24h minute bars using paginated requests compatible with endpoint limits."""
        now_ts = int(datetime.now(timezone.utc).timestamp())
        start_ts = now_ts - 24 * 3600

        all_rows: list[list[float]] = []
        before_ts: int | None = None
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
                except Exception as exc:
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
        # WS 默认关闭，使用1分钟REST轮询作为主链路，避免WS鉴权问题
        self.enabled = os.getenv("BIRDEYE_WS_ENABLED", "0") == "1"
        self._last_error_log_ts: float = 0.0

    def _ws_headers(self) -> list[tuple[str, str]]:
        # FIX #3: 返回 list of tuples，websockets 库正确识别此格式
        # dict 在 websockets 12.x 会被静默忽略导致 403
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
        return list(headers.items())

    @staticmethod
    def _key_fingerprint(raw_key: str) -> str:
        key = raw_key.strip()
        if not key:
            return "empty"
        prefix = key[:6]
        suffix = key[-4:] if len(key) >= 4 else key
        return f"{prefix}...{suffix} len={len(key)} trim_changed={raw_key != key}"

    def _build_ws_url(self) -> tuple[str, str]:
        # FIX #3: 不把 API key 放进 query string，只走 header
        raw_key = os.getenv("BIRDEYE_API_KEY", "")
        return self.ws_url, self._key_fingerprint(raw_key)

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
                # FIX #3: headers 现在是 list of tuples，websockets 正确传入握手 header
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
                            reason=f"Birdeye WS connect: {self.ws_url} key={fingerprint} header_keys={[k for k,v in headers]}",
                        )
                    )
                    self.service.logs = self.service.logs[-500:]
                    self.service.save_state()
                async with websockets.connect(  # type: ignore[attr-defined]
                    ws_url,
                    ping_interval=20,
                    ping_timeout=20,
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
        volumes_1m: list[float] | None,
        ema9_1m: float,
        ema20_1m: float,
        rsi1_prev: float,
        rsi1_now: float,
        allow_open_rebound: bool,
        # FIX #5: 保留参数签名兼容性，但标注为未使用
        close_5m: float | None = None,
        ema9_5m: float | None = None,
        ema20_5m: float | None = None,
        ema9_5m_prev: float | None = None,
        ema20_5m_prev: float | None = None,
        rsi5_prev: float | None = None,
        rsi5_now: float | None = None,
        startup_bucket: int | None = None,
        close_5m_prev: float | None = None,
        close_5m_prev2: float | None = None,
        token_age_hours: float | None = None,
        ema9_1m_prev: float | None = None,
        ema20_1m_prev: float | None = None,
        ema9_vals_full: list[float] | None = None,
        ema20_vals_full: list[float] | None = None,
    ) -> tuple[StrategyName, Signal, str]:
        # FIX #2: 用 closes_1m[-2] 作为已确认收盘价，排除活K干扰
        close_1m = closes_1m[-2] if len(closes_1m) >= 2 else closes_1m[-1]
        has_rebound = token.rebound_entry_price is not None

        if has_rebound:
            entry = token.rebound_entry_price

            # 更新持仓最高价（用于移动止盈）
            if token.rebound_peak_price is None or close_1m > token.rebound_peak_price:
                token.rebound_peak_price = close_1m

            peak = token.rebound_peak_price or close_1m

            # 卖出条件1：RSI超买（>=85）
            if rsi1_now >= 85:
                return StrategyName.REBOUND, Signal.SELL, f"反弹策略卖出：RSI超买({rsi1_now:.1f}>=85)"

            # 卖出条件2：移动止盈 - 从最高点回撤12%
            # RSI < 35 时暂停移动止盈（超卖区反弹刚开始，给更多空间）
            trailing_stop = peak * 0.88
            if close_1m <= trailing_stop and rsi1_now > 35:
                gain_pct = (peak / entry - 1.0) * 100 if entry and entry > 0 else 0.0
                return StrategyName.REBOUND, Signal.SELL, f"移动止盈：最高{peak:.4f}回撤12%至{trailing_stop:.4f}，峰值收益{gain_pct:.1f}%"

            # 卖出条件3：跌破首仓止损线（-10%）
            if entry is not None and close_1m <= entry * 0.90:
                return StrategyName.REBOUND, Signal.SELL, f"止损：跌破首仓价{entry:.4f}的90%"

        if not has_rebound and cross_up(rsi1_prev, rsi1_now, 30):
            now_utc = datetime.now(timezone.utc)

            # 实盘过滤1：卖出后20分钟冷静期，禁止开单
            if token.last_sell_at is not None:
                cooldown_mins = (now_utc - token.last_sell_at).total_seconds() / 60
                if cooldown_mins < 20:
                    return StrategyName.REBOUND, Signal.HOLD, f"卖出冷静期：距上次卖出{cooldown_mins:.1f}分钟，需等满20分钟"

            # 实盘过滤2：EMA间距过滤（间距>15% 或 间距在扩大 则不开单）
            if ema20_1m > 0 and ema9_1m_prev is not None and ema20_1m_prev is not None and ema20_1m_prev > 0:
                ema_gap_now = abs(ema9_1m - ema20_1m) / ema20_1m
                ema_gap_prev = abs(ema9_1m_prev - ema20_1m_prev) / ema20_1m_prev
                if ema_gap_now > 0.15:
                    return StrategyName.REBOUND, Signal.HOLD, f"EMA间距过大({ema_gap_now*100:.1f}%)，趋势过强不开单"
                # 比较前3根均值，排除单根抖动误判
                if (
                    ema9_vals_full is not None and ema20_vals_full is not None
                    and len(ema9_vals_full) >= 4 and len(ema20_vals_full) >= 4
                ):
                    valid = [
                        abs(ema9_vals_full[j] - ema20_vals_full[j]) / ema20_vals_full[j]
                        for j in range(-4, -1)
                        if ema20_vals_full[j] > 0
                    ]
                    ema_gap_avg3 = sum(valid) / len(valid) if valid else ema_gap_prev
                else:
                    ema_gap_avg3 = ema_gap_prev
                if ema_gap_now >= ema_gap_avg3:
                    return StrategyName.REBOUND, Signal.HOLD, f"EMA间距未收窄(前3根均值{ema_gap_avg3*100:.1f}%→当前{ema_gap_now*100:.1f}%)，趋势未减弱"


            # 实盘过滤4：价格企稳（最近3根不创新低，或出现大阳线止跌）
            if len(closes_1m) >= 5:
                stab_lows = [closes_1m[i] for i in range(-4, -1)]
                prev_low = closes_1m[-5]
                no_new_low = all(l >= prev_low * 0.995 for l in stab_lows)
                # 大阳线例外：最近1根涨幅>2%，说明已出现止跌信号，直接放行
                last_candle_gain = (closes_1m[-2] - closes_1m[-3]) / closes_1m[-3] if closes_1m[-3] > 0 else 0.0
                big_bullish = last_candle_gain > 0.02
                if not (no_new_low or big_bullish):
                    return StrategyName.REBOUND, Signal.HOLD, f"价格未企稳：最近3根创新低且无大阳线({last_candle_gain*100:.1f}%<2%)"

            return StrategyName.REBOUND, Signal.BUY, "反弹策略买入：RSI上穿30+EMA收窄+价格企稳"

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
    # 反弹策略3 前置过滤条件
    use_filter3: bool = False          # 是否启用四条前置条件
    filter3_min_drop_ranging: float = 0.20   # 震荡结构最小跌幅
    filter3_min_drop_trending: float = 0.30  # 趋势结构最小跌幅
    filter3_lookback_high: int = 60    # 前高回看窗口（根K线数）
    filter3_stability_bars: int = 3    # 价格企稳判断K线数
    filter3_vol_ratio: float = 1.5     # 放量判断倍数（均量倍数）
    filter3_min_interval: int = 20     # 两次买入最小间隔（K线数）
    filter3_rsi_strength: float = 60.0 # 近期RSI最高值门槛
    filter3_ema_gap_max: float = 0.15    # EMA间距上限（超过则不买）
    filter3_ema_gap_narrowing: bool = True  # 是否要求间距收窄
    trailing_stop_pct: float | None = None  # 移动止盈回撤比例（None=不启用）


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
        # 对齐实盘：EMA收窄 + 跌幅门槛 + 价格企稳 + 移动止盈12% + 止损10%
        # 与实盘唯一差异：回测无法模拟冷静期20分钟和价格区间结构判断
        mode="rebound",
        candle_minutes=1,
        require_open_gate=False,
        buy_rsi_threshold=30.0,
        enable_add=False,
        stop_loss_pct=0.90,
        sell_cross_65=False,            # 实盘已移除RSI下穿卖出
        sell_cross_70=False,
        sell_cross_75=False,
        overbought_rsi=85.0,
        use_filter3=True,
        filter3_min_drop_ranging=0.0,   # 对齐实盘：已移除跌幅门槛
        filter3_min_drop_trending=0.0,
        filter3_lookback_high=60,
        filter3_stability_bars=3,       # 价格企稳：最近3根不创新低
        filter3_vol_ratio=999.0,        # 量能不限制（实盘已移除此条件）
        filter3_min_interval=20,        # 两次买入间隔≥20根K线
        filter3_rsi_strength=0.0,
        filter3_ema_gap_max=0.15,       # EMA间距≤15%
        filter3_ema_gap_narrowing=True, # EMA间距必须收窄
        trailing_stop_pct=0.12,         # 对齐实盘移动止盈12%
    ),
    BacktestConfig(
        name="反弹策略3",
        # 对齐实盘但保留RSI下穿卖出，用于对比移动止盈效果
        mode="rebound",
        candle_minutes=1,
        require_open_gate=False,
        buy_rsi_threshold=30.0,
        enable_add=False,
        stop_loss_pct=0.90,
        sell_cross_65=True,             # 保留RSI下穿，用于对比
        sell_cross_70=True,
        sell_cross_75=True,
        overbought_rsi=85.0,
        use_filter3=True,
        filter3_min_drop_ranging=0.0,   # 对齐实盘：已移除跌幅门槛
        filter3_min_drop_trending=0.0,
        filter3_lookback_high=60,
        filter3_stability_bars=3,
        filter3_vol_ratio=999.0,        # 量能不限制
        filter3_min_interval=20,
        filter3_rsi_strength=0.0,
        filter3_ema_gap_max=0.15,
        filter3_ema_gap_narrowing=True,
        trailing_stop_pct=0.12,         # 对齐实盘移动止盈12%
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
    last_buy_bar: int = -999  # 反弹策略3：记录上次买入K线索引
    peak_price: float | None = None  # 移动止盈：持仓最高价

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
                # 更新持仓最高价（移动止盈用）
                if peak_price is None or close_now > peak_price:
                    peak_price = close_now

                # 移动止盈：从最高点回撤 trailing_stop_pct
                trailing_sell = (
                    config.trailing_stop_pct is not None
                    and peak_price is not None
                    and close_now <= peak_price * (1 - config.trailing_stop_pct)
                )

                # RSI卖出条件（移动止盈开启时只保留超买，关闭时保留RSI下穿）
                if config.trailing_stop_pct is not None:
                    # 启用移动止盈：只保留超买卖出和止损
                    rsi_sell = rsi_now >= config.overbought_rsi
                else:
                    # 未启用移动止盈：保留原RSI下穿逻辑
                    rsi_sell = (
                        rsi_now >= config.overbought_rsi
                        or (config.sell_cross_75 and cross_down(rsi_prev, rsi_now, 75))
                        or (config.sell_cross_70 and cross_down(rsi_prev, rsi_now, 70))
                        or (config.sell_cross_65 and cross_down(rsi_prev, rsi_now, 65))
                    )

                stop_loss_sell = (
                    config.stop_loss_pct is not None
                    and first_entry is not None
                    and first_entry > 0
                    and close_now <= first_entry * config.stop_loss_pct
                )

                sell = trailing_sell or rsi_sell or stop_loss_sell

                if sell and first_entry is not None and first_entry > 0:
                    legs = [first_entry]
                    if add_entry is not None and add_entry > 0:
                        legs.append(add_entry)
                    realized += sum((close_now / entry) - 1.0 for entry in legs)
                    trades += 1
                    first_entry = None
                    add_entry = None
                    added_once = False
                    peak_price = None
                    continue

                if config.enable_add and (not added_once) and first_entry is not None and cross_up(rsi_prev, rsi_now, config.buy_rsi_threshold) and close_now <= first_entry * config.add_drop_pct:
                    add_entry = close_now
                    added_once = True
                    trades += 1
                    continue

            if (not has_pos) and cross_up(rsi_prev, rsi_now, config.buy_rsi_threshold):
                # 反弹策略3：四条前置过滤条件
                allow_buy = True
                if config.use_filter3 and i >= config.filter3_lookback_high:
                    # --- 条件1：区分结构（震荡/震荡上升 vs 单边下跌）---
                    # 近30根内价格有没有创新低（如果一直创新低说明单边下跌，不买）
                    window_lows = lows[max(0, i - 30):i]
                    is_still_making_lows = (
                        len(window_lows) >= 5
                        and min(window_lows[-5:]) < min(window_lows[:5]) * 0.97
                    )
                    # 近期RSI最高值要超过60（归零币RSI永远涨不上去）
                    rsi_window = rsi_vals[max(0, i - 30):i]
                    rsi_max_recent = max(rsi_window) if rsi_window else 0.0
                    rsi_has_strength = rsi_max_recent >= config.filter3_rsi_strength
                    is_ranging_or_up = (not is_still_making_lows) and rsi_has_strength

                    # --- 条件2：跌幅门槛 ---
                    lookback = min(config.filter3_lookback_high, i)
                    recent_high = max(closes[i - lookback:i]) if lookback > 0 else close_now
                    drop_pct = (recent_high - close_now) / recent_high if recent_high > 0 else 0.0
                    min_drop = config.filter3_min_drop_ranging if is_ranging_or_up else config.filter3_min_drop_trending
                    drop_ok = drop_pct >= min_drop

                    # --- 条件3：买点前有放量下跌（最好有，没有也允许但量不能在放大）---
                    vol_window = vols[max(0, i - 10):i]
                    vol_avg = sum(vol_window) / len(vol_window) if vol_window else 0.0
                    # 近5根有没有放量阴线（量 > 均量1.5倍 且 是阴线）
                    had_vol_drop = False
                    for j in range(max(1, i - 5), i):
                        if j < len(closes) and j < len(vols):
                            if closes[j] < closes[j - 1] and vols[j] > vol_avg * config.filter3_vol_ratio:
                                had_vol_drop = True
                                break
                    # 退而求其次：当前量能没有持续放大也可以接受
                    recent_vol3 = sum(vols[max(0, i - 3):i]) / 3 if i >= 3 else vol_avg
                    prev_vol5 = sum(vols[max(0, i - 8):i - 3]) / 5 if i >= 8 else vol_avg
                    vol_not_expanding = recent_vol3 <= prev_vol5 * 1.2
                    vol_ok = had_vol_drop or vol_not_expanding

                    # --- 条件4：价格企稳（不创新低 或 大阳线止跌，对齐实盘）---
                    stab = config.filter3_stability_bars
                    if i >= stab + 1:
                        stab_lows = lows[i - stab:i]
                        prev_low = lows[i - stab - 1]
                        no_new_low = all(l >= prev_low * 0.995 for l in stab_lows)
                    else:
                        no_new_low = False
                    # 大阳线例外：最近1根涨幅>2%直接放行
                    if i >= 2 and closes[i - 2] > 0:
                        big_bullish = (closes[i - 1] - closes[i - 2]) / closes[i - 2] > 0.02
                    else:
                        big_bullish = False
                    price_stable = no_new_low or big_bullish

                    # --- 两次买入最小间隔（过滤RSI钝化连发）---
                    interval_ok = (last_buy_bar < 0) or (i - last_buy_bar >= config.filter3_min_interval)

                    # --- EMA间距过滤（前3根均值，对齐实盘）---
                    if ema20_now > 0 and i >= 4:
                        ema_gap_now_pct = abs(ema9_now - ema20_now) / ema20_now
                        # 前3根均值
                        avg3_gaps = [
                            abs(ema9_vals[j] - ema20_vals[j]) / ema20_vals[j]
                            for j in range(i - 3, i)
                            if ema20_vals[j] > 0
                        ]
                        ema_gap_avg3 = sum(avg3_gaps) / len(avg3_gaps) if avg3_gaps else ema_gap_now_pct
                        gap_ok = ema_gap_now_pct <= config.filter3_ema_gap_max
                        gap_narrowing = (not config.filter3_ema_gap_narrowing) or (ema_gap_now_pct < ema_gap_avg3)
                        ema_gap_ok = gap_ok and gap_narrowing
                    else:
                        ema_gap_ok = True

                    allow_buy = is_ranging_or_up and drop_ok and vol_ok and price_stable and interval_ok and ema_gap_ok

                if allow_buy:
                    first_entry = close_now
                    add_entry = None
                    added_once = False
                    last_buy_bar = i
                    peak_price = close_now  # 初始化移动止盈最高价
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
            "liquidity": token.liquidity,
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
            "last_sell_at": self._dt_to_str(token.last_sell_at),
            "add_source": token.add_source,
            "exit_check_fail_count": token.exit_check_fail_count,
            "exit_check_fail_at": self._dt_to_str(token.exit_check_fail_at),
            "buy_tax": token.buy_tax,
            "sell_tax": token.sell_tax,
            "rebound_peak_price": token.rebound_peak_price,
            "rugcheck_checked": token.rugcheck_checked,
            "rugcheck_retry_count": token.rugcheck_retry_count,
            "jupiter_checked": token.jupiter_checked,
            "lp_locked_pct": token.lp_locked_pct,
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
            liquidity=payload.get("liquidity"),
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
            last_sell_at=parse_cg_datetime(payload.get("last_sell_at")),
            add_source=str(payload.get("add_source", "manual")),
            exit_check_fail_count=int(payload.get("exit_check_fail_count", 0)),
            exit_check_fail_at=parse_cg_datetime(payload.get("exit_check_fail_at")),
            buy_tax=payload.get("buy_tax"),
            sell_tax=payload.get("sell_tax"),
            rebound_peak_price=payload.get("rebound_peak_price"),
            rugcheck_checked=bool(payload.get("rugcheck_checked", False)),
            rugcheck_retry_count=int(payload.get("rugcheck_retry_count", 0)),
            jupiter_checked=bool(payload.get("jupiter_checked", False)),
            lp_locked_pct=payload.get("lp_locked_pct"),
        )

    async def auto_scan_trending(self) -> None:
        """
        定时扫描 Birdeye trending 列表，自动将符合条件的币加入白名单。
        筛选条件：
          - FDV >= 100000
          - LP >= 10000
          - AGE 1小时 ~ 7天
          - 近2小时振幅 >= 30%
          - 当前价 >= 近2小时最高价 * 60%（排除已崩塌的币）
        """
        # 环境变量控制是否启用自动扫描
        if os.getenv("AUTO_SCAN_ENABLED", "1") != "1":
            return
        try:
            trending = await self.market_data.fetch_trending(network="solana", limit=20)
        except Exception as exc:
            self.logs.append(SignalLog(
                ts=datetime.now(timezone.utc), symbol="system", address="-",
                strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                reason=f"auto_scan fetch_trending error: {exc}",
            ))
            self.logs = self.logs[-500:]
            return

        added = 0
        for item in trending:
            address = item.get("address", "")
            symbol = item.get("symbol", "")
            fdv = float(item.get("fdv") or 0)
            liquidity = float(item.get("liquidity") or 0)
            created_at_raw = item.get("created_at")

            if not address:
                continue

            # 已在白名单或黑名单，跳过
            async with self.lock:
                already_in = address in self.tokens or address in self.blacklist
            if already_in:
                continue

            # FDV >= 100000
            if fdv < 100000:
                continue

            # LP >= 10000
            if liquidity < 10000:
                continue

            # AGE：1小时 ~ 7天
            pool_created = parse_cg_datetime(created_at_raw)
            if pool_created is not None:
                age_hours = (datetime.now(timezone.utc) - pool_created).total_seconds() / 3600
                if age_hours < 1.0 or age_hours > 168.0:  # 168h = 7天
                    continue

            # 拉取近2小时K线，计算振幅
            try:
                ohlcv_2h = await self.market_data.fetch_ohlcv_2h("solana", address)
                if len(ohlcv_2h) < 10:
                    continue
                recent_high = max(float(row[2]) for row in ohlcv_2h)
                recent_low = min(float(row[3]) for row in ohlcv_2h)
                current_price = float(ohlcv_2h[-1][4])
                if recent_low <= 0:
                    continue
                amplitude_2h = (recent_high - recent_low) / recent_low
                price_ratio = current_price / recent_high if recent_high > 0 else 0.0

                # 近2小时振幅 >= 30%
                if amplitude_2h < 0.30:
                    continue

                # 当前价 >= 近2小时最高价 * 60%（排除已崩塌）
                if price_ratio < 0.60:
                    continue

            except Exception:
                continue

            # 符合条件，加入白名单
            try:
                await self.add_token(
                    AddTokenRequest(network="solana", address=address, symbol=symbol),
                    source="auto_scan",
                )
                added += 1
                self.logs.append(SignalLog(
                    ts=datetime.now(timezone.utc), symbol=symbol, address=address,
                    strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                    reason=f"auto_scan加入白名单：振幅{amplitude_2h*100:.1f}% 当前价/高点{price_ratio*100:.1f}% FDV={fdv:,.0f}",
                ))
                self.logs = self.logs[-500:]
            except Exception:
                continue

        if added > 0:
            self.save_state()

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

    async def _check_token_tax(self, token: TokenRecord) -> None:
        """收录代币时在后台做一次税率检测，之后不再重复。"""
        if token.jupiter_checked:
            return
        try:
            jupiter_key = os.getenv("JUPITER_API_KEY", "").strip()
            if jupiter_key:
                samples = []
                for _ in range(3):
                    jup = await self.market_data.fetch_jupiter_tax(token.address)
                    if "error" not in jup and jup.get("total_tax") is not None:
                        samples.append(jup["total_tax"])
                    await asyncio.sleep(0.5)
                if samples:
                    avg_total_tax = sum(samples) / len(samples)
                    token.buy_tax = round(avg_total_tax / 2, 4)
                    token.sell_tax = round(avg_total_tax / 2, 4)
                    token.jupiter_checked = True
                    self.logs.append(SignalLog(
                        ts=datetime.now(timezone.utc),
                        symbol=token.symbol, address=token.address,
                        strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                        reason=f"[收录时]Jupiter税率检测({len(samples)}次均值)：买入税={token.buy_tax*100:.2f}% 卖出税={token.sell_tax*100:.2f}% 样本={[round(x*100,2) for x in samples]}",
                    ))
                    self.logs = self.logs[-500:]
                    self.save_state()
                    return
            # 无Jupiter Key 或 Jupiter失败，用RugCheck
            rc = await self.market_data.fetch_rugcheck_tax(token.address)
            if rc.get("buy_tax") is not None:
                token.buy_tax = rc["buy_tax"]
                token.jupiter_checked = True
            if rc.get("sell_tax") is not None:
                token.sell_tax = rc["sell_tax"]
            self.save_state()
        except Exception:
            pass  # 失败不标记，下次 add_token 时重试

    async def add_token(self, req: AddTokenRequest, source: str = "manual") -> TokenRecord:
        """统一入口：手动/webhook/auto_scan 三种方式均走此方法，自动去重。"""
        if req.network.lower() != "solana":
            raise HTTPException(400, "仅支持 solana")
        async with self.lock:
            if req.address in self.blacklist:
                raise HTTPException(400, "该代币在黑名单中")
            token = self.tokens.get(req.address)
            if token:
                # 已存在：自动去重，不重复加入，只更新来源记录
                return token
            token = TokenRecord(
                network=req.network.lower(),
                address=req.address,
                symbol=req.symbol,
                add_source=source,
            )
            self.tokens[req.address] = token
            self.save_state()
            await self.notify_realtime_event(req.address)
            # 收录时立即在后台做一次税率检测，不阻塞主流程
            asyncio.create_task(self._check_token_tax(token))
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
            # FIX: 被抑制的事件不直接丢弃，而是重新入队延迟处理
            # 避免 RSI 穿越恰好落在抑制窗口内时信号永久丢失
            async def _reschedule() -> None:
                await asyncio.sleep(5.0)
                await self.realtime_queue.put(address)
            asyncio.create_task(_reschedule())
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
            token.liquidity = meta.get("liquidity")
            # 只在首次（未做过Jupiter检测）时用Birdeye的税率，避免覆盖已缓存的检测结果
            if not token.jupiter_checked:
                token.buy_tax = meta.get("buy_tax")
                token.sell_tax = meta.get("sell_tax")

            # 税率检测在收录时已通过 _check_token_tax 完成，此处无需重复检测

                # RugCheck 首次检测：rugged + LP Locked
                # 最多重试3次，超过3次放弃（免费API有频率限制）
                if not token.rugcheck_checked and token.rugcheck_retry_count < 3:
                    try:
                        rc_check = await self.market_data.fetch_rugcheck_tax(token.address)
                        token.rugcheck_checked = True

                        # 检测已 Rug
                        if rc_check.get("rugged"):
                            self.logs.append(SignalLog(
                                ts=datetime.now(timezone.utc),
                                symbol=token.symbol, address=token.address,
                                strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                                reason="RugCheck标记为已Rug，移除白名单",
                            ))
                            self.logs = self.logs[-500:]
                            async with self.lock:
                                self.tokens.pop(token.address, None)
                                self.blacklist.add(token.address)
                            self.save_state()
                            return

                        # 检测 LP Locked
                        lp_locked = rc_check.get("lp_locked_pct")
                        if lp_locked is not None:
                            token.lp_locked_pct = lp_locked
                            if lp_locked < 0.90:
                                self.logs.append(SignalLog(
                                    ts=datetime.now(timezone.utc),
                                    symbol=token.symbol, address=token.address,
                                    strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                                    reason=f"LP锁定不足({lp_locked*100:.2f}% < 90%)，移除白名单",
                                ))
                                self.logs = self.logs[-500:]
                                if token.rebound_entry_price is not None or token.startup_entry_price is not None:
                                    await self.dispatcher.send(token, Signal.SELL, "LP锁定不足，强制平仓")
                                async with self.lock:
                                    self.tokens.pop(token.address, None)
                                    self.blacklist.add(token.address)
                                self.save_state()
                                return

                        self.logs.append(SignalLog(
                            ts=datetime.now(timezone.utc),
                            symbol=token.symbol, address=token.address,
                            strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                            reason=f"RugCheck检测完成：LP锁定={lp_locked*100:.2f}% rugged=False" if lp_locked is not None else "RugCheck检测完成：LP锁定数据不可用",
                        ))
                        self.logs = self.logs[-500:]
                        self.save_state()
                    except Exception:
                        # 失败计数+1，超过3次不再重试
                        token.rugcheck_retry_count += 1
                        if token.rugcheck_retry_count >= 3:
                            token.rugcheck_checked = True  # 放弃检测，不再重试
                            self.logs.append(SignalLog(
                                ts=datetime.now(timezone.utc),
                                symbol=token.symbol, address=token.address,
                                strategy=StrategyName.SELL_ONLY, signal=Signal.HOLD,
                                reason=f"RugCheck检测失败{token.rugcheck_retry_count}次，放弃检测（免费API限制）",
                            ))
                            self.logs = self.logs[-500:]
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

            # FIX #1: 拉取更多K线（500根），减少RSI预热期噪声
            ohlcv = await self.market_data.fetch_ohlcv(token.network, token.address, limit=500)
            closes = [float(row[4]) for row in ohlcv if len(row) >= 5]
            volumes = [float(row[5]) for row in ohlcv if len(row) >= 6]
            # 至少需要 20 根已确认K线（排除活K后还有足够数据）
            if len(closes) < 20:
                return
            ema9_vals = ema(closes, 9)
            ema20_vals = ema(closes, 20)
            rsi_vals = rsi(closes, 9)
            # FIX #2: 至少需要3个RSI值，才能用[-3]和[-2]（排除活K）
            if len(rsi_vals) < 3:
                return
            ema9_now = ema9_vals[-1]
            ema20_now = ema20_vals[-1]
            ema9_prev_1m = ema9_vals[-2] if len(ema9_vals) >= 2 else None
            ema20_prev_1m = ema20_vals[-2] if len(ema20_vals) >= 2 else None

            # FIX #2 & #4: 使用已确认K线的RSI（排除最后一根活K）
            # rsi_vals[-1] 对应活K，rsi_vals[-2]/[-3] 对应已收盘K线
            rsi_prev = rsi_vals[-3]
            rsi_now = rsi_vals[-2]
            # 已确认收盘价（排除活K）
            close = closes[-2]

            base_age = token.pool_created_at or token.added_at
            token_age_hours = max(0.0, (datetime.now(timezone.utc) - base_age).total_seconds() / 3600)

            startup_bucket = int(float(ohlcv[-1][0])) // 60 if ohlcv else None
            ema9_5m = ema20_5m = ema9_5m_prev = ema20_5m_prev = None
            rsi5_prev = rsi5_now = None
            close_5m = close
            close_5m_prev = closes[-3] if len(closes) >= 3 else None
            close_5m_prev2 = closes[-4] if len(closes) >= 4 else None
            allow_open_rebound = True

            strategy, signal, reason = self.engine.evaluate(
                token,
                closes,
                volumes,
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
                ema9_vals,
                ema20_vals,
            )

            # 记录当前 RSI 到 token（用于 dashboard 展示）
            token.last_rsi = rsi_now

            if signal == Signal.BUY:
                if strategy == StrategyName.STARTUP:
                    if token.startup_entry_price is None:
                        token.startup_entry_price = close
                    token.position.has_position = token.startup_entry_price is not None
                else:
                    if token.rebound_entry_price is None:
                        token.rebound_entry_price = close
                        token.rebound_peak_price = close  # 初始化移动止盈最高价
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
                token.rebound_peak_price = None  # 重置移动止盈最高价
                token.startup_entry_price = None
                token.startup_last_cross_bucket = 0
                token.startup_last_entry_cross_bucket = 0
                token.position.added_once = False
                token.position.has_position = False
                token.last_sell_at = datetime.now(timezone.utc)  # 记录卖出时间，触发20分钟冷静期

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
        # 税率过高直接移除（买卖税率任一超过1.2%）
        # 税率为 None 说明 API 未返回，不触发退出
        max_tax = 0.03  # 3%
        buy_tax_val = token.buy_tax if token.buy_tax is not None else 0.0
        sell_tax_val = token.sell_tax if token.sell_tax is not None else 0.0
        # Birdeye 有时返回百分比值（如5.0代表5%），有时返回小数（如0.05代表5%）
        # 统一处理：如果值>1说明是百分比格式，除以100转换
        if buy_tax_val > 1:
            buy_tax_val = buy_tax_val / 100
        if sell_tax_val > 1:
            sell_tax_val = sell_tax_val / 100
        too_high_tax = (
            token.buy_tax is not None and buy_tax_val > max_tax
        ) or (
            token.sell_tax is not None and sell_tax_val > max_tax
        )
        if too_high_tax:
            self.logs.append(SignalLog(
                ts=datetime.now(timezone.utc),
                symbol=token.symbol,
                address=token.address,
                strategy=StrategyName.SELL_ONLY,
                signal=Signal.SELL if (token.rebound_entry_price or token.startup_entry_price) else Signal.HOLD,
                reason=f"税率过高移除：买入税={buy_tax_val*100:.2f}% 卖出税={sell_tax_val*100:.2f}%，超过3%上限",
            ))
            self.logs = self.logs[-500:]
            if token.rebound_entry_price is not None or token.startup_entry_price is not None:
                await self.dispatcher.send(token, Signal.SELL, "税率过高，强制平仓")
            self.tokens.pop(token.address, None)
            self.blacklist.add(token.address)
            self.save_state()
            return

        # FDV=0 或 liquidity=0 说明 API 返回异常数据，不触发退出
        # 只有明确有值且低于阈值才计入
        fdv_valid = token.fdv is not None and token.fdv > 0
        lp_valid = token.liquidity is not None and token.liquidity > 0
        too_low_fdv = fdv_valid and token.fdv < 50000
        too_low_lp = lp_valid and token.liquidity < 10000

        if not (too_low_fdv or too_low_lp):
            # 条件正常，重置连续失败计数
            token.exit_check_fail_count = 0
            return

        # 连续2次检测都低于阈值且间隔>=5分钟才真正移除，防止 API 抖动误移除
        now_utc = datetime.now(timezone.utc)
        if token.exit_check_fail_count == 0:
            # 第一次失败：记录时间，不移除
            token.exit_check_fail_count = 1
            token.exit_check_fail_at = now_utc
            self.logs.append(SignalLog(
                ts=now_utc,
                symbol=token.symbol,
                address=token.address,
                strategy=StrategyName.SELL_ONLY,
                signal=Signal.HOLD,
                reason=f"退出预警第1次：FDV={token.fdv:.0f} LP={token.liquidity:.0f}，5分钟后再次确认",
            ))
            self.logs = self.logs[-500:]
            self.save_state()
            return
        else:
            # 第二次失败：检查距第一次是否超过5分钟
            first_fail_mins = (now_utc - token.exit_check_fail_at).total_seconds() / 60 if token.exit_check_fail_at else 999
            if first_fail_mins < 5:
                self.logs.append(SignalLog(
                    ts=now_utc,
                    symbol=token.symbol,
                    address=token.address,
                    strategy=StrategyName.SELL_ONLY,
                    signal=Signal.HOLD,
                    reason=f"退出预警等待中（距首次{first_fail_mins:.1f}分钟 < 5分钟）：FDV={token.fdv:.0f} LP={token.liquidity:.0f}",
                ))
                self.logs = self.logs[-500:]
                self.save_state()
                return
            # 超过5分钟仍低于阈值，确认移除
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
                signal=Signal.SELL if (too_low_fdv or too_low_lp) else Signal.HOLD,
                reason=f"白名单移除并加入黑名单（FDV低={too_low_fdv} LP低={too_low_lp}）",
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
        while True:
            address = await service.realtime_queue.get()
            await service.process_realtime_event(address)

    async def auto_scan_loop() -> None:
        # 启动延迟30秒，等服务完全就绪后再开始扫描
        await asyncio.sleep(30)
        while True:
            await service.auto_scan_trending()
            # 每5分钟扫描一次
            await asyncio.sleep(int(os.getenv("AUTO_SCAN_INTERVAL_SECONDS", "300")))

    asyncio.create_task(periodic_reconcile_loop())
    asyncio.create_task(realtime_signal_loop())
    asyncio.create_task(ws_consumer.run_forever())
    asyncio.create_task(auto_scan_loop())


@app.post("/webhook/add-token")
async def add_token(req: AddTokenRequest) -> dict[str, Any]:
    token = await service.add_token(req, source="webhook")
    return {
        "ok": True,
        "token": {"network": token.network, "address": token.address, "symbol": token.symbol},
        "message": "已加入白名单并开始1分钟K线监控",
        "source": token.add_source,
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
                "lp": f"{(t.liquidity or 0):,.0f}",
                "address": t.address,
                "gmgn": f"https://gmgn.ai/sol/token/{t.address}",
                "pnl": current_pnl_text,
                "total_pnl": total_pnl_text,
                "last_rsi": f"{t.last_rsi:.1f}" if t.last_rsi is not None else "N/A",
                "source": t.add_source,
                "tax": f"买{(t.buy_tax or 0)*100:.1f}%/卖{(t.sell_tax or 0)*100:.1f}%" if (t.buy_tax is not None or t.sell_tax is not None) else "N/A",
                "lp_locked": f"{t.lp_locked_pct*100:.1f}%" if t.lp_locked_pct is not None else "未检测",
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
