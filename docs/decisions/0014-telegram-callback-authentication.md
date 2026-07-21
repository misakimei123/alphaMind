# ADR-0014：Telegram callback 使用 HMAC、双重白名单与一次性 Proposal nonce

- 状态：Accepted
- 日期：2026-07-21
- 关联任务：R3-03

## 背景

R3-01 已提供 Proposal expected-state、TTL、nonce hash 和不可变审批事件；R3-02 已使用
`python-telegram-bot` 展示候选并消费内部 `VerifiedTelegramCallback`。尚未完成的安全边界是：从原始
`Update.callback_query` 提取 user/chat/message/data，证明按钮确由本系统为该 Proposal 和目标 chat 生成，
拒绝非白名单、篡改、过期和重复 callback，同时不把原始 ID、nonce、secret 或完整 Action 放进按钮或
Proposal Store。

Telegram 明确提示 callback `data` 不能被当作消息上现有按钮的可信证明，且 callback query 必须 ACK；
`CallbackQuery.message` 还可能是不可访问消息。仅解析 `approve:<proposal_id>` 无法证明 action、proposal、
nonce、TTL 与消息 chat 的绑定。

## 决策

1. `TelegramSecurityPolicy` 按 runtime 配置声明的环境变量名读取 user/chat allowlist 与 callback secret。
   通用 `EffectiveConfig` 不读取或保存真实值；环境错误只返回枚举，不回显 ID 或 secret；
2. 原始 user/chat ID 进入业务层前转换为 SHA-256。认证同时要求“当前环境白名单”和“Proposal 创建时白名单
   快照”命中；配置新增身份不能追溯批准旧 Proposal，配置移除身份立即生效；
3. 每个非 HOLD Action 使用 `secrets.token_bytes(32)` 生成独立 nonce，只保存 `nonce_sha256`。同一批次
   nonce hash 重复、长度错误或 generator 异常均 fail-closed；原始 nonce 不写数据库、callback 或日志；
4. callback data 固定为 `v1:<action-code>:<proposal-suffix>:<tag>`。`tag` 是 HMAC-SHA256 前 128 bits 的
   base64url 编码，签名绑定 action、proposal ID、nonce hash、expires_at 和目标 chat hash；完整 payload
   不超过 Telegram 64 UTF-8 bytes；
5. HMAC 使用至少 32 bytes 的环境 callback secret，并用 `hmac.compare_digest` 常数时间比较。修改 action、
   proposal、tag 或把按钮带到另一个 chat 都无法通过验证；secret 轮换使旧按钮 fail-closed；
6. `TelegramCallbackProcessor` 是原始 Update 唯一入口：存在 callback query 时先调用
   `answer_callback_query`，ACK 失败不记录决定；随后拒绝非 string data、inline/inaccessible message、未知
   Proposal、错误签名和非白名单身份；
7. 认证成功后只生成 hash 化的 `VerifiedTelegramCallback`。callback query ID 先 SHA-256 再作为 Store
   idempotency key；不保存原始 query ID；
8. TTL 由 Proposal Store 的 Action 有效期与审批 TTL 较早值控制。到期 callback 只能迁移为 `EXPIRED`；
   首次批准/拒绝依赖 expected `PENDING_APPROVAL` 追加一个决定事件，重复点击返回既有终态，不追加第二个
   用户决定，也不产生执行事实；
9. `APPROVED` 仍只表示人工授权。价格、仓位、余额、挂单、RiskSnapshot、市场能力和执行幂等必须由
   R3-04 重新校验；R3-03 不调用 Freqtrade、Bybit 或任何交易写接口。

## 后果

- R3-03 在不保存原始 Telegram 身份和 nonce 的前提下，完成 raw Update 到 Proposal 单次决定的认证闭环；
- 同一 allowlist 内的另一个 chat 也不能重放被转发按钮；被删除或不可访问的消息不能授权；
- callback secret 轮换会让所有尚未完成的旧按钮失效。运行手册必须把轮换视为显式取消未决审批，而不是
  尝试兼容旧签名；
- 当前实现是离线集成测试，未连接真实 Telegram 或读取真实凭据；真实 polling/webhook 生命周期和部署
  身份仍需后续运行任务验证，但不能放宽本 ADR 的认证规则。

## 被否决方案

- **只依赖 Telegram callback data 文本**：客户端可提交被篡改或与原消息按钮不一致的数据，缺少来源绑定；
- **把原始 nonce 放进 callback data**：违反已冻结 Schema 注释并扩大泄漏、重放和日志暴露面；
- **只检查当前环境白名单**：配置扩权会追溯授权创建时并未允许的新身份；
- **只检查 Proposal 白名单快照**：配置紧急移除身份后，旧 Proposal 仍可被批准；
- **用 callback query ID 作为 nonce**：该 ID 由 Telegram 在点击后产生，无法在 Proposal 创建时绑定，且原值
  不应进入持久化事件；
- **认证前修改 Proposal 状态**：会让未授权或篡改 callback 影响授权事实源。
