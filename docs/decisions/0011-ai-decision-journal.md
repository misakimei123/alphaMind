# ADR-0011：AI Decision Journal 的所有权与不可变合同

- 状态：Accepted
- 日期：2026-07-21
- 关联任务：R2-07

## 背景

R2-03 的 `UsageLedger` 只负责模型尝试、Token、成本和输入/config/prompt hash；它刻意不保存
DecisionContext、模型响应或候选动作。P3-03 的 Audit Outbox 负责把审计事件可靠投递到 Audit DB，事件会经历
`PENDING`、`IN_FLIGHT`、`DELIVERED` 等交付状态。Freqtrade Runtime DB 则只属于 Freqtrade 的订单、成交、
持仓和重启恢复。

R2-07 必须让每个已完成 AI 周期都留下可复核的 HOLD、合法候选动作或模型错误，并为 R3 Proposal Store
提供稳定输入。如果复用 Usage DB，会混淆计费尝试与决策终态；如果只写 Audit Outbox/Audit DB，会让审计
可用性影响审批主链；如果写 Runtime DB，则会制造第二个订单/持仓写入者。

## 决策

新增独立 SQLite `DecisionJournal`，默认由 `run-ai-decision --decision-db` 指向
`user_data/state/ai-decisions.sqlite`。它遵守以下合同：

1. 每个 `cycle_id` 只能有一个不可变终态：`HOLD`、`CANDIDATE_ACTIONS` 或 `MODEL_ERROR`；相同
   cycle 与相同内容幂等，异文冲突拒绝覆盖；
2. 只有通过 Schema binder 和 R2-04 逐动作业务校验的决策正文能够持久化；全部拒绝、provider 错误、
   超时、拒答、预算错误等只保存安全 `error_code` 和可公开验证摘要，不保存异常消息、API key、原始
   Prompt 或未经绑定的原始响应；
3. 每条记录绑定 environment、profile/model ID、prompt ID/version/SHA-256、effective config SHA-256、
   DecisionContext 输入 SHA-256，以及当前四份 Decision/Action Schema 版本；候选正文同时绑定自身
   SHA-256；
4. 记录正文使用确定性 canonical JSON，并保存整体 SHA-256。读取时重新校验正文 hash、索引列、版本
   绑定和 outcome/动作一致性；数据库内容被篡改时 fail-closed；
5. provider 只有在 Journal 追加成功后才能向调用方返回成功候选动作。Journal 不可写、内容冲突或校验
   失败时返回 `decision_persistence_error` 和 `HOLD_ONLY`，不得继续交给审批；
6. Journal 明确记录 `runtime_authority=false`、`contains_secrets=false`。它是 AI 输出事实源，不证明
   Telegram 已批准、风险层已定仓、订单已提交或交易所已成交。

## 后果

- R3 可以从独立 Journal 读取已验证候选动作，再建立 Proposal/Approval 状态机；R3 不需要解析 provider
  stdout，也不依赖 Audit Writer 是否在线。
- Audit 层后续可以按 Journal 记录派生审计事件，但不得反向覆盖 Journal，也不得用 Audit DB 恢复
  Freqtrade 订单状态。
- 当前 schema v1 不提供 update/delete API。未来字段变更必须升级 `record_schema_version` 并采用显式
  迁移或新库，不得静默改变旧记录语义。
- SQLite WAL + `synchronous=FULL` 提供单机 durable append；多主机共享存储和归档不在 R2-07 范围。

## 被否决方案

- **只扩展 UsageLedger**：一次 cycle 可以有多个计费 attempt，但只能有一个决策终态，生命周期不同。
- **只写 Audit Outbox/Audit DB**：Outbox 的交付状态可变，Audit DB 不是审批主链事实源；审计故障不能
  让已验证候选动作变成不可恢复的内存对象。
- **写入 Freqtrade Runtime DB**：违反 Runtime DB 单一写入者与订单/持仓权威边界。
