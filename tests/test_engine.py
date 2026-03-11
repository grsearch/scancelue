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


def test_rebound_buy_add_sell_and_stoploss_logic():
    token = TokenRecord(network="solana", address="a", symbol="A")

    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 101, 102], 102, 101, 34, 36, True,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 95, 90], 92, 91, 34, 36, True,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.ADD

    token.position.added_once = True
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 95, 90], 92, 91, 71, 69, True,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.SELL

    # stoploss at 70% of first entry
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 80, 69], 80, 85, 45, 44, True,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.SELL



def test_rebound_open_gate_blocks_buy_and_add_when_5m_not_ready():
    token = TokenRecord(network="solana", address="g", symbol="G")
    strategy, signal, reason = StrategyEngine.evaluate(
        token, [100, 101, 102], 102, 101, 34, 36, False,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.HOLD
    assert "禁止开单" in reason

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 95, 90], 92, 91, 34, 36, False,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.HOLD


def test_hold_when_no_signal():
    token = TokenRecord(network="solana", address="c", symbol="C")
    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True)
    strategy, signal, reason = StrategyEngine.evaluate(
        token, [100, 99, 98], 99, 100, 50, 49, True,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.HOLD
    assert "无买卖信号" in reason


def test_persistence_roundtrip(tmp_path: Path):
    svc = MonitorService()
    svc.state_path = tmp_path / "state.json"

    token = TokenRecord(network="solana", address="addr1", symbol="AAA")
    token.fdv = 12345
    token.rebound_entry_price = 0.8
    token.rebound_add_entry_price = 0.6
    token.realized_pnl_sol = 1.23
    token.position = PositionState(has_position=True, added_once=True)
    svc.tokens[token.address] = token
    svc.blacklist.add("addr2")
    svc.save_state()

    restored = MonitorService()
    restored.state_path = svc.state_path
    restored.load_state()

    assert "addr1" in restored.tokens
    assert restored.tokens["addr1"].rebound_entry_price == 0.8
    assert restored.tokens["addr1"].rebound_add_entry_price == 0.6
    assert restored.tokens["addr1"].realized_pnl_sol == 1.23
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
