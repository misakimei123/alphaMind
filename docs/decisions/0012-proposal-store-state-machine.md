# ADR-0012：Proposal Store 的事件历史与审批前状态机

- 状态：Accepted
- 日期：2026-07-21
- 关联任务：R3-01

## 背景

R2-07 的 Decision Journal 已保存通过本地 Schema 与业务校验的 HOLD、候选动作和模型错误，但 Journal
只描述 AI 周期终态，不能表达“哪个动作已展示、批准、拒绝或过期”。R3 必须把一个 ModelDecision 中的
多个 Action 分开授权，并保证重复消费、并发请求或重复点击不能产生第二次状态迁移。

ApprovalEvent v1 和 ApprovalRecord v1 已冻结状态名称与合法迁移。R3-01 需要实现其本地运行时存储，
同时保持以下边界：Telegram 发送属于 R3-02；环境白名单、原始 nonce 生成及 callback 解析属于 R3-03；
执行前账户/行情/风险复核属于 R3-04；订单和成交事实仍属于 Freqtrade Runtime DB 与交易所。

## 决策

新增独立 SQLite FULL/WAL `ProposalStore`，默认路径为
`user_data/state/proposals.sqlite`。采用不可变事件历史加当前投影：

1. 只消费 `DecisionOutcome.CANDIDATE_ACTIONS`，且只为其中非 HOLD Action 创建 Proposal。proposal ID
   由 action ID 确定性派生，因此同一 Action 重放不会创建第二条记录；
2. 每个 Proposal 保存 source Decision Journal record SHA-256、完整 Action canonical JSON 与
   action SHA-256。创建时在同一事务追加 `CREATED`、`VALIDATION_PASSED`，终态为 `VALIDATED`；
3. R3-01 启用的迁移为 `VALIDATED → PENDING_APPROVAL → APPROVED/REJECTED/EXPIRED`。每次迁移都要求
   expected current state，并追加符合 ApprovalEvent v1 的事件；不能原地改写历史事件；
4. event ID 由 idempotency key 确定性生成。相同 key 与相同请求返回既有结果，相同 key 异文冲突、
   不同 key 的第二次用户决定以及非法状态迁移均 fail-closed；
5. Proposal 有效期取“Journal 记录时间 + Action valid_for_seconds”和“创建时间 + approval TTL”的较早者，
   不能因队列延迟延长模型动作有效期；过期前不能写 `APPROVAL_EXPIRED`，到期时不能再批准或请求审批；
6. Store 只接收 user/chat/nonce 的 SHA-256，不接收或保存原始 Telegram ID、callback nonce、Bot token、
   Bybit key。允许集合和 nonce 的安全生成、callback 一次性消费仍由 R3-03 完成；
7. 每次读取重新校验 ApprovalRecord Schema、Action/event hash、事件顺序与时间单调性、proposal/action
   绑定、当前 state 和 updated timestamp。任一正文、索引或投影被篡改即拒绝读取；
8. R3-01 不启用 `REVALIDATING` 之后的执行态写入；R3-04 已按
   [ADR-0015](0015-execution-preflight-revalidation.md) 显式扩展到 `QUEUED/EXPIRED/CANCELLED` 并绑定
   重新校验证据。订单提交、部分成交和最终结果仍必须由 R4 扩展，不能用当前 Store 声称已成交。

## 后果

- R3-02 可以安全读取 `VALIDATED` Proposal，消息发送成功后调用 `request_approval`；R3-03 可以在回调
  边界完成原始凭据验证后调用单次 `decide`。
- 一个 ModelDecision 可以产生多个独立 Proposal，各自具有 nonce、TTL、事件历史和最终用户决定。
- Proposal Store 是授权与重新校验事实源，但不是订单/持仓权威；`QUEUED` 仍必须经过后续
  ExecutionGateway。
- schema 或状态范围扩展必须显式迁移，不得静默把旧 Proposal 解释成已执行。

## 被否决方案

- **直接更新 Decision Journal**：会破坏 R2-07 的 AI 周期终态不可变性，并把一个决策中的多个动作混成
  单一审批状态。
- **只保存 ApprovalRecord 快照**：无法证明迁移顺序，也无法可靠识别重复点击、异文幂等冲突或并发覆盖。
- **把 Proposal 写入 Freqtrade Runtime DB**：违反 Runtime DB 单一写入者和订单/持仓权威边界。
