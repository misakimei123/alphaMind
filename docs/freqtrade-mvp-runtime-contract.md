# Freqtrade Runtime Contract（AI MVP）

> 2026-07-18 修订：本文最初只覆盖 BTC/ETH 现货 Donchian。当前规范已扩展为 AI Action、Telegram 人工授权、配置化 Instrument Registry、Bybit Spot/Futures 双实例和 ExecutionGateway。旧现货专用段落作为已实现 v1 底座保留；与 [完整开发计划](development-plan.md) 冲突时，以开发计划为准。

## 0. 当前修订范围

```text
Scheduler + Account/Market Snapshot + News
  -> DecisionContext
  -> LLM Action
  -> Schema/Deterministic Risk Validation
  -> Telegram Approval
  -> Execution Revalidation
  -> ExecutionGateway
  -> Freqtrade Spot / Freqtrade Futures
  -> Bybit
```

当前运行合同：

- 默认 30 分钟 AI 决策周期，可配置且不可重叠；
- 初始配置 BTC、ETH、SOL、HYPE，业务代码不得固定币种；
- Bybit spot 与 USDT linear perpetual；Spot/Futures 配置、Runtime DB、bot identity 和 secret 隔离；
- 现货 long，合约按配置支持 long/short、isolated、one-way 和最大杠杆；
- Proposal/Action Store 保存候选与审批状态，但不是订单或持仓权威；
- Telegram 批准构成普通 AI 动作授权，批准后必须重新校验；
- ExecutionGateway 只驱动 Freqtrade，不直接调用 Bybit 写接口；
- Freqtrade 仍是唯一允许向 Bybit 创建、修改或取消订单的运行组件；
- RiskSnapshot v2 覆盖 spot/futures、挂单、mark、liq、funding、保证金和名义敞口；
- futures 优先使用 stoploss on exchange；spot 继续显式管理 bot-managed stoploss 残余风险。

旧文档中的固定 4h、BTC/ETH、现货 long/flat、无杠杆和 Donchian 唯一信号，只描述 v1 历史实现，不再限制 R0-R6。

## 1. 目的和适用范围

本文档描述 alphaMind 与 Freqtrade 的运行所有权和风险边界。任务、范围和进度由 [完整开发计划](development-plan.md) 唯一收口；本文不得反向恢复已被取代的旧范围。

以下是已实现 v1 现货底座范围，继续作为兼容和迁移输入：

- Bybit 国际版单一目标交易所；
- BTC/USDT、ETH/USDT 现货；
- long/flat，不使用杠杆；
- 4h 简单趋势策略；
- Freqtrade backtest、dry-run 和初始 Live Canary；
- 不包含自研 Order Manager、多账户、多腿订单和衍生品。

## 2. 单一运行所有权

MVP 必须遵守以下单一所有权规则：

1. Freqtrade 是唯一允许向交易所创建、取消和查询交易订单的进程；
2. 不启动第二个拥有交易写权限的 alphaMind Order Manager；
3. Freqtrade Runtime DB 是 `Trade`、`Order`、open position 和运行恢复状态的唯一内部权威；
4. alphaMind Research/Audit DB 只能保存研究和审计事件，不能修改或重建 Freqtrade 运行状态；
5. 外部 risk watchdog 只允许读取账户与运行状态、发布风险快照和暂停新入场，不允许直接下单；
6. 交易所是实盘余额、订单和成交的操作事实来源；发现差异时停止新入场并按 Freqtrade 支持的恢复流程处理。

`DecisionContext`、`Action`、审批状态和执行映射可以持久化，但不成为交易所订单/持仓权威。ExecutionGateway 是获批 Action 到 Freqtrade 的受控适配器，不是自研 Order Manager；只有 Freqtrade 可以持有 Bybit 写权限。

## 3. 长期概念到 Freqtrade 的映射

