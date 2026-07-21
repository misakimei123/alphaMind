# ADR-0013：使用 python-telegram-bot 实现 Telegram 审批适配层

- 状态：Accepted
- 日期：2026-07-21
- 关联任务：R3-02

## 背景

R3-01 已把逐 Action Proposal、不可变审批事件和审批前状态机收口到本地 Proposal Store。R3-02 需要把
`VALIDATED` Proposal 展示到 Telegram，并处理详情、批准、拒绝和过期后的消息状态，但不能提前承担
R3-03 的原始 user/chat 白名单、nonce 生成与 callback 验证，也不能把 Telegram 点击解释为订单或成交。

Telegram Bot API 是异步网络边界，按钮 `callback_data` 只有 1–64 UTF-8 bytes，callback 必须及时 ACK。
项目需要使用成熟、主流且具有类型标注的 Python 客户端，同时保持离线可测和错误信息不泄漏 token。

## 决策

1. 使用 `python-telegram-bot` 22.x，当前锁定 `22.8`；依赖范围为 `>=22.8,<23`。其默认网络后端要求
   `httpx<0.29`，因此项目同步收紧既有 `httpx` 上界；
2. `TelegramBotClient` 只封装 `Bot.send_message`、`Bot.edit_message_text` 和
   `Bot.answer_callback_query` 三个 async 方法，并提供 `Bot.initialize`/`shutdown` 生命周期；底层
   `TelegramError` 统一转换为不含 token、URL 或响应正文的安全错误；
3. 消息使用纯文本和原生 `InlineKeyboardMarkup`，不启用 parse mode。概览与详情展示标的、市场、方向、
   动作、入场、止损、止盈、杠杆、有效期、理由、新闻引用和风险，并明确“批准尚不等于执行”；
4. 按钮 payload 只包含操作类型和 proposal ID，不包含原始 nonce、user/chat ID 或完整交易参数，并在构造
   时强制 64 bytes 上限。原始 callback 解析和可信身份构造仍由 R3-03 完成；
5. 消息发送成功后才调用 `ProposalStore.request_approval`。Store 迁移失败时尽力编辑消息、移除按钮，不能
   伪造 `PENDING_APPROVAL`；HTTP 与 SQLite 之间无法实现原子事务，R3-03 继续以幂等 callback 和恢复策略
   收口崩溃窗口；
6. 本层只接受 `VerifiedTelegramCallback`。它是 R3-03 验证边界未来产出的内部事实，不代表 R3-02 已实现
   白名单或 nonce 校验。回调先 ACK，再展示详情或调用 Store 的单次决定；到期回调转为 `EXPIRED`；
7. `APPROVED` 只表示人工授权，消息必须显示“尚未执行”。执行前重新校验属于 R3-04，订单、成交和仓位
   事实仍属于后续 ExecutionGateway、Freqtrade Runtime DB 与交易所。

## 后果

- R3-02 可以用 `python-telegram-bot` 的正式类型和 async API 完成展示与消息更新，同时测试通过 fake Bot
  和离线 Store 验证，不连接真实 Telegram；
- Telegram token 仍只在运行环境创建客户端时出现，不写入 Proposal Store、callback data、日志或 fixture；
- R3-03 必须实现原始 Update/callback 解析、user/chat 白名单、nonce 一次性消费和重复点击闭环，不能直接
  把未验证的 Telegram 数据构造成 `VerifiedTelegramCallback`；
- 消息已发送但进程在 Store 迁移前崩溃的极小窗口不能靠普通 HTTP 调用消除，后续恢复逻辑必须把 Store
  状态作为授权事实源，绝不能仅凭消息存在或按钮点击执行。

## 被否决方案

- **继续手写 httpx Bot API 客户端**：会重复实现 `python-telegram-bot` 已提供的类型、错误和异步生命周期，
  扩大协议维护面；
- **在 callback data 中携带原始 nonce 或完整 Action**：增加泄漏和重放风险，且容易超过 Telegram 64 bytes
  限制；
- **R3-02 直接解析并信任原始 Update**：会把展示任务与 R3-03 安全边界混合，在白名单和 nonce 未完成时
  产生虚假的授权保证；
- **批准后直接调用交易接口**：绕过 R3-04 重新校验和 R4 ExecutionGateway，违反 Freqtrade 单一交易写入者
  及订单事实所有权。
