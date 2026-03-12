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
)


def test_ema_rsi_shapes_and_cross_helpers():
    closes = [float(i) for i in range(1, 80)]
    assert len(ema(closes, 9)) == len(closes)
    assert len(rsi(closes, 9)) >= 2
    assert cross_up(29, 30, 30)
    assert cross_down(75, 74.5, 75)


def test_rebound_buy_add_sell_logic():
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
        token, [100, 95, 90], 92, 91, 29, 31, True,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.ADD

    token.position.added_once = True
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 95, 90], 92, 91, 71, 69, True,
        None, None, None, None, None, None, None, None,
    )
    assert signal == Signal.SELL




def test_rebound_open_gate_blocks_buy_and_add_when_5m_not_ready():
    token = TokenRecord(network="solana", address="g", symbol="G")
    strategy, signal, reason = StrategyEngine.evaluate(
        token, [100, 101, 102], 102, 101, 29, 31, False,
        None, None, None, None, None, None, None, None,
    )
    assert strategy == StrategyName.REBOUND
    assert signal == Signal.HOLD
    assert "禁止开单" in reason

    token.rebound_entry_price = 100
    token.position = PositionState(has_position=True, added_once=False)
    strategy, signal, _ = StrategyEngine.evaluate(
        token, [100, 95, 90], 92, 91, 29, 31, False,
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


def test_whitelist_exit_uses_added_at_when_pool_created_at_missing():
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
    svc.tokens[token.address] = token

    asyncio.run(svc._handle_whitelist_exit(token))

    assert token.address in svc.blacklist
    assert token.address not in svc.tokens



def test_backtest_module_runs_and_returns_metrics():
    base_ts = 1_700_000_000
    closes = [100.0] * 25 + [92.0, 88.0, 85.0, 83.0, 81.0, 84.0, 88.0, 93.0, 99.0, 105.0, 98.0, 90.0, 80.0, 72.0, 68.0, 70.0, 73.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="策略X", require_5m_gate=False, use_stop70=True)
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "策略X"
    assert isinstance(res.total_pnl_sol, float)
    assert isinstance(res.realized_pnl_sol, float)
    assert isinstance(res.unrealized_pnl_sol, float)
    assert isinstance(res.trades, int)


def test_backtest_strategy5_ema_cross_buy_sell():
    base_ts = 1_700_000_000
    closes = [100.0] * 20 + [99.0, 98.0, 99.0, 100.0, 102.0, 104.0, 106.0, 108.0, 110.0, 105.0, 100.0, 96.0, 93.0]
    ohlcv = []
    for i, c in enumerate(closes):
        ts = base_ts + i * 60
        ohlcv.append([ts, c, c, c, c, 1000])

    cfg = BacktestConfig(name="策略5", require_5m_gate=False, use_stop70=False, use_ema_cross=True)
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "策略5"
    assert res.trades >= 2


def test_backtest_strategy6_ema_cross_runs_without_5m_gate():
    base_ts = 1_700_000_000
    # 策略6无5m门槛，使用EMA交叉，可在数据内产生交易
    cfg = BacktestConfig(name="策略6", require_5m_gate=False, use_stop70=False, use_ema_cross=True)
    assert res.trades >= 1
def test_backtest_strategy6_takes_profit_at_40_percent():
    # 先形成EMA上穿开仓，再快速拉升超过40%触发止盈
    closes = [100.0] * 30 + [95.0, 92.0, 94.0, 98.0, 105.0, 120.0, 135.0, 145.0, 142.0]

    cfg = BacktestConfig(
        name="策略6",
        require_5m_gate=False,
        use_stop70=False,
        use_ema_cross=True,
        take_profit_pct=0.40,
        overbought_rsi=80.0,
    )
    res = run_rebound_backtest_24h(ohlcv, cfg, now_ts=base_ts + len(closes) * 60)

    assert res.strategy == "策略6"
    assert res.trades >= 1
    assert res.realized_pnl_sol > 0


def test_backtest_configs_include_strategy6_rules():
    names = [cfg.name for cfg in BACKTEST_CONFIGS]
    assert "策略6" in names

    cfg5 = next(cfg for cfg in BACKTEST_CONFIGS if cfg.name == "策略5")
    cfg6 = next(cfg for cfg in BACKTEST_CONFIGS if cfg.name == "策略6")
    assert cfg5.use_ema_cross is True
    assert cfg6.use_ema_cross is True
    assert cfg5.require_5m_gate is False
    assert cfg6.require_5m_gate is False
    assert cfg6.take_profit_pct == 0.40
    assert cfg6.overbought_rsi == 80.0
