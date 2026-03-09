from pathlib import Path
from datetime import datetime, timezone

from app.main import (
    MarketState,
    PositionState,
    Signal,
    StrategyEngine,
    TokenRecord,
    MonitorService,
    ema,
    format_pool_age,
    parse_cg_datetime,
    recent_pump_ratio,
    distance_from_ema,
    max_recent_candle_gain,
    rsi,
)


def test_ema_and_rsi_shapes():
    closes = [float(i) for i in range(1, 40)]
    assert len(ema(closes, 9)) == len(closes)
    rv = rsi(closes, 9)
    assert len(rv) >= 2


def test_market_state_and_confirm_switch():
    token = TokenRecord(network="solana", address="x", symbol="T")
    engine = StrategyEngine()
    raw = engine.raw_state(11, 10, 9.5, 60)
    assert raw == MarketState.TREND
    state = engine.confirm_state(token, raw)
    assert state == MarketState.RANGE
    state = engine.confirm_state(token, raw)
    assert state == MarketState.TREND


def test_sell_only_never_buy():
    token = TokenRecord(network="solana", address="x", symbol="T")
    token.confirmed_state = MarketState.DOWN
    token.position = PositionState(has_position=False)
    strategy, sig, _ = StrategyEngine.evaluate(token, [1, 1.1, 1.0], 1.01, 1.05, 55, 45)
    assert sig in {Signal.HOLD, Signal.SELL}
    assert sig != Signal.BUY
    assert sig != Signal.ADD
    assert strategy.value == "sell_only"


def test_format_pool_age_uses_pool_created_time_only():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    assert format_pool_age(created, now) == "1.50h"
    assert format_pool_age(None, now) == "N/A"


def test_parse_cg_datetime_supports_seconds_and_milliseconds():
    sec = parse_cg_datetime(1700000000)
    ms = parse_cg_datetime(1700000000000)
    assert sec is not None and sec.tzinfo is not None
    assert ms is not None and ms.tzinfo is not None
    assert sec == ms


def test_persistence_roundtrip(tmp_path: Path):
    svc = MonitorService()
    svc.state_path = tmp_path / "state.json"

    token = TokenRecord(network="solana", address="addr1", symbol="AAA")
    token.fdv = 12345
    token.price = 0.12
    svc.tokens[token.address] = token
    svc.blacklist.add("addr2")
    svc.save_state()

    restored = MonitorService()
    restored.state_path = svc.state_path
    restored.load_state()

    assert "addr1" in restored.tokens
    assert restored.tokens["addr1"].symbol == "AAA"
    assert restored.tokens["addr1"].fdv == 12345
    assert "addr2" in restored.blacklist


def test_recent_pump_ratio():
    closes = [100, 101, 102, 111]
    assert round(recent_pump_ratio(closes, 3), 4) == 0.11


def test_range_buy_uses_cross_up_30():
    token = TokenRecord(network="solana", address="x2", symbol="R")
    token.confirmed_state = MarketState.RANGE
    token.position = PositionState(has_position=False)
    strategy, sig, _ = StrategyEngine.evaluate(token, [1, 1.0, 1.01, 1.02, 1.03, 1.04], 1.03, 1.02, 29.5, 31.0)
    assert strategy.value == "rsi_strategy"
    assert sig == Signal.BUY


def test_raw_state_can_enter_pre_trend_earlier():
    state = StrategyEngine.raw_state(10.0, 10.02, 10.01, 53)
    assert state == MarketState.PRE_TREND


def test_pre_trend_switch_requires_single_confirmation():
    token = TokenRecord(network="solana", address="p1", symbol="P")
    state = StrategyEngine.confirm_state(token, MarketState.PRE_TREND)
    assert state == MarketState.PRE_TREND


def test_trend_anti_chase_filter_blocks_overheated_buy():
    token = TokenRecord(network="solana", address="hot", symbol="H")
    token.confirmed_state = MarketState.TREND
    closes = [100, 101, 102, 104, 106, 108, 110]
    strategy, sig, _ = StrategyEngine.evaluate(token, closes, 106, 103, 65, 69)
    assert strategy.value == "trend_strategy"
    assert sig == Signal.HOLD


def test_helper_dist_and_candle_gain():
    assert round(distance_from_ema(103, 100), 4) == 0.03
    assert round(max_recent_candle_gain([100, 103, 105], 2), 4) == 0.03
