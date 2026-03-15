# SOL 新币监控与策略信号服务

这是一个基于 FastAPI 的服务，支持：

- `POST /webhook/add-token`：接收龙虾AI扫链 webhook，收录白名单并开始 1 分钟K线监控。
- 从 CoinGecko Pro On-Chain API 拉取 SOL 新币数据（OHLCV + FDV/价格/池创建时间(pool_created_at)）。
- 执行实盘反弹策略（1分钟K线），输出 `BUY/SELL/HOLD`。
- 自动将交易信号通过 webhook 下发：
  - BUY -> `POST /webhook/new-token`
  - SELL -> `POST /force-sell`
- 白名单退出条件：
  - FDV < 20000
  - `AGE > 6h`
  - 若退出时仍有持仓，则先发送 SELL webhook，再移入黑名单。
- 提供 Dashboard：`GET /dashboard`（含当前盈亏与历史总盈亏）
- `GET /dashboard/backtest`（白名单代币过去24小时回测：反弹策略/趋势策略）
- 白名单/黑名单与信号日志会持久化到本地文件，服务重启后自动恢复。

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --host 0.0.0.0 --port 3003
```

## 环境变量

- `COINGECKO_API_KEY`：CoinGecko Pro API key（推荐配置）
- `COINGECKO_BASE_URL`：默认 `https://pro-api.coingecko.com/api/v3/onchain`
- `BUY_WEBHOOK_URL`：默认 `http://43.162.102.148:3002/webhook/new-token`
- `SELL_WEBHOOK_URL`：默认 `http://43.162.102.148:3002/force-sell`
- `STATE_FILE`：持久化状态文件路径，默认 `data/state.json`
- `MONITOR_TICK_BUFFER_SECONDS`：1分钟边界后的缓冲秒数，默认 `5`（轮询对齐到 `xx:01:05 / xx:02:05 ...`）

## 添加代币示例

```bash
curl -X POST http://127.0.0.1:3003/webhook/add-token \
-H "Content-Type: application/json" \
-d '{"network":"solana","address":"8jiVXftnn2ZG6bugK7HAH5j2G3D6TpsG521gqsWwpump","symbol":"AUTISM"}'
```

## 当前实盘策略

### 实盘策略（仅反弹策略）
- 买入：`RSI 上穿 30`
- 卖出：`RSI 下穿 70/75` 或 `RSI >= 85`


## 回测策略（24小时，统一使用1分钟K线）

- 反弹策略：
  - 买入：`RSI 上穿 30`
  - 卖出：`RSI 下穿 70/75` 或 `RSI >= 85`
- 趋势策略：
  - 买入：
    - `EMA9 > EMA20`
    - `EMA20_now > EMA20_3bars_ago`
    - `close > VWAP`
    - 最近3根内发生过：`EMA9 上穿 EMA20` 或突破最近5根高点
    - 随后出现1~3根回踩：`low >= EMA20 * 0.995` 且回踩成交量低于启动K
    - 当前触发：`close > high[1]` 且 `close > EMA9` 且 `RSI(9) > 55` 且 `RSI(9) < 78`
  - 卖出：`close <= entry * 0.955` 或 `close < EMA20` 或 `EMA9 下穿 EMA20` 或 `RSI(9) 从 >75 跌回 <70`

说明：回测页面中的“交易次数”= **买入/加仓次数 + 卖出次数**。
