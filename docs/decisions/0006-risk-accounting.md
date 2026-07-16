# ADR-0006：冻结风险会计与 Kill Switch 合同

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-06 |
| 制定日期 | 2026-07-16 |
| 独立评审 | 项目所有人 misakimei123，2026-07-16 |
| 评审基线 | `main@52f1ae8` |
| 评审结论 | 作为 P0-08 Scope Frozen 批准的一部分，无修改接受 |
| 前置决策 | ADR-0001、ADR-0002、ADR-0003、ADR-0005 |
| 配置基线 | `configs/common/risk-limits.toml` |
| 机器可读合同 | `data/schemas/risk-snapshot.schema.yaml` |
| 操作手册 | `docs/runbooks/kill-switch.md` |

## 1. 决策范围

本文冻结 Freqtrade MVP 的账户级风险会计、RiskSnapshot 和停止动作。它不授权启动
watchdog、接入认证交易所 API、创建交易所写路径或运行 Paper/Live；对应实现分别受
P3-01、P3-02 和后续阶段门禁约束。

MVP 保持单一写入者：只有 Freqtrade 可以创建、取消和退出交易订单。risk watchdog
只读账户、运行状态与公开行情，生成原子替换的 RiskSnapshot；它不得直接下单。

## 2. 会计币种、资产范围与 NAV

唯一会计币种为 USDT。现货 long/flat 的 NAV 定义为：

```text
marked_position_value_i = base_quantity_i * conservative_exit_mark_i

NAV(t) = quote_cash
       + sum(marked_position_value_i)
       - accrued_fees
       - known_liabilities
```

会计规则：

- `quote_cash` 只包含经对账的可用与冻结 USDT；未成交入场单锁定的 USDT 仍属于现金，
  同时计入 `pending_entry_exposure`，不得重复扣减 NAV；
- `base_quantity` 只包含经 Freqtrade Runtime DB 与交易所对账的 BTC/ETH 现货数量；
- 已实现损益通过现金和持仓变化进入 NAV，未实现损益通过 mark price 进入 NAV；
- 已发生但尚未从余额扣除的手续费计入 `accrued_fees`；借款、负余额和其他已知应付款计入
  `known_liabilities`；MVP 禁止杠杆，因此任何非零借款同时触发人工复核；
- 奖励、返佣、空投、人工交易、充值、提币和跨账户划转不得自动当作策略收益；
- 未知资产、未知负债、币种不一致或无法对账的余额差异触发
  `unexplained_balance_difference`，进入 KILLED_MANUAL_REVIEW；
- 所有金额使用精确十进制字符串传输和落盘，禁止先经过二进制浮点再参与风险判断。

## 3. Mark price

BTC/USDT 与 ETH/USDT 的 `conservative_exit_mark` 固定为同一交易所现货市场上、同一观测
时点附近的 `min(best_bid, last_trade)`。两项行情都必须为正数，并且在生成快照时不超过
30 秒；任一缺失、陈旧、市场类型不符或时间戳来自未来超过 5 秒，快照不得给出
`entry_allowed=true`。

这一价格只用于风险会计，不是成交保证。不得使用 ask、mid price、其他交易所价格、未完成
candle 的 OHLC 或成本价替代。P3-01 可以在不改变语义的前提下为盘口深度增加更保守的
退出滑点扣减；任何可能提高 NAV 的替代规则必须新建决策并重新评审。

## 4. 外部现金流、周期 PnL 与高水位

外部现金流使用账户视角：充值和流入为正，提币和流出为负。已识别外部现金流不计入策略
PnL：

```text
period_pnl = NAV(end) - NAV(start) - net_external_cash_flow

cashflow_adjusted_cumulative_pnl =
    NAV(t) - approved_capital_baseline - cumulative_net_external_cash_flow

adjusted_hwm_after_flow = adjusted_hwm_before_flow + net_external_cash_flow
cashflow_adjusted_hwm = max(adjusted_hwm_after_flow, NAV_after_flow)

drawdown = max(1 - NAV(t) / cashflow_adjusted_hwm, 0)
```

日边界为每天 `00:00:00 UTC`，周边界为 ISO 周一 `00:00:00 UTC`。边界快照缺失时不得用
第一笔晚到观测静默替代，必须保持 fail-closed 并人工补齐可复核证据。

Paper 和 Live Canary 的冻结评估窗口原则上禁止主动现金流。窗口内发生任何现金流时，先记录
唯一事件、金额、币种、时间和原因，再进入 `CLOSE_ONLY` 人工复核；无法分类或金额无法对账
时升级为 `KILLED_MANUAL_REVIEW`。因此现金流不会自动扩大当日、当周或项目损失预算。

## 5. 风险阈值

以下值继承 ADR-0001 的项目所有人批准和 `risk-limits.toml`，本决策不提高任何边界：

| 规则 | 精确口径 | 达到边界时动作 |
|---|---|---|
| 单笔计划风险 0.25% | 入场 NAV × `0.0025` | 缩小 stake；不足最小数量/名义金额时拒绝 |
| 单日亏损 1% | `max(-daily_pnl, 0) / daily_opening_nav` | `CLOSE_ONLY`，到下一 UTC 日仅在全部检查恢复后自动解除 |
| 单周亏损 3% | `max(-weekly_pnl, 0) / weekly_opening_nav` | `CLOSE_ONLY`，必须人工复核后解除 |
| 回撤 5% | 相对现金流调整高水位 | Kill Switch，人工决定安全处置 |
| 项目绝对损失 | `min(approved_baseline × 0.10, 45 USDT)` | 达到任一边界即 Kill Switch |

