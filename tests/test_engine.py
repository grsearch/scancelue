import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.main import (
    BACKTEST_CONFIGS,
    BacktestConfig,
    MonitorService,
    PositionState,
    Signal,
    StrategyEngine,
    StrategyName,
    run_rebound_backtest_24h,
    TokenRecord,
    cross_down,
    cross_up,
    ema,
    format_pool_age,
    parse_cg_datetime,
    rsi,
    seconds_until_next_tick,
)


def test_ema_rsi_shapes_and_cross_helpers():
    closes = [float(i) for i in range(1, 80)]
    assert len(ema(closes, 9)) == len(closes)
    assert len(rsi(closes, 9)) >= 2
    assert cross_up(29, 30, 30)
    assert cross_down(75, 74.5, 75)


def test_rebound_buy_and_sell_logic():
    token = TokenRecord(network="solana", address="a", symbol="A")

    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 101, 102], 102, 101, 29, 31, True,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 99, 98], 99, 100, 71, 69, True,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.SELL


def test_rebound_can_open_without_5m_gate():
    token = TokenRecord(network="solana", address="r1", symbol="R1")
    strategy, signal, reason = StrategyEngine.evaluate(
        token, [100, 101, 102], 102, 101, 29, 31, False,
        None, None, None, None, None, None, None, None,
        None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.BUY


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
    assert restored.tokens["addr1"].no_open_mode is False
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


def test_whitelist_exit_when_age_gt_24h_and_fdv_lt_50k():
    svc = MonitorService()

    class _DummyDispatcher:
        async def send(self, token, signal, reason):
            return None

    svc.dispatcher = _DummyDispatcher()

    token = TokenRecord(
        network="solana",
        address="old_token",
        symbol="OLD",
        added_at=datetime.now(timezone.utc).replace(microsecond=0),
    )
    token.added_at = datetime.now(timezone.utc) - timedelta(hours=25)
    token.fdv = 40000
    svc.tokens[token.address] = token

    asyncio.run(svc._handle_whitelist_exit(token))

    assert token.address in svc.blacklist
    assert token.address not in svc.tokens


def test_whitelist_not_exit_when_age_gt_24h_but_fdv_ge_50k():
    svc = MonitorService()

    class _DummyDispatcher:
        async def send(self, token, signal, reason):
            return None

    svc.dispatcher = _DummyDispatcher()

    token = TokenRecord(network="solana", address="keep_token", symbol="KEEP")
    token.added_at = datetime.now(timezone.utc) - timedelta(hours=30)
    token.fdv = 60000
    svc.tokens[token.address] = token

    asyncio.run(svc._handle_whitelist_exit(token))

    assert token.address not in svc.blacklist
    assert token.address in svc.tokens


def test_backtest_module_runs_and_returns_metrics():
    base_ts = 1_700_000_000
    closes = [100.0] * 25 + [92.0, 88.0, 85.0, 83.0, 81.0, 84.0, 88.0, 93.0, 99.0, 105.0, 98.0, 90.0, 80.0, 72.0, 68.0, 70.0, 73.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="反弹策略", mode="rebound")
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "反弹策略"
    assert isinstance(res.total_pnl_sol, float)
    assert isinstance(res.realized_pnl_sol, float)
    assert isinstance(res.unrealized_pnl_sol, float)
    assert isinstance(res.trades, int)


def test_backtest_rebound_strategy_add_and_sell():
    base_ts = 1_700_000_000
    closes = [100.0] * 20 + [99.0, 98.0, 99.0, 100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 105.0, 100.0, 96.0, 93.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="反弹策略", mode="rebound")
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "反弹策略"
    assert res.trades >= 1


def test_backtest_trend_strategy_runs():
    base_ts = 1_700_000_000
    closes = [100.0] * 30 + [99.0, 98.0, 97.0, 98.0, 100.0, 103.0, 105.0, 104.0, 102.0, 99.0, 96.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="趋势策略", mode="trend")
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "趋势策略"
    assert isinstance(res.trades, int)



def test_backtest_trend_strategy_buy_and_sell_on_cross():
    base_ts = 1_700_000_000
    # 构造先金叉再死叉的序列
    closes = [100.0] * 30 + [99.0, 98.0, 97.0, 98.0, 100.0, 103.0, 106.0, 104.0, 101.0, 98.0, 95.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="趋势策略", mode="trend")
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "趋势策略"
    assert res.trades >= 1



def test_backtest_configs_include_rebound12_only():
    names = [cfg.name for cfg in BACKTEST_CONFIGS]
    assert names == ["反弹策略", "趋势策略"]

    cfg1 = next(cfg for cfg in BACKTEST_CONFIGS if cfg.name == "反弹策略")
    cfg2 = next(cfg for cfg in BACKTEST_CONFIGS if cfg.name == "趋势策略")
    assert cfg1.candle_minutes == 5
    assert cfg2.candle_minutes == 5
    assert cfg1.require_open_gate is False
    assert cfg1.buy_rsi_threshold == 30.0
    assert cfg1.enable_add is False
    assert cfg1.stop_loss_pct is None
    assert cfg1.sell_cross_65 is False
    assert cfg2.require_open_gate is False
    assert cfg2.mode == "trend"
    assert cfg2.enable_add is False
    assert cfg2.stop_loss_pct is None
    assert cfg2.sell_cross_65 is False



def test_seconds_until_next_tick_aligns_to_5m_boundary_with_buffer():
    now_ts = 12 * 3600 + 3 * 60 + 20
    assert seconds_until_next_tick(now_ts=now_ts, interval_seconds=300, buffer_seconds=5) == 105


def test_seconds_until_next_tick_negative_buffer_is_clamped():
    now_ts = 12 * 3600 + 4 * 60 + 59
    assert seconds_until_next_tick(now_ts=now_ts, interval_seconds=300, buffer_seconds=-7) == 1