| 长期概念 | Freqtrade MVP 实现 | 所有者 | 审计证据 |
|---|---|---|---|
| `TradeIntent` | `populate_entry_trend`、`populate_exit_trend`、`enter_tag`、`exit_tag` | Freqtrade strategy | signal timestamp、pair、tag、strategy hash |
| 策略参数 | 固定 strategy/config 版本 | Freqtrade strategy | commit、config hash、Strategy Card |
| 风险定仓 | `custom_stake_amount` 加 Freqtrade wallet/config 上限 | Freqtrade strategy | NAV snapshot、预算风险、批准 stake、限制原因 |
| 入场审批 | `confirm_trade_entry` 只读取内存中的最新风险快照 | Freqtrade strategy | allow/reject、snapshot id、reason code |
| 硬止损 | `stoploss`；确需动态止损时使用 `custom_stoploss`。Freqtrade 当前不支持 Bybit spot 的 `stoploss_on_exchange`，MVP 使用 bot-managed stoploss | Freqtrade | stop price、触发、提交和成交事件；进程/网络中断暴露单独审计 |
| 最大持仓时间 | exit signal 或轻量 callback | Freqtrade strategy | entry time、expiry、exit reason |
| 冷却和已实现交易回撤保护 | Freqtrade protections | Freqtrade | protection type、lookback、trigger、unlock time |
| 账户级日亏、周亏和现金流调整回撤 | 外部 risk watchdog | risk watchdog | RiskSnapshot、阈值、暂停事件 |
| `RiskApprovedOrder` | MVP 不作为独立下单实体；只保存风险审批审计事件 | Audit DB | signal id、stake、预算、reason code |
| Order Manager | Freqtrade 内部订单生命周期 | Freqtrade | Runtime DB、日志和交易所订单 |
| 对账与恢复 | Freqtrade Runtime DB、交易所查询和受控人工恢复 | Freqtrade operator | 差异、处置、人工操作和最终状态 |

AI MVP 在该 v1 映射上新增：

| 当前概念 | Freqtrade AI MVP 实现 | 所有者 | 审计证据 |
|---|---|---|---|
| `DecisionContext` | 账户/市场/新闻/风险的周期快照 | alphaMind scheduler | cycle id、input hash、freshness |
| `Action` | 严格 Schema 的候选交易动作 | AI decision layer | model、prompt、action payload、news refs |
| 人工授权 | Telegram 白名单 + nonce + TTL + 幂等状态机 | approval service | user/chat、decision、time、reason |
| 执行前复核 | 最新价格、仓位、挂单、余额、风险和市场规则检查 | ExecutionGateway | action id、recheck snapshot、allow/reject |
| OPEN/CLOSE | Freqtrade force entry/exit 或批准信号适配器 | Freqtrade | action id、trade id、order id |
| ADD/REDUCE | 批准 Action 驱动 `adjust_trade_position` 或等价 RPC | Freqtrade | adjustment id、stake/amount、remaining position |
| 合约杠杆 | strategy `leverage()`，受三层上限裁剪 | Freqtrade + risk | requested/effective/max leverage |
| Spot/Futures runtime | 两个隔离 Freqtrade 实例与 Runtime DB | Freqtrade | bot identity、db identity、market type |

`confirm_trade_entry` 位于下单关键路径，禁止在其中发起网络请求、数据库重查询或复杂计算。risk watchdog 先生成缓存快照；策略在 `bot_loop_start` 或等价的非关键位置加载快照，`confirm_trade_entry` 只进行常数时间判断。快照缺失、过期或解析失败时默认拒绝新入场。

Bybit V5 API 本身提供 spot TP/SL 订单，但当前 Freqtrade Bybit spot 适配不支持 `stoploss_on_exchange`。MVP 不通过第二写路径直接调用 Bybit TP/SL API。Live Canary 前必须验证 bot-managed stoploss、进程守护和告警，并由项目所有人明确接受“bot 或网络离线期间没有交易所托管止损”的残余风险；否则不得上线。