日/周 opening NAV 必须为对应 UTC 边界的已对账 NAV，且大于零。外部现金流、边界缺失或
币种不一致期间不允许自动使用比例门槛。`0.25%` 是计划风险预算，不是最大实际亏损保证；
跳空、手续费、滑点、网络中断和 bot-managed stoploss 失效均可能造成更大实际损失。

## 6. 风险状态与优先级

RiskSnapshot 只发布三种状态，按下表从上到下取最高优先级：

| 状态 | 输入条件 | 输出 | 必须动作 |
|---|---|---|---|
| `KILLED_MANUAL_REVIEW` | 回撤或项目绝对损失达界；未知资金差异；账户币种错误；未知负债；会计状态无法唯一恢复 | `entry_allowed=false`、`close_only=true`、`kill_switch=true` | 撤销未成交入场，保留止损/退出，按 runbook 人工决定持仓处置 |
| `CLOSE_ONLY` | 单日或单周亏损达界；已识别现金流待复核；数据不完整但尚未证明达到 Kill 边界 | `false / true / false` | 禁止新风险，撤销未成交入场，允许所有安全退出 |
| `ENTRY_ALLOWED` | 快照完整、新鲜、阈值均未触发且无待复核事项 | `true / false / false` | 仍需逐笔通过仓位、暴露、余额、精度和最小名义金额检查 |

同一时刻命中多个条件时，reason code 全部保留，但状态取最高优先级。Kill Switch 不是“立即
市价清仓”的同义词；在薄弱流动性或异常行情中强制市价单可能扩大损失，实际退出方式必须按
runbook 评估。任何状态都必须满足 `safe_exit_allowed=true`，风险系统不得阻止止损、减仓、
撤销未成交入场单或经批准的紧急退出。

## 7. RiskSnapshot 新鲜度与发布

- watchdog 目标发布周期为 15 秒；每个快照的 `expires_at_utc` 固定为
  `generated_at_utc + 60 秒`；
- 账户和行情源观测在生成时不得超过 30 秒；生成时间比消费者时钟超前超过 5 秒视为无效；
- 使用同目录临时文件、flush/fsync 后原子替换；消费者不得读取部分写入文件；
- `schema_version=1`。消费者只能接受明确支持的版本，不得忽略未知必填字段后继续入场；
- 快照缺失、过期、JSON/YAML 解析失败、schema 不通过、签名/hash 不匹配、版本不支持或
  原子发布失败时，消费者本地降级为 `entry_allowed=false`、`close_only=true`、
  `kill_switch=false`，并发出相应 `snapshot_*` reason code；
- 消费者不得无限沿用最后一个有效快照。恢复新入场必须读取一个新生成、完整且未过期的快照。

上述本地降级不伪造新的 RiskSnapshot，也不自动宣称已触发资金级 Kill Switch；如果无法证明
账户未越过 Kill 边界，操作上仍按人工复核处理。

## 8. 暴露、余额与交易所约束

RiskSnapshot 同时记录已持仓 quote 暴露和未成交入场 quote 暴露。逐笔审批必须继续使用
P2-03 的同一纯函数，并取风险预算、波动上限、symbol 暴露、方向暴露和可用余额中的最小值。
数量只能向下对齐交易所 step；最小数量、最小名义金额、price/amount precision 和可用余额
必须来自锁定版本的运行配置或实时 instrument metadata，不得从文档示例硬编码。

挂单暴露不能因资金仍显示为冻结现金而被忽略。任何无法归属到已知 Freqtrade Trade/Order
的挂单或持仓进入人工复核，禁止新入场。

## 9. 解除与恢复

- 日亏状态只允许在新的 UTC 日、获得新鲜完整快照且没有周亏/Kill/人工复核原因时自动解除；
- 周亏、现金流复核和所有 Kill 状态不得自动解除；
- 恢复必须记录触发证据、账户与 Runtime DB 对账结果、开放订单/持仓、处置决定、批准人、
  新快照 ID 和恢复时间；
- 不得通过重启进程、删除状态文件、回滚时钟或提高阈值解除；
- 提高任一风险阈值必须更新 ADR-0001、本决策和配置，并由项目所有人重新批准。

## 10. 验收与延期边界

本合同使每个状态具有确定输入、输出和动作；schema 固定可重放字段，runbook 固定人工处置。
P0-06 不实现运行服务。以下验证迁移但继续阻止对应门禁：

- P3-01：watchdog、原子发布、会计计算、30 秒源新鲜度、15 秒发布与 60 秒 TTL；
- P3-02：Freqtrade 只读快照、stale/missing/corrupt/unsupported fail-closed 与安全退出；
- P3-05：Kill、时钟漂移、资金差异和恢复演练；
- P4-01/P4-05：Paper 启动与恢复审批证据。

项目所有人于 2026-07-16 在 P0-08 Scope Frozen 批准中接受本风险口径和 runbook，P0-06
状态更新为 `DONE`。后续实质修改风险阈值、会计口径或恢复动作时必须重新评审。
