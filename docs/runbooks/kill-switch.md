# Kill Switch 与 Close-Only 操作手册

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-06 |
| 风险合同 | `docs/decisions/0006-risk-accounting.md` |
| 适用范围 | Freqtrade AI MVP，Bybit Spot + USDT Linear Perpetual，Instrument Registry 配置标的 |

## 1. 目标与权限边界

2026-07-18 修订：本手册同时适用于 Spot/Futures 双实例。AI 决策周期、新闻服务和 Telegram 审批故障不得阻止已有止损、安全减仓和 Kill Switch。RiskSnapshot v2 已能表达 mark/liq、保证金、funding 和保护挂单，但合约处置、reduce-only、position mode 与交易所托管保护单仍须完成 R5 Demo/Testnet 验证，不能仅凭只读快照宣称处置已验证。

本手册用于风险快照异常、`CLOSE_ONLY` 或 `KILLED_MANUAL_REVIEW`。它不授权扩大风险、绕过
Freqtrade 或由 watchdog 直接下单。只有 Freqtrade 是交易写入者；人工操作必须通过已批准的
Freqtrade/交易所恢复流程完成并留痕。

任何风险状态都允许止损、减仓、撤销未成交入场和经批准的安全退出。不得为了阻止开仓而同时
关闭退出路径。

R3-06 提供双重白名单保护的 Telegram 控制命令：`/pause_ai` 只停止模型请求，`/stop_entries` 阻止
OPEN/ADD，`/emergency` 同时执行前两项并持久化“撤销待处理入场、人工复核”意图。回执中的
“交易指令：未发送”是必要边界：R4 ExecutionGateway 完成前，该入口不代表已经撤单、减仓或平仓。
`/resume_entries` 只在最新可信 RiskSnapshot 允许入场时可用；紧急模式不能通过普通 Telegram resume
命令解除。

## 2. 触发分级

| 级别 | 典型 reason code | 初始动作 | 自动恢复 |
|---|---|---|---|
| Fail-closed | `snapshot_missing`、`snapshot_stale`、`snapshot_corrupt`、`schema_version_unsupported` | 拒绝新入场，保持退出，告警并恢复快照发布 | 仅在新快照完整有效后 |
| Close-only | `daily_loss_limit_reached`、`weekly_loss_limit_reached`、`external_cash_flow_pending_review` | 拒绝新入场，撤销未成交入场，保留退出 | 仅日亏可在下一 UTC 日有条件自动恢复 |
| Kill | `drawdown_limit_reached`、`absolute_loss_limit_reached`、`manual_kill_switch`、`unexplained_balance_difference`、`accounting_currency_mismatch` | 拒绝新入场，撤销未成交入场，停止自动恢复，人工决定持仓处置 | 否 |

reason code 同时出现时按 Kill > Close-only > Fail-closed 处理。

## 3. 首个 5 分钟

1. 记录 UTC 触发时间、当前 commit/config hash、bot 实例、RiskSnapshot ID、全部 reason code 和
   告警来源；禁止删除或覆盖原始日志与快照。
2. 确认入场已 fail-closed；如果不能确认，停止策略新信号消费或使用 Freqtrade 支持的暂停方式，
   但不得终止安全退出逻辑。
3. 撤销所有未成交入场单；记录每个 order id、撤销结果和累计成交数量。部分成交不得按零仓位
   处理。
4. 不撤销保护性退出或 stoploss；检查退出路径、网络、进程、时钟与告警仍可用。
5. 获取只读证据：交易所余额/订单/成交、Freqtrade Runtime DB 状态、当前持仓、公开行情、
   最新有效快照与前一个快照。此阶段禁止创建补偿订单。

## 4. 对账清单

逐项核对并保存差异：

- 交易所 open/closed orders 与 Freqtrade Trade/Order；
- BTC、ETH、USDT 的可用和冻结余额；
- partial fill、手续费币种、返佣、奖励、充值、提币、人工交易和自动兑换；
- `quote_cash`、base quantity、accrued fees、known liabilities 与 mark price 时间戳；
- 日/周 opening NAV、净外部现金流、现金流调整高水位、累计 PnL 与绝对损失边界；
- 系统 UTC 时钟、快照生成/过期时间和行情观测新鲜度；
- bot-managed stoploss 是否仍运行；进程或网络离线期间的未保护暴露。

如果任何余额或订单无法唯一解释，保持 `KILLED_MANUAL_REVIEW`，不得用“差额较小”作为自动
忽略理由。

## 5. 持仓处置

Kill Switch 不等于无条件立即市价清仓。运维负责人依据当前流动性、价差、stoploss 状态、
网络可靠性和持仓规模，从以下已批准动作中选择并记录理由：

1. 保持现有保护性退出并禁止新入场；
2. 通过 Freqtrade 正常退出路径分批减仓；
3. 在保护性退出失效或损失继续扩大的紧急情况下，通过受控人工恢复流程退出。

watchdog、Audit Writer 或临时脚本不得成为第二个订单写入者。任何人工交易都会产生新的对账
事件；完成前保持人工复核状态。

## 6. 分原因处置

### 6.1 快照缺失、陈旧、损坏或版本不兼容

- 检查 watchdog 进程、原子替换目录权限、磁盘空间、时钟和 schema 版本；
- 保留最后一个有效快照用于调查，但不得继续授权入场；
- 修复后生成全新 snapshot ID；消费者成功校验后才可解除 fail-closed；
- 如果故障期间无法证明账户未触及 Kill 边界，升级人工复核。

### 6.2 日亏或周亏

- 确认 PnL 包含未实现损益、手续费并扣除了已识别外部现金流；
- 日亏只在下一 UTC 日、完整新快照且不存在其他 reason code 时允许自动恢复；
- 周亏必须由项目所有人复核，禁止等到下周后无记录自动恢复。

### 6.3 回撤或项目绝对损失

- 复算现金流调整高水位、批准资本基线、10% 比例边界和 45 USDT 固定边界；
- 即使价格反弹后数值低于阈值，也不得自动清除 Kill 状态；
- 项目所有人必须决定继续关闭风险、终止候选或另行批准恢复；不得临时提高阈值。

### 6.4 外部现金流或未知资金差异

- 为充值、提币、返佣、奖励、人工交易或划转建立唯一事件记录；
- 已识别现金流先进入 `CLOSE_ONLY`；无法分类、币种错误、未知负债或金额对不上时进入 Kill；
- 修正会计记录不能回写或篡改原始交易所/Runtime DB 证据。

## 7. 恢复门禁

恢复新入场前必须同时满足：

- 触发原因已经解释且不再存在；
- 交易所与 Freqtrade Runtime DB 的订单、成交和持仓已对账；
- 所有未成交入场单已处理，partial fill 已计入持仓；
- watchdog 发布新的完整快照，源观测不超过 30 秒，快照未超过 60 秒 TTL；
- `entry_allowed=true`、`close_only=false`、`kill_switch=false`，且没有待人工复核 reason code；
- 记录批准人、UTC 时间、commit/config hash、恢复前后 snapshot ID 和验证结果；
- 周亏、现金流复核和 Kill 场景获得项目所有人明确批准。

禁止通过重启、删除快照、修改系统时间、回滚 Runtime DB 或提高阈值恢复。

## 8. 演练与证据

P3-05 至少演练：stale snapshot、部分写入、clock skew、日亏、周亏、回撤、绝对损失、未知
余额差异、partial fill 后撤单和恢复审批。每次演练保存输入 fixture、期望状态、实际动作、
告警时间、恢复证据和未解决差异；任何安全退出被阻塞或新入场未 fail-closed 都判定失败。
