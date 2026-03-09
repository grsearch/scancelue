# SOL 新币监控与策略信号服务

这是一个基于 FastAPI 的服务，支持：

- `POST /webhook/add-token`：接收龙虾AI扫链 webhook，收录白名单并开始 1 分钟监控。
- 从 CoinGecko Pro On-Chain API 拉取 SOL 新币数据（OHLCV + FDV/价格/池创建时间(pool_created_at)）。
- 按你定义的 `trend/range/down` 三态 + 策略切换二次确认机制输出 `BUY/ADD/SELL/HOLD`。
- 自动将交易信号通过 webhook 下发：
  - BUY/ADD -> `POST /webhook/new-token`
  - SELL -> `POST /force-sell`
- 白名单退出条件：
  - FDV < 20000
  - 池年龄 > 48h（以 pool_created_at 为准）
  - 若退出时仍有持仓，则先发送 SELL webhook，再移入黑名单。
- 提供 Dashboard：`GET /dashboard`

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

## 添加代币示例

```bash
curl -X POST http://127.0.0.1:3003/webhook/add-token \
-H "Content-Type: application/json" \
-d '{"network":"solana","address":"8jiVXftnn2ZG6bugK7HAH5j2G3D6TpsG521gqsWwpump","symbol":"AUTISM"}'
```
