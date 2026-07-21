# ADR-0015：批准后必须重新绑定可信快照才能进入执行队列

- 状态：Accepted
- 日期：2026-07-21
- 关联任务：R3-04

## 背景

R3-03 已证明 Telegram callback 的身份、目标 chat、nonce、TTL 和单次用户决定，但 `APPROVED` 只代表
用户授权了原始 Action。审批期间价格、仓位、余额、挂单、RiskSnapshot 或交易所市场规则都可能变化；若
批准后直接交给 ExecutionGateway，会把旧快照上的意图错误解释为当前可执行事实。

既有 ApprovalEvent Schema 已预留 `APPROVED -> REVALIDATING -> QUEUED/EXPIRED/CANCELLED/FAILED`，
但 R3-01 Store 的 SQLite CHECK 和运行时方法只启用了审批前状态。R3-04 必须显式启用重新校验状态，同时
保持 Freqtrade/交易所仍是后续订单与成交的唯一权威。

## 决策

1. `RevalidationCoordinator` 只接受 `APPROVED` Proposal。它先以 expected state 和确定性 idempotency key
   迁移到 `REVALIDATING`；重复处理已经 `QUEUED/CANCELLED/EXPIRED` 的 Proposal 只返回既有终态；
2. `ActionRevalidator` 只消费经过 `DecisionContractBinder` 绑定的新 `DecisionContext`、经过
   `load_risk_snapshot` 验证的 `SnapshotReadResult`、有效配置中的 Instrument Registry 与
   `MarketCapabilitySnapshot`。任一输入缺失、损坏或不一致均 fail-closed；
3. Context 的 `risk_snapshot_id`、NAV、spot 可用余额、futures 可用保证金、risk state 和 open order ID
   集合必须与同一 RiskSnapshot 一致，防止混用不同时间点的账户、挂单和风险事实；
4. `OPEN/ADD` 只有在当前 RiskSnapshot 仍允许 entry、价格仍位于用户批准的 entry range、目标仓位状态
   仍满足 Action、没有同方向风险增加挂单且可用余额/保证金足以覆盖当前最小 notional 时才能通过；
5. `REDUCE/CLOSE/CANCEL_ORDER/REPLACE_PROTECTION` 必须保留安全退出权限，并重新检查目标仓位、方向、
   挂单或保护单引用。重新设置保护不得放松已有 stop；
6. 当前市场必须仍可用，price tick、quantity step、最小数量和最小 notional 必须完整且为正；所有批准
   价格重新按当前 tick 校验，futures leverage 重新受当前 effective max leverage 限制；
7. Proposal 到期时经 `REVALIDATING -> EXPIRED` 收口；其他重新校验拒绝进入 `CANCELLED`。两类失败均不
   创建 execution 详情，不调用交易接口；
8. 只有全部检查通过才能进入 `QUEUED`，并一次性保存确定性 execution ID、revalidated timestamp、
   `context_sha256`、`risk_snapshot_id`、market capability source SHA-256 和空 order ID 集合。这是交给 R4
   ExecutionGateway 的不可变输入绑定，不代表订单已提交；
9. R3-01 SQLite Store 原库就地迁移扩展 CHECK 状态，保留原 proposal/event 表内容、WAL/FULL 和外键；
   不创建第二套订单数据库，也不迁移或修改 Freqtrade Runtime DB。

## 后果

- 人工批准不再能绕过审批期间发生的价格、账户、挂单、风险或规则变化；
- 相同 Proposal 最多形成一个 `EXECUTION_QUEUED` 事件和一个 execution ID，重复调度不会扩大执行次数；
- R3-04 仍是离线 dry-run 合同：没有连接真实 Bybit/Freqtrade、读取交易凭据或创建/修改/取消订单；
- R4 必须消费已绑定的 QUEUED execution 详情并继续实现 submit unknown、订单幂等和成交对账，不能重新把
  `APPROVED` 直接解释为可下单。

## 被否决方案

- **批准后直接调用 Freqtrade**：绕过最新账户和市场事实，且混淆授权与订单所有权；
- **只重新检查价格**：仓位、余额、挂单、风险状态和市场规则同样可能在审批期间变化；
- **重新运行 LLM 决策**：会生成未经用户批准的新意图；R3-04 只能校验原始 Action，不得改写它；
- **重新校验失败后保留 APPROVED 等待自动重试**：会让过期授权在未来条件变化时被动复活；
- **把校验结果只留在内存**：进程重启后无法证明 QUEUED execution 绑定了哪个 Context 和 RiskSnapshot。
