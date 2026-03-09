from datetime import datetime, timezone

from app.main import (
    MarketState,
    PositionState,
    Signal,
    StrategyEngine,
    TokenRecord,
    ema,
    format_token_age,
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


def test_format_token_age_uses_created_time_only():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    assert format_token_age(created, now) == "1.50h"
    assert format_token_age(None, now) == "N/A"
