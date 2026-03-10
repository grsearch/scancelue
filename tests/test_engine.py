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
    rsi,
)


def test_ema_rsi_shapes_and_cross_helpers():
    closes = [float(i) for i in range(1, 80)]
    assert len(ema(closes, 9)) == len(closes)
    assert len(rsi(closes, 9)) >= 2
    assert cross_up(29, 30, 30)
    assert cross_down(75, 74.5, 75)


def test_rebound_buy_add_sell_logic():
    token = TokenRecord(network="solana", address="a", symbol="A")
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 101, 102, 103], 102, 101, 29, 31, True, None, None, None, None, None, None, None, None, None)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 95, 90], 92, 91, 29, 31, True, None, None, None, None, None, None, None, None, None)
    assert signal == Signal.ADD

    token.position.added_once = True
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 95, 90], 92, 91, 66, 64, True, None, None, None, None, None, None, None, None, None)
    assert signal == Signal.SELL


def test_startup_buy_and_sell_on_5m_logic():
    token = TokenRecord(network="solana", address="b", symbol="B")
    strategy, signal, _ = StrategyEngine.evaluate(
        token,
        [100, 101, 102],
        101,
        100,
        40,
        45,
        False,  # rebound gate closed should not block startup
        102,
        100,
        99,
        98,
        99,
        60,
        62,
        123,
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
        124,
    )
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.SELL


def test_open_gate_hold_without_stoploss():
    token = TokenRecord(network="solana", address="c", symbol="C")
    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True)
    strategy, signal, reason = StrategyEngine.evaluate(token, [100, 95, 90], 95, 96, 45, 44, True, None, None, None, None, None, None, None, None, None)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.HOLD
    assert "持仓中" in reason

    flat = TokenRecord(network="solana", address="d", symbol="D")
    strategy, signal, reason = StrategyEngine.evaluate(flat, [1, 1.01, 1.02], 1.0, 1.0, 49, 52, False, None, None, None, None, None, None, None, None, None)
    assert signal == Signal.HOLD
    assert "无买卖信号" in reason or "禁止开单" in reason


def test_persistence_roundtrip(tmp_path: Path):
    svc = MonitorService()
    svc.state_path = tmp_path / "state.json"

    token = TokenRecord(network="solana", address="addr1", symbol="AAA")
    token.fdv = 12345
    token.rebound_entry_price = 0.8
    token.startup_entry_price = 0.9
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


def test_startup_buy_only_once_per_5m_cross_bucket():
    token = TokenRecord(network="solana", address="x", symbol="X")
    args = (
        [100, 101, 102], 101, 100, 40, 45, False,
        102, 100, 99, 98, 99, 60, 62, 200,
    )
    strategy, signal, _ = StrategyEngine.evaluate(token, *args)
    assert strategy == StrategyName.STARTUP and signal == Signal.BUY
    token.startup_last_buy_bucket = 200
    strategy, signal, _ = StrategyEngine.evaluate(token, *args)
    assert signal == Signal.HOLD
