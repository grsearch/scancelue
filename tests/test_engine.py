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
    closes = [float(i) for i in range(1, 50)]
    assert len(ema(closes, 9)) == len(closes)
    assert len(rsi(closes, 9)) >= 2
    assert cross_up(29, 30, 30)
    assert cross_down(75, 74.5, 75)


def test_rebound_buy_and_sell_logic():
    token = TokenRecord(network="solana", address="a", symbol="A")
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 101, 102, 103], 102, 101, 29, 31, True)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY

    token.position = PositionState(has_position=True)
    token.active_strategy = StrategyName.REBOUND
    token.entry_price = 103
    strategy, signal, _ = StrategyEngine.evaluate(token, [103, 104, 103], 103, 102, 66, 64, True)
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.SELL


def test_startup_buy_and_sell_logic():
    token = TokenRecord(network="solana", address="b", symbol="B")
    strategy, signal, _ = StrategyEngine.evaluate(token, [100, 101, 102, 103], 101, 100, 49, 55, True)
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.BUY

    token.position = PositionState(has_position=True)
    token.active_strategy = StrategyName.STARTUP
    token.entry_price = 103
    strategy, signal, _ = StrategyEngine.evaluate(token, [103, 104, 105], 104, 103, 74, 76, True)
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.SELL


def test_stop_loss_10_percent_and_open_gate():
    token = TokenRecord(network="solana", address="c", symbol="C")
    token.position = PositionState(has_position=True)
    token.active_strategy = StrategyName.STARTUP
    token.entry_price = 100
    strategy, signal, reason = StrategyEngine.evaluate(token, [100, 95, 90], 95, 96, 45, 44, True)
    assert strategy == StrategyName.STARTUP
    assert signal == Signal.SELL
    assert "10%止损" in reason

    flat = TokenRecord(network="solana", address="d", symbol="D")
    strategy, signal, reason = StrategyEngine.evaluate(flat, [1, 1.01, 1.02], 1.0, 1.0, 49, 52, False)
    assert signal == Signal.HOLD
    assert "禁止开单" in reason


def test_persistence_roundtrip(tmp_path: Path):
    svc = MonitorService()
    svc.state_path = tmp_path / "state.json"

    token = TokenRecord(network="solana", address="addr1", symbol="AAA")
    token.fdv = 12345
    token.entry_price = 0.8
    token.active_strategy = StrategyName.REBOUND
    svc.tokens[token.address] = token
    svc.blacklist.add("addr2")
    svc.save_state()

    restored = MonitorService()
    restored.state_path = svc.state_path
    restored.load_state()

    assert "addr1" in restored.tokens
    assert restored.tokens["addr1"].active_strategy == StrategyName.REBOUND
    assert restored.tokens["addr1"].entry_price == 0.8
    assert "addr2" in restored.blacklist


def test_datetime_and_age_helpers():
    sec = parse_cg_datetime(1700000000)
    ms = parse_cg_datetime(1700000000000)
    assert sec == ms

    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    created = datetime(2026, 1, 1, 10, 30, tzinfo=timezone.utc)
    assert format_pool_age(created, now) == "1.50h"
    assert format_pool_age(None, now) == "N/A"
