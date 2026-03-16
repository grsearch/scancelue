# SOL 新币监控与策略信号服务

这是一个基于 FastAPI 的服务，支持：

- `POST /webhook/add-token`：接收龙虾AI扫链 webhook，收录白名单并开始 1 分钟K线监控。
- 从 Birdeye API 拉取 SOL 新币数据（OHLCV + FDV/价格/创建时间）。
- 执行实盘反弹策略（1分钟K线），输出 `BUY/SELL/HOLD`。
- 自动将交易信号通过 webhook 下发：
  - BUY -> `POST /webhook/new-token`
  - SELL -> `POST /force-sell`
- 白名单退出条件：
  - FDV < 20000
  - `AGE > 6h`
  - 若退出时仍有持仓，则先发送 SELL webhook，再移入黑名单。
- 提供 Dashboard：`GET /dashboard`（含当前盈亏与历史总盈亏）
- `GET /dashboard/backtest`（白名单代币过去24小时回测：反弹策略/反弹策略2）
- 说明：当前实现已采用“WebSocket 主链路 + REST 辅助”模式：Birdeye WebSocket 触发实时信号评估，Birdeye REST 用于启动预热、断线补K与对账。若日志出现 WS HTTP 403，请检查 `BIRDEYE_API_KEY` 与 `BIRDEYE_WS_HEADERS_JSON`。
- 白名单/黑名单与信号日志会持久化到本地文件，服务重启后自动恢复。

## 快速启动

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
uvicorn app.main:app --host 0.0.0.0 --port 3003
```

## 环境变量

- `BIRDEYE_API_KEY`：Birdeye API key（推荐配置）
- `BIRDEYE_BASE_URL`：默认 `https://public-api.birdeye.so`
- `BIRDEYE_WS_ENABLED`：是否启用 Birdeye WebSocket（`1` 启用，默认 `1`）
- `BIRDEYE_WS_URL`：Birdeye WebSocket 地址，默认 `wss://public-api.birdeye.so/socket/solana`
- `BIRDEYE_WS_HEADERS_JSON`：可选，WS额外请求头JSON（用于兼容Birdeye网关鉴权）
- `BIRDEYE_WS_SUBPROTOCOL`：WS子协议，默认 `echo-protocol`
- `BIRDEYE_WS_ORIGIN`：WS Origin 头，默认 `ws://public-api.birdeye.so`
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
- 卖出：`RSI 下穿 65/70/75` 或 `RSI >= 85` 或 `close <= 首仓价 * 0.90`


## 回测策略（24小时，统一使用1分钟K线）

- 反弹策略：
  - 买入：`RSI 上穿 30`
  - 卖出：`RSI 下穿 70/75` 或 `RSI >= 85`
- 反弹策略2：
  - 买入：`RSI 上穿 30`
  - 卖出：`RSI 下穿 65/70/75` 或 `RSI >= 85` 或 `close <= 首仓价 * 0.90`

说明：回测页面中的“交易次数”= **买入/加仓次数 + 卖出次数**。
