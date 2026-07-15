# ADR-0002：Bybit 国际版开发目标与 Capability Matrix

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-02 |
| 决策日期 | 2026-07-15 |
| 目标交易所 | Bybit 国际版 |
| 市场 | Spot |
| 交易对 | BTC/USDT、ETH/USDT |
| Freqtrade exchange id | `bybit` |

## 1. 决策

alphaMind 的通用实现和首轮验证固定使用 Bybit 国际版现货：

- `BTC/USDT`
- `ETH/USDT`
- long/flat
- 无杠杆、无 margin borrowing
- Freqtrade 是唯一交易写入者

常驻地区不参与策略、风险、交易对或 callback 的代码路径选择。实际 Live Canary 前必须重新核对届时的账户资格、服务条款、API 端点和部署位置；未通过时阻止 live，但不自动切换交易所或修改研究规则。

## 2. 选择依据

| 能力 | 结论 | 证据 |
|---|---|---|
| Freqtrade spot 支持 | 支持 | Freqtrade 官方 Supported Spot Exchanges 列出 Bybit |
| BTC/USDT spot | 支持 | Bybit V5 instrument endpoint 的 spot 示例返回 BTCUSDT |
| ETH/USDT spot | 支持，运行前动态查询 | Bybit V5 spot 与枚举文档覆盖 ETHUSDT；不得硬编码市场状态 |
| API 版本 | V5 | Bybit V5 统一 Spot/Derivatives/Options API |
| Market / Limit | 支持 | `POST /v5/order/create` |
| Post-only / IOC / FOK | 支持 | `timeInForce` 枚举与 place order |
| 自定义幂等标识 | 支持 | `orderLinkId`，最长 36 字符且要求唯一 |
| 单笔订单查询 | 支持 | realtime/history 可按 `orderId` 或 `orderLinkId` 查询 |
| Open/Closed Orders | 支持 | `/v5/order/realtime` 与 `/v5/order/history` |
| Fill/Execution | 支持 | `/v5/execution/list`；一张订单可有多次 execution |
| 私有订单 WebSocket | 支持 | `order.spot` 与 execution 私有 topic |
| 动态精度和最小金额 | 支持 | `/v5/market/instruments-info` 返回 tickSize、minOrderAmt、数量上限 |
| API Key 权限分离 | 支持 | SpotTrade、Wallet/Withdrawal 权限分离 |
| IP 绑定 | 支持 | API key info/modify 接口包含 `ips` |
| Testnet | 支持 | `api-testnet.bybit.com` |
| Demo Trading | 支持但能力不完整 | `api-demo.bybit.com`，官方声明并非全部 API 可用 |
| Freqtrade sandbox | 不支持 | Testnet 只进入独立 Contract Harness |
| Freqtrade Bybit spot on-exchange stoploss | **不支持** | Freqtrade exchange feature matrix 对 Bybit spot 标记为不支持 |

## 3. 订单与对账合同

Bybit 下单和撤单响应只是异步接受确认，不代表订单已成交或撤单已完成：

1. Freqtrade 提交订单；
2. 保存交易所 `orderId` 和可用的 `orderLinkId`；
3. 通过 Freqtrade/CCXT 的订单查询和私有事件确认终态；
4. partial fill 使用累计成交数量，不把取消视为零成交；
5. 请求超时后先按订单 ID、`orderLinkId`、open orders 和 executions 对账；
6. 无法唯一判断时停止新入场，不盲目重试。

历史保留边界：

- realtime 接口可查询当前订单和最近 closed 状态；
- order history 默认返回近 7 天，取消/拒绝等完整状态重点保留近 24 小时；
- 超过 7 天主要保留有成交的订单；
- alphaMind/Freqtrade 必须及时持久化订单和 execution，不把远端历史当永久审计库。

## 4. 精度、金额与限频

禁止硬编码文档示例中的数值：

- 启动时查询 `instruments-info`；
- 验证 `status=Trading`、base/quote、`tickSize`、`minOrderAmt`、数量步长和最大数量；
- 市场规则 hash 进入启动审计；
- 规则变化时拒绝使用旧缓存盲目下单。

当前官方限频模型：

