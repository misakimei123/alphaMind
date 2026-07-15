# Freqtrade MVP Runtime Contract

## 1. 目的和适用范围

本文档冻结 alphaMind 第一阶段的实际运行链，解决长期逻辑架构与 Freqtrade MVP 之间的映射问题。本文档优先于架构文档中的长期自研接口描述；如果二者发生冲突，MVP 按本文档执行。

适用范围：

- 单一目标交易所；
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

`TradeIntent`、`RiskApprovedOrder` 和自研订单状态机在 MVP 中是逻辑模型，不是独立的可执行订单队列。只有自研执行系统按架构门槛单独获批后，才允许把它们实现为生产实体。

## 3. 长期概念到 Freqtrade 的映射

| 长期概念 | Freqtrade MVP 实现 | 所有者 | 审计证据 |
|---|---|---|---|
| `TradeIntent` | `populate_entry_trend`、`populate_exit_trend`、`enter_tag`、`exit_tag` | Freqtrade strategy | signal timestamp、pair、tag、strategy hash |
| 策略参数 | 固定 strategy/config 版本 | Freqtrade strategy | commit、config hash、Strategy Card |
| 风险定仓 | `custom_stake_amount` 加 Freqtrade wallet/config 上限 | Freqtrade strategy | NAV snapshot、预算风险、批准 stake、限制原因 |
| 入场审批 | `confirm_trade_entry` 只读取内存中的最新风险快照 | Freqtrade strategy | allow/reject、snapshot id、reason code |
| 硬止损 | `stoploss`；确需动态止损时使用 `custom_stoploss` | Freqtrade | stop price、order type、触发和成交事件 |
| 最大持仓时间 | exit signal 或轻量 callback | Freqtrade strategy | entry time、expiry、exit reason |
| 冷却和已实现交易回撤保护 | Freqtrade protections | Freqtrade | protection type、lookback、trigger、unlock time |
| 账户级日亏、周亏和现金流调整回撤 | 外部 risk watchdog | risk watchdog | RiskSnapshot、阈值、暂停事件 |
| `RiskApprovedOrder` | MVP 不作为独立下单实体；只保存风险审批审计事件 | Audit DB | signal id、stake、预算、reason code |
| Order Manager | Freqtrade 内部订单生命周期 | Freqtrade | Runtime DB、日志和交易所订单 |
| 对账与恢复 | Freqtrade Runtime DB、交易所查询和受控人工恢复 | Freqtrade operator | 差异、处置、人工操作和最终状态 |

`confirm_trade_entry` 位于下单关键路径，禁止在其中发起网络请求、数据库重查询或复杂计算。risk watchdog 先生成缓存快照；策略在 `bot_loop_start` 或等价的非关键位置加载快照，`confirm_trade_entry` 只进行常数时间判断。快照缺失、过期或解析失败时默认拒绝新入场。

风险公式先得到 base asset 的 `approved_quantity`；`custom_stake_amount` 按审批时的保守 entry price 转换为 quote stake，并再次受 Freqtrade 的 `min_stake`、`max_stake`、wallet 和配置限制。`confirm_trade_entry` 只能接受或拒绝已经计算出的 amount，不能在最后一步扩大 stake。

风险审批事件必须异步写入 Audit DB。下单关键 callback 不直接访问远程 Audit DB；它只生成带 `snapshot_id` 和 reason code 的有界本地事件，由后台审计写入器持久化。审计通道不可用或积压超过阈值时 fail-closed，停止新入场。

## 4. RiskSnapshot 合同

risk watchdog 生成版本化、原子替换的只读快照：

```yaml
schema_version: 1
snapshot_id: ""
generated_at_utc: ""
expires_at_utc: ""
account_id: ""
nav: 0
available_cash: 0
unrealized_pnl: 0
accrued_fees: 0
daily_pnl: 0
weekly_pnl: 0
drawdown_from_adjusted_hwm: 0
open_exposure: 0
pending_order_exposure: 0
entry_allowed: false
close_only: true
kill_switch: false
reason_codes: []
```

运行规则：

- 所有时间边界使用 UTC；
- `generated_at_utc` 超过预设新鲜度时禁止新入场；
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

初始阈值仍为待 Phase 0 确认的保守默认值：

| 规则 | 口径 | 动作 |
|---|---|---|
| 单笔 0.25% | 入场时计划风险/NAV | 降低或拒绝 stake |
| 单日亏损 1% | 含未实现盈亏和费用、扣除外部现金流 | 禁止新入场，保留安全退出 |
| 单周亏损 3% | 同上，以 UTC 周为边界 | 禁止新入场并进入人工复核 |
| 回撤 5% | 相对现金流调整后高水位 | Kill Switch，进入预定义安全处置 |

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
