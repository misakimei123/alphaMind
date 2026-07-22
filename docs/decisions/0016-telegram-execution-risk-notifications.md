# ADR-0016：Telegram 执行与风险通知采用受限事实和持久化 outbox

- 状态：Accepted
- 日期：2026-07-22
- 关联任务：R3-05

## 背景

R3-04 已将通过重新校验的 Action 收口为 `QUEUED`，但用户仍需要收到执行成功、部分成交、未执行、失败和
风险事件通知。R4 尚未实现 ExecutionGateway、订单提交与成交对账，因此 R3-05 不能从 Proposal Store
推断订单或成交，也不能提前把 `QUEUED` 解释为已执行。

Telegram `sendMessage` 与本地 SQLite 无法组成原子事务：若先发送后记录，进程在两者之间崩溃可能重复
通知；若先记录为完成再发送，则可能永久丢失通知。执行和风险通知还必须在 Telegram 暂时不可用或进程
重启后继续投递，并避免把 provider 响应、异常正文、凭据或原始 Telegram 身份写入状态库。

## 决策

1. 新增 `TelegramNotification v1`。通知只接受执行组件或风险组件产生的可信受限事实，类型固定为
   `EXECUTION_SUCCEEDED`、`EXECUTION_PARTIALLY_FILLED`、`EXECUTION_NOT_EXECUTED`、
   `EXECUTION_FAILED` 和 `RISK_ALERT`；
2. 执行事实必须绑定 proposal、execution、instrument、market 和 action。部分成交必须满足
   `0 < filled < requested` 并包含成交均价；未执行不得声明成交；失败若已有累计成交，必须明确保留的
   数量与均价。风险告警不能夹带 execution 事实；
3. 通知正文只展示稳定 reason code、受限标识符、数量、均价、订单引用和风险状态。部分成交或失败明确
   提醒可能仍有敞口，最终事实以 Freqtrade Runtime DB 与交易所对账为准；原始异常和远端响应正文不进入
   合同或消息；
4. `source_event_id` 经 UUIDv5 确定性派生稳定 `notification_id`。相同来源和相同内容重复入队是幂等操作，
   相同来源的异文冲突 fail-closed；
5. 通知先写入独立 SQLite FULL/WAL outbox，再由 worker lease 认领和投递。outbox 不保存原始 chat/user ID；
   目标 chat 由当前运行环境在投递时提供。失败只保存稳定错误码并指数退避，达到上限进入 dead letter；
6. 投递语义为至少一次。发送成功后、SQLite 标记完成前崩溃仍可能产生重复消息；所有消息携带稳定
   notification ID 供用户和后续审计识别。不得宣称 Telegram 通知 exactly-once；
7. R3-05 不扩展 Proposal Store 的订单/成交状态，也不读取真实 Telegram、Bybit 或 Freqtrade 凭据。
   R4 必须从 Freqtrade Runtime DB 与交易所产生可信执行事实后，才能驱动真实执行结果通知。

## 后果

- R4 可以把真实对账结果映射为同一稳定通知合同，而不需要让 Telegram 适配器读取 Runtime DB；
- Telegram 暂时失败和 worker 重启不会静默丢失已入队事实；正常运行中相同来源只发送一次；
- 极小概率重复发送是跨系统非原子边界的显式残余风险，稳定 notification ID 使其可识别和审计；
- Proposal Store 继续只拥有授权与重新校验事实，不成为第二套订单、成交或风险权威。

## 被否决方案

- **进程内集合去重**：重启后失效，无法满足通知恢复；
- **直接发送、不持久化**：Telegram 故障或进程退出会静默丢失通知；
- **把执行结果写进 Proposal Store**：R4 尚未提供可信订单/成交来源，会提前突破 Runtime DB 权威边界；
- **保存原始异常供排障**：可能泄漏 token、远端响应和账户信息；通知链只保存稳定错误码。