- 默认 HTTP IP 上限为 5 秒 600 次；
- 交易接口还按 UID 和 endpoint 使用滚动每秒限频；
- spot create/cancel 当前表格值为 20 次/秒，order/execution 查询为 50 次/秒；
- 实现必须读取 `X-Bapi-Limit-*` 响应头并保留安全余量，不能贴着上限运行。

## 5. API Key 安全基线

开发、Contract Harness 和 Live 使用不同 key：

- 只授予 SpotTrade 和必要只读权限；
- 不授予 Withdrawal；
- 绑定固定 IP；
- 不把 key 写入 Git、日志或 Audit DB；
- Testnet key 不能用于 mainnet；
- Live key 只在 Live Canary 审批后创建；
- 发现异常立即撤销 key，并保持 Freqtrade 停止新入场。

## 6. Stoploss 残余风险

Bybit V5 API 本身支持 spot TP/SL，但 Freqtrade 当前不支持 Bybit spot 的 `stoploss_on_exchange`。MVP 决策：

- 使用 Freqtrade bot-managed `stoploss` / `custom_stoploss`；
- 不增加直接调用 Bybit TP/SL 的第二交易写路径；
- 进程、主机或网络离线期间没有交易所托管止损；
- Phase 3 必须完成进程守护、心跳、数据新鲜度和离线告警；
- Live Canary 前由项目所有人显式接受该残余风险，否则停止上线；
- 如果该风险不可接受，只能通过新的架构决策更换交易所或重新立项执行适配，不能在 strategy callback 中旁路下单。

## 7. 验证分层

| 环境 | Bybit 用途 | 不能证明 |
|---|---|---|
| Historical Backtest | Bybit 现货 OHLCV 与规则验证 | API 写路径 |
| Freqtrade Dry-run | 主网公开行情上的模拟运行 | Bybit 真实接单 |
| Replay/Fault Injection | alphaMind 风险与恢复逻辑 | Bybit 真实参数 |
| Bybit Testnet Contract Harness | key、签名、精度、下单/查询/撤单合同 | 主网流动性与生产资格 |
| Live Canary | 小额主网生产路径 | 长期 alpha |

Bybit Demo Trading 可用于补充 API 体验，但因官方明确声明能力不完整，不能替代 Testnet Contract Harness。

## 8. 未决但不阻塞 P0-02 的事项

以下内容转入后续任务：

- P0-03：锁定 Freqtrade、CCXT、Python 和镜像 digest；
- P0-05：冻结 Bybit 历史数据起止日期和 hash；
- P0-06：固定 bot-managed stoploss、NAV 和 Kill Switch；
- P3-06：使用 Bybit Testnet 验证锁定版本的真实 API 合同；
- Live Canary gate：重新核对账户资格、服务条款、部署端点和 key 权限。

## 9. 官方证据

- [Freqtrade Supported Exchanges](https://docs.freqtrade.io/en/stable/)
- [Freqtrade Exchange-specific Notes](https://docs.freqtrade.io/en/stable/exchanges/)
- [Bybit V5 Integration Guidance](https://bybit-exchange.github.io/docs/v5/guide)
- [Bybit Instruments Info](https://bybit-exchange.github.io/docs/v5/market/instrument)
- [Bybit Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order)
- [Bybit Cancel Order](https://bybit-exchange.github.io/docs/v5/order/cancel-order)
- [Bybit Open Orders](https://bybit-exchange.github.io/docs/v5/order/open-order)
- [Bybit Order History](https://bybit-exchange.github.io/docs/v5/order/order-list)
- [Bybit Execution History](https://bybit-exchange.github.io/docs/v5/order/execution)
- [Bybit Private Order Stream](https://bybit-exchange.github.io/docs/v5/websocket/private/order)
- [Bybit API Rate Limits](https://bybit-exchange.github.io/docs/v5/rate-limit)
- [Bybit Demo Trading](https://bybit-exchange.github.io/docs/v5/demo)

## 10. 结论

P0-02 状态为 `DONE`。Bybit 国际版是当前唯一开发目标，BTC/USDT 与 ETH/USDT 是首批通用现货交易对。下一步进入 P0-03，锁定运行版本并用无密钥公开接口验证 Freqtrade/CCXT 对 Bybit spot 的实际识别结果。