风险公式先得到 base asset 的 `approved_quantity`；`custom_stake_amount` 按审批时的保守 entry price 转换为 quote stake，并再次受 Freqtrade 的 `min_stake`、`max_stake`、wallet 和配置限制。`confirm_trade_entry` 只能接受或拒绝已经计算出的 amount，不能在最后一步扩大 stake。

风险审批事件必须异步写入 Audit DB。下单关键 callback 不直接访问远程 Audit DB；它只生成带 `snapshot_id` 和 reason code 的有界本地事件，由后台审计写入器持久化。审计通道不可用或积压超过阈值时 fail-closed，停止新入场。

## 4. RiskSnapshot 合同

risk watchdog 生成版本化、原子替换的只读快照。规范性字段和状态约束以
[`risk-snapshot.schema.yaml`](../data/schemas/risk-snapshot.schema.yaml) 为准，会计与操作语义以
[ADR-0006](decisions/0006-risk-accounting.md) 和
[Kill Switch runbook](runbooks/kill-switch.md) 为准。以下为非完整结构摘录（金额使用十进制字符串）：

```yaml
schema_version: 1
snapshot_id: ""
generated_at_utc: ""
expires_at_utc: ""
account_id: ""
accounting_currency: USDT
accounting:
  nav: "500"
  daily_pnl: "0"
  weekly_pnl: "0"
  cashflow_adjusted_high_water_mark: "500"
  drawdown_fraction: "0"
exposure:
  open_exposure_quote: "0"
  pending_entry_exposure_quote: "0"
decision:
  state: ENTRY_ALLOWED
  entry_allowed: true
  close_only: false
  kill_switch: false
  safe_exit_allowed: true
  reason_codes: [risk_checks_passed]
```

运行规则：

- 所有时间边界使用 UTC；
- 目标每 15 秒发布，快照 TTL 为 60 秒，账户和行情源在生成时不得超过 30 秒；
- 快照缺失、陈旧、损坏、版本不支持或时钟异常时禁止新入场；
- `entry_allowed=false` 只能阻止新风险，不能阻止止损、安全退出和撤销未成交入场单；
- `kill_switch=true` 进入 `CLOSE_ONLY` 或人工处置状态，具体退出方式必须由 runbook 预先定义；
- watchdog 不直接调用交易所写接口；
- 快照发布失败时保持 fail-closed，不沿用无限期旧快照。

## 5. 风险和会计口径

### 5.1 NAV 与损益

现货 MVP 使用：

```text
NAV(t) = quote_cash
       + sum(base_quantity_i * mark_price_i)
       - accrued_fees
       - known_liabilities

period_pnl = NAV(end)
           - NAV(start)
           - net_external_cash_flow

drawdown = 1 - NAV(t) / cashflow_adjusted_high_water_mark

project_absolute_loss = max(-cashflow_adjusted_cumulative_pnl, 0)
fraction_loss_limit = cashflow_adjusted_capital_baseline * maximum_absolute_loss_fraction
effective_absolute_loss_limit = min(fraction_loss_limit, maximum_absolute_loss)
```

要求：

- 未实现盈亏、已实现盈亏和手续费都进入 NAV；
- 日、周边界统一使用 UTC；
- 充值、提币、返佣、奖励和非交易资产变化作为外部现金流单独记录，不能计为策略收益；
- 高水位必须按外部现金流调整；
- mark price 的来源、缺失和陈旧处理在 Phase 0 固定；现货 MVP 默认使用目标交易所可交易中间价或保守可退出价。

冻结 Paper 和 Live Canary 窗口内原则上禁止主动充值、提币或跨账户划转。确有必要时必须记录现金流，并按以下规则调整高水位：

```text
high_water_mark_after_flow = high_water_mark_before_flow + net_external_cash_flow
high_water_mark = max(high_water_mark_after_flow, NAV_after_flow)
```

无法解释或未预注册的资金流会使当前评估窗口失效。

