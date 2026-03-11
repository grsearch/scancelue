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
  - 池年龄 > 48h（以 pool_created_at 为准）
  - 若退出时仍有持仓，则先发送 SELL webhook，再移入黑名单。
- 提供 Dashboard：`GET /dashboard`
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

### 反弹策略（1分钟）
- 开单前过滤（5m）：`EMA9 > EMA20`，否则禁止反弹策略开新单。
- 买入：`RSI 上穿 30`。
- 加仓（仅一次）：在已有反弹仓位下，且满足 5m 开单条件，`RSI 再次上穿 30` 且 `close <= 首仓价 * 0.90`。
- 卖出：`RSI 下穿 65` 或 `RSI 下穿 75` 或 `RSI >= 85` 或 `5m close < EMA20`。

### 启动策略（5分钟）
- 买入：当出现 `ema_cross_up = (ema9_prev <= ema20_prev and ema9_now > ema20_now)` 后，在**3根5m K线内**，只要仍满足 `EMA9 > EMA20`、`close > EMA9`、`close <= EMA9 * 1.05`，即可触发买入。
- 卖出：`EMA9 下穿 EMA20` 或 `close < EMA9`。
- 说明：启动策略基于5分钟执行，不受反弹策略的5m开单门控限制；同一根5分钟上穿信号仅触发一次买入，避免重复开单。

