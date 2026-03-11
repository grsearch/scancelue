from datetime import datetime, timezone
from pathlib import Path

from app.main import (
    MonitorService,
    PositionState,
    Signal,
    StrategyEngine,
    StrategyName,
    TokenRecord,
    cross_down,
    cross_up,
    ema,
    format_pool_age,
    parse_cg_datetime,
    resample_5m_closes,
    rsi,
)


def test_ema_rsi_shapes_and_cross_helpers():
    closes = [float(i) for i in range(1, 80)]
    assert len(ema(closes, 9)) == len(closes)
    assert len(rsi(closes, 9)) >= 2
    assert cross_up(29, 30, 30)
    assert cross_down(75, 74.5, 75)


def test_resample_5m_closes_allows_partial_liquidity():
    ohlcv = [
        [0, 0, 0, 0, 1.0, 0],
        [60, 0, 0, 0, 1.1, 0],
        [120, 0, 0, 0, 1.2, 0],
        [300, 0, 0, 0, 2.0, 0],
    ]
    bars = resample_5m_closes(ohlcv)
    assert bars == [(0, 1.2), (1, 2.0)]


def test_rebound_buy_add_sell_logic():
    token = TokenRecord(network="solana", address="a", symbol="A")
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 101, 102, 103], 102, 101, 29, 31, True, None, None, None, None, None, None, None, None)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 95, 90], 92, 91, 29, 31, True, None, None, None, None, None, None, None, None)
    assert signal == Signal.ADD

    token.position.added_once = True
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 95, 90], 92, 91, 71, 69, True, None, None, None, None, None, None, None, None)
    assert signal == Signal.SELL

    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 99, 98], 98, 97, 45, 46, True, 98, 101, 95, 99, 94, 50, 49, 10, 97, 99)
    assert signal == Signal.SELL


def test_startup_buy_and_sell_on_5m_logic():
    token = TokenRecord(network="solana", address="b", symbol="B")

    # bar1: cross appears, should not buy immediately
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [100, 101, 102],
        101,
        100,
        40,
        45,
        False,
        102,
        100,
        99,
        98,
        99,
        60,
        62,
        123,
        101,
        100,
    )
    assert signal == Signal.HOLD

    # bar2: in window, state valid -> BUY
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [101, 102, 103],
        102,
        101,
        45,
        48,
        False,
        103,
        101,
        100,
        100,
        99,
        62,
        64,
        124,
        102,
        101,
    )
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.BUY

    token.startup_entry_price = 102
    token.position = PositionState(has_position=True)
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [102, 101, 100],
        100,
        101,
        50,
        48,
        False,
        100,
        99,
        101,
        102,
        100,
        74,
        68,
        125,
        101,
        102,
    )
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.SELL


def test_open_gate_hold_without_stoploss():
    token = TokenRecord(network="solana", address="c", symbol="C")
    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True)
    strategy, signal, reason = StrategyEngine.evaluate(token, [100, 95, 90], 95, 96, 45, 44, True, None, None, None, None, None, None, None, None)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.HOLD

    flat = TokenRecord(network="solana", address="d", symbol="D")
    strategy, signal, reason = StrategyEngine.evaluate(flat, [1, 1.01, 1.02], 1.0, 1.0, 49, 52, False, None, None, None, None, None, None, None, None)
    assert signal == Signal.HOLD
    assert "无买卖信号" in reason or "禁止开单" in reason


def test_persistence_roundtrip(tmp_path: Path):
    svc = MonitorService()
    svc.state_path = tmp_path / "state.json"

    token = TokenRecord(network="solana", address="addr1", symbol="AAA")
    token.fdv = 12345
    token.rebound_entry_price = 0.8
    token.startup_entry_price = 0.9
    token.startup_last_cross_bucket = 777
    token.position = PositionState(has_position=True, added_once=True)
    svc.tokens[token.address] = token
    svc.blacklist.add("addr2")
    svc.save_state()

    restored = MonitorService()
    restored.state_path = svc.state_path
    restored.load_state()

    assert "addr1" in restored.tokens
    assert restored.tokens["addr1"].rebound_entry_price == 0.8
    assert restored.tokens["addr1"].startup_entry_price == 0.9
    assert restored.tokens["addr1"].startup_last_cross_bucket == 777
    assert restored.tokens["addr1"].position.added_once is True
    assert "addr2" in restored.blacklist


def test_datetime_and_age_helpers():
    sec = parse_cg_datetime(1700000000)
    ms = parse_cg_datetime(1700000000000)
    assert sec == ms

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    assert format_pool_age(created, now) == "1.50h"
    assert format_pool_age(None, now) == "N/A"

    added = datetime(2026, 1, 1, 11, 0, tzinfo=timezone.utc)
    assert format_pool_age(None, now, added) == "1.00h"


def test_startup_buy_only_once_per_5m_cross_bucket():
    token = TokenRecord(network="solana", address="x", symbol="X")
    token.startup_last_cross_bucket = 199
    args = (
        [100, 101, 102], 101, 100, 40, 45, False,
        102, 100, 99, 98, 99, 60, 62, 200, 101, 100,
    )
    strategy, signal, _ = StrategyEngine.evaluate(token, *args)
    assert strategy == StrategyName.STARTUP and signal == Signal.BUY
    token.startup_last_buy_bucket = 200
    strategy, signal, _ = StrategyEngine.evaluate(token, *args)
    assert signal == Signal.HOLD


def test_startup_buy_allowed_on_2nd_3rd_bar_and_blocked_on_two_red_bars():
    token = TokenRecord(network="solana", address="y", symbol="Y")

    # mark cross at bucket 300
    StrategyEngine.evaluate(
        token,
        [100, 101, 102],
        101,
        100,
        40,
        45,
        False,
        102,
        100,
        99,
        98,
        99,
        60,
        62,
        300,
        101,
        100,
    )

    # bar2 after cross can buy
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [101, 102, 103],
        102,
        101,
        45,
        48,
        False,
        103,
        101,
        100,
        100,
        99,
        62,
        64,
        301,
        102,
        101,
    )
    assert strategy == StrategyName.STARTUP and signal == Signal.BUY

    # reset position to test bar3 filter
    token.startup_entry_price = None
    token.position = PositionState(has_position=False)
    token.startup_last_buy_bucket = None

    # bar3 with two consecutive red closes should be blocked
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [101, 100, 99],
        100,
        99,
        48,
        46,
        False,
        99,
        100,
        99,
        100,
        98,
        60,
        58,
        302,
        100,
        101,
    )
    assert signal == Signal.HOLD

    # outside window should not buy
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [101, 102, 103],
        102,
        101,
        45,
        48,
        False,
        103,
        101,
        100,
        100,
        99,
        62,
        64,
        304,
        102,
        101,
    )
    assert signal == Signal.HOLD