### 5.2 单笔预算风险

`0.25% NAV` 表示入场时的计划风险预算，不是最大实际亏损保证：

```text
risk_cash = NAV * risk_fraction

estimated_unit_loss = abs(entry_price - stop_price)
                    + fee_buffer_per_unit
                    + slippage_buffer_per_unit
                    + gap_buffer_per_unit

quantity_by_risk = risk_cash / estimated_unit_loss

approved_quantity = floor_to_exchange_step(min(
    quantity_by_risk,
    volatility_cap,
    symbol_exposure_cap,
    directional_exposure_cap,
    available_balance_cap
))
```

批准前还必须计入未成交订单暴露、最小名义金额和价格/数量精度。跳空、网络中断、stop-limit 未成交和极端滑点可能使实际亏损超过预算，报告中必须将该差异记为 execution loss，而不能声称 0.25% 是损失上限。

### 5.3 熔断语义

P0-06 冻结的保守阈值为：

| 规则 | 口径 | 动作 |
|---|---|---|
| 单笔 0.25% | 入场时计划风险/NAV | 降低或拒绝 stake |
| 单日亏损 1% | 含未实现盈亏和费用、扣除外部现金流 | 禁止新入场，保留安全退出 |
| 单周亏损 3% | 同上，以 UTC 周为边界 | 禁止新入场并进入人工复核 |
| 回撤 5% | 相对现金流调整后高水位 | Kill Switch，进入预定义安全处置 |
| 项目绝对损失 10% / 45 USDT | 从现金流调整资本基线计算比例金额，再与固定金额取更严格者 | 达到任一配置边界时 Kill Switch；上调必须重新审批 |

## 6. 数据库和恢复所有权

### 6.1 Freqtrade Runtime DB

唯一保存和恢复：

- Freqtrade `Trade`；
- Freqtrade `Order`；
- open position；
- filled/cancelled order；
- bot 重启所需运行状态。

dry-run 和 live 必须使用独立数据库；多实例必须使用独立数据库、用户或 schema。Runtime DB 的 schema migration 由所锁定的 Freqtrade 版本负责，alphaMind 不直接修改其表结构。

### 6.2 alphaMind Research/Audit DB

保存：

- hypothesis、experiment 和 data manifest；
- strategy/config/environment hash；
- RiskSnapshot 和风险审批审计事件；
- signal tag、人工干预、告警和 review result；
- 指向 Freqtrade `trade_id`、exchange order id 的只读关联。

Audit DB 不能生成订单、覆盖 Freqtrade Trade/Order 状态，或在启动时反向恢复 Freqtrade。运行恢复顺序是：交易所和 Freqtrade Runtime DB 先恢复并完成处置，Audit DB 后补审计事件。

详细所有权、只读路径、outbox、writer 和 Replay 合同以
[ADR-0007](decisions/0007-audit-and-replay.md)、
[`audit-event.schema.yaml`](../data/schemas/audit-event.schema.yaml) 与
[`experiment.schema.yaml`](../data/schemas/experiment.schema.yaml) 为准。

callback 只向独立 SQLite WAL outbox 追加事件，不执行远程 DB/API 请求。outbox 固定 10,000
pending/256 MiB 硬容量；达到 8,000 pending、最老事件 5 分钟或 192 MiB 中任一条件时，
新入场 fail-closed，并保留最后 2,000 个逻辑槽位给 exit、Kill、reconcile 和 operator 事件。
Audit Writer 以 `event_id` 和 content hash 幂等写入；Audit DB 的 Runtime 引用始终为只读，
不得通过外键、trigger 或恢复脚本修改 Freqtrade 状态。

### 6.3 Replay 边界

Replay 进程不持有生产 API Key、Runtime/Audit 生产凭据或交易写权限。partial fill、submit
unknown 和重复事件使用冻结 fixture、fake adapter 与锁定版本 Freqtrade integration 验证；
Replay 只能证明 alphaMind 风险、审计、适配和运维处置逻辑，不能成为生产订单状态权威，也
不能声称真实交易所写路径已经通过。

