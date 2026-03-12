# SOL 新币监控与策略信号服务

这是一个基于 FastAPI 的服务，支持：

- `POST /webhook/add-token`：接收龙虾AI扫链 webhook，收录白名单并开始 1 分钟监控。
- 从 CoinGecko Pro On-Chain API 拉取 SOL 新币数据（OHLCV + FDV/价格/池创建时间(pool_created_at)）。
- 执行双策略：1分钟反弹策略 + 5分钟启动策略（均使用 RSI=9, EMA9, EMA20），并输出 `BUY/ADD/SELL/HOLD`。
- 自动将交易信号通过 webhook 下发：
  - BUY -> `POST /webhook/new-token`
  - SELL -> `POST /force-sell`
- 白名单退出条件：
  - FDV < 20000
  - 池年龄退出机制：已暂停
  - 若退出时仍有持仓，则先发送 SELL webhook，再移入黑名单。
- 提供 Dashboard：`GET /dashboard`（含当前盈亏与历史总盈亏）
- `GET /dashboard/backtest`（白名单代币过去24小时回测：反弹策略 + 趋势策略）
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

## 添加代币示例

```bash
curl -X POST http://127.0.0.1:3003/webhook/add-token \
-H "Content-Type: application/json" \
-d '{"network":"solana","address":"8jiVXftnn2ZG6bugK7HAH5j2G3D6TpsG521gqsWwpump","symbol":"AUTISM"}'
```

## 当前实盘策略

### 反弹策略（1分钟，唯一执行策略）
- 开单前过滤（5m）：`EMA9 > EMA20`，否则禁止开新单/加仓。
- 买入：`RSI 上穿 30`。
- 加仓（仅一次）：在已有反弹仓位下，`RSI 再次上穿 30` 且 `close <= 首仓价 * 0.90`。
- 卖出：`RSI 下穿 65` 或 `RSI 下穿 70` 或 `RSI 下穿 75` 或 `RSI >= 85`。


## 回测策略（24小时）

- 反弹策略：
  - 买入：`RSI 上穿 30`
  - 加仓（仅一次）：`RSI 再次上穿 30` 且 `close <= 首仓价 * 0.90`
  - 卖出：`RSI 下穿 65/70/75` 或 `RSI >= 85` 或 `close <= 首仓价 * 0.70`
- 趋势策略：
  - 买入：`EMA9 上穿 EMA20` 后 3 根K线内，且 `close > EMA9`、`close <= EMA9*1.1`、`RSI < 75`
  - 卖出：
    - `EMA9 下穿 EMA20` 后进入警戒状态
    - 警戒状态下 `close <= 首仓价 * 0.80` 卖出
    - 或 `RSI 下穿 70` 卖出
    - 或 `RSI >= 80` 卖出