## 7. 验证证据分层

| 层级 | 证明内容 | 明确不能证明 |
|---|---|---|
| Historical Backtest | 规则、point-in-time、历史成本和样本外统计 | 真实 API、订单和成交 |
| Freqtrade Dry-run | 实时信号、运行配置和模拟订单 | 交易所写权限、真实接单和 partial fill |
| Deterministic Replay/Fault Injection | 超时、重复事件、partial fill、撤单竞争和恢复逻辑 | 交易所真实参数与生产端点行为 |
| 独立 Testnet Contract Harness | 目标交易所支持时验证 API Key、参数、精度、订单和查询契约 | 生产流动性和策略收益；Freqtrade 本身不运行 sandbox account |
| Live Canary | 生产写路径、真实费用、滑点和运维 | 长期 alpha 和扩容安全性 |

如果交易所没有可信 Testnet、测试下单端点或可接受的最小额 contract test，Live Canary 将是第一次完整验证生产写路径。该风险必须在上线审批中显式接受，不能用 Shadow Execution 声称已经验证。

## 8. 交易所 Capability Matrix

Phase 0 必须对目标交易所逐项记录：

```yaml
exchange: ""
jurisdiction_available: false
freqtrade_supported: false
spot_symbols: []
api_key_trade_without_withdrawal: false
ip_allowlist: false
testnet_or_test_order_endpoint: false
min_notional: ""
price_precision: ""
amount_precision: ""
market_order: false
limit_order: false
post_only: false
stop_market: false
stop_limit: false
client_order_id: false
fetch_order: false
fetch_open_orders: false
fetch_closed_orders: false
fetch_my_trades: false
order_history_retention: ""
rate_limit_model: ""
websocket_private_orders: false
manual_trade_detection: false
fee_currency_rules: ""
```

缺少 `client_order_id`、单笔订单查询或历史订单时，必须记录替代对账方案和无法消除的歧义。自研执行系统优先把可靠幂等与查询能力作为交易所准入要求；无法唯一判断写请求结果时进入人工停机，不允许盲目重试。

Bybit 开发目标的已核查 capability matrix 见 [ADR-0002](decisions/0002-exchange-selection.md) 与 [机器可读清单](../configs/common/exchange-capabilities.yaml)。动态精度、最小金额和限频不得从文档示例硬编码，必须在运行时从 Bybit V5 instrument/rate-limit 响应读取或在锁定版本的配置中生成。

对账必须识别外部人工交易、充值提币、奖励、返佣、手续费币种变化和交易所自动行为。无法分类的余额变化进入 `HALTED_MANUAL_REVIEW`，不能自动计入策略 PnL。

## 9. 自研执行系统触发条件

只有同时满足以下条件，才允许替换本合同：

1. 已证明 Freqtrade 无法满足具体且当前需要的能力；
2. capability matrix 和故障场景已经完成；
3. 新系统定义 `TradeIntent`、`RiskApprovedOrder`、订单和持仓的单一所有权；
4. 已完成同源信号测试、事件回放、故障注入和并行 shadow；
5. 新系统重新通过完整 Paper 门槛；
6. 切换时不存在 Freqtrade 与新 Order Manager 同时拥有交易写权限的窗口。

## 10. 参考资料

- [Freqtrade Strategy Callbacks](https://www.freqtrade.io/en/stable/strategy-callbacks/)
- [Freqtrade Protections](https://docs.freqtrade.io/en/stable/plugins/)
- [Freqtrade FAQ：Sandbox Accounts](https://www.freqtrade.io/en/stable/faq/)
- [Freqtrade Database Setup](https://docs.freqtrade.io/en/stable/advanced-setup/)
- [CCXT Manual](https://github.com/ccxt/ccxt/wiki/manual)
