# ADR-0007：冻结 Audit、Replay 与数据库所有权

| 元数据 | 内容 |
|---|---|
| 状态 | READY_TO_VERIFY |
| 任务 | P0-07 |
| 制定日期 | 2026-07-16 |
| 前置决策 | ADR-0003、ADR-0005、ADR-0006 |
| 审计事件合同 | `data/schemas/audit-event.schema.yaml` |
| 实验合同 | `data/schemas/experiment.schema.yaml` |

## 1. 决策范围

本文冻结 Freqtrade MVP 的 Runtime DB、Research/Audit DB、本地 Audit Outbox、Audit Writer
和 Deterministic Replay 边界。P0-07 只建立合同，不实现数据库、sidecar、Replay runner、
认证交易所连接或订单写路径；实现与故障演练仍受 P3-03、P3-04 和 P3-05 门禁约束。

核心不变量：交易所保存实盘操作事实，Freqtrade Runtime DB 保存唯一内部订单/持仓运行事实，
Audit DB 只保存不可反向恢复的观察与审计证据。三者不得互相冒充。

## 2. Runtime DB 所有权与环境隔离

- 只有锁定版本的 Freqtrade 可以写入、迁移和解释 Runtime DB 的 `Trade`、`Order`、open
  position 与恢复状态；alphaMind 不直接 INSERT、UPDATE、DELETE 或执行 schema migration；
- backtest、dry-run、replay、testnet contract 和 live 使用不同数据库、用户/schema、配置与
  secret；禁止用一个 `is_live` 开关复用同一状态库；
- Windows 本地开发和 dry-run 可以使用隔离 SQLite；Paper/Live 生产候选固定评估 PostgreSQL，
  并为 watchdog 创建 `CONNECT + USAGE + SELECT` 的独立只读角色，强制
  `default_transaction_read_only=on`；
- watchdog 对 SQLite 只能使用只读 URI 和 `PRAGMA query_only=ON`；对 PostgreSQL 只能执行
  版本登记的 SELECT allowlist。任何 schema/version 不匹配都使新入场 fail-closed；
- watchdog 的账户余额与成交事实来自独立 read-only 交易所凭据，公开 mark price 使用无认证
  公共端点；该凭据不得拥有 trade 或 withdrawal 权限；
- callback 不读取 Runtime DB、交易所或 watchdog。它只读取内存中的已验证 RiskSnapshot，
  并写本地 outbox；
- Audit DB 中的 `freqtrade_trade_id`、`exchange_order_id` 等只是可空只读关联，不建立能级联
  修改 Runtime DB 的外键、trigger 或双向同步。

Freqtrade 负责运行时 migration；运维负责人负责备份、恢复、版本升级和演练。Paper/Live 候选
目标为 Runtime DB `RPO <= 5 分钟`、`RTO <= 60 分钟`，具体 PostgreSQL base backup、WAL
归档、恢复命令和实测证据由 P3-04 落地。恢复顺序固定为：交易所事实 -> Freqtrade Runtime
DB -> Freqtrade 恢复/对账 -> 允许安全处置 -> Audit Writer 补写。Audit DB 不参与反向恢复。

## 3. 本地 Audit Outbox

callback 到 Audit DB 的唯一通道是独立的本地 SQLite outbox；它不与 Freqtrade Runtime DB
共库。固定合同如下：

| 项目 | 冻结值 |
|---|---|
| 存储 | 持久卷上的独立 SQLite，WAL，`synchronous=FULL` |
| 写入 | 单条 append-only INSERT，`event_id` 主键，单事务 |
| callback 等待上限 | 50 ms；超时、只读、磁盘满或写失败时新入场 fail-closed |
| 单事件上限 | canonical JSON UTF-8 不超过 16 KiB |
| 逻辑容量 | 10,000 个未确认事件 |
| 文件容量 | 256 MiB；以逻辑大小和实际文件大小中先到者为准 |
| 预警阈值 | 5,000 pending、最老事件 2 分钟或 128 MiB，任一达到即告警 |
| 入场停止阈值 | 8,000 pending、最老事件 5 分钟或 192 MiB，任一达到即拒绝新入场 |
| 安全事件保留 | 最后 2,000 个逻辑槽位只允许 exit、Kill、reconcile、operator 事件 |

入场批准相关事件必须先成功写入 outbox，才允许 callback 返回批准。outbox 背压只阻止新风险，
不得阻止止损、减仓或紧急退出。安全退出事件在 outbox 已满或损坏时仍先执行退出，同时写本地
emergency log 并触发最高级告警；不得为了“审计完整”扩大资金风险。

心跳和高频行情不进入逐条 outbox；它们使用聚合指标。outbox 文件不可写、event 超限、WAL
恢复失败或 backlog 达停止阈值，均产生 `audit_backpressure` 状态并保持 close-only，直到新鲜
RiskSnapshot 与 outbox 健康检查同时恢复。

## 4. Audit Writer、幂等与失败状态

Audit Writer 是唯一写 Research/Audit DB 的组件，不持有 Runtime DB 写权限或交易所 trade
凭据。投递语义为 at-least-once，Audit DB 以 `event_id` 唯一约束实现幂等：

- 状态只允许 `PENDING`、`IN_FLIGHT`、`DELIVERED`、`DEAD_LETTER`；
- 每批最多 100 条，按 producer + sequence 稳定排序；claim lease 为 60 秒，writer 崩溃后
  超时事件回到 `PENDING`；
- 失败使用带 jitter 的指数退避，基础 1 秒、上限 60 秒；连续 20 次失败转入
  `DEAD_LETTER`，保留原事件、错误分类和每次 attempt，不自动删除；
- duplicate key 只有在 Audit DB 中相同 `event_id` 的 content hash 一致时才算成功；hash
  不同进入 dead-letter 和人工复核；
- Audit DB 确认持久化且 hash 一致后标记 `DELIVERED`；本地已交付记录至少保留 7 天，清理
  只能由 writer 在不影响 pending/dead-letter 的事务中执行；
- Audit DB 或 writer 离线不会停止已有持仓的安全退出，但达到 outbox 停止阈值后禁止新入场。

## 5. AuditEvent 合同

`audit-event.schema.yaml` 冻结公共 envelope：事件 ID、producer sequence、UTC 时间、环境与证据
层级、strategy/config/runtime hash、RiskSnapshot、只读 Runtime 关联、reason code、payload
hash 和 secret/redaction 标志。Audit 记录固定 `runtime_authority=false`，不能成为 Trade/Order
恢复源。

event type 的 payload 可以独立版本化，但必须先登记 `payload_schema` 和 hash；未知 payload
版本不得静默解释。事件禁止包含 API key、secret、完整认证 header、私钥或可复用 token。
事件内容 hash 使用 RFC 8785/JCS canonical JSON 对除 `event_content_sha256` 外的完整事件计算。

## 6. Experiment 合同

`experiment.schema.yaml` 固定实验在运行前必须登记的身份、假设、strategy/config/runtime hash、
dataset manifest、Final Holdout 状态、walk-forward folds、参数空间、trial budget、成本假设、
指标和证伪条件。结果只能追加到同一 experiment identity，不能覆盖预注册字段。

- `PRE_REGISTERED` 不得包含结果；`RUNNING` 必须记录开始时间；`COMPLETED`、`REJECTED`、
  `INVALIDATED` 必须记录结束时间、artifact hash 和结论；
- Final Holdout 为 `SEALED_UNREAD` 时，实验只能引用开发池；读取 holdout 后必须记录 commit、
  时间和 access count，并遵守 ADR-0005；
- Historical Backtest、Dry-run、Replay、Testnet Contract 和 Live Canary 的 evidence layer 分开，
  不得将 replay 结果标记为 production write path 已验证。

## 7. Replay 权限和验证边界

Deterministic Replay 使用冻结 fixture、fake adapter 或锁定版本 Freqtrade 的公开测试接口。Replay
进程必须满足：无生产 API Key、无 Runtime/Audit 生产凭据、无外网交易路由、
`trade_write_permitted=false`。schema 对 replay evidence 强制这一组合。

Replay 负责证明 alphaMind 拥有的确定性逻辑：RiskSnapshot 消费、风险状态、outbox 幂等与背压、
审计投递、告警、对账差异分类和运维动作。它不实现或声明拥有生产订单状态机。

| 场景 | 验证方式 | 结论边界 |
|---|---|---|
| partial fill 后撤单 | contract fixture 输入累计 filled、remaining、fee 和终态；锁定版本 Freqtrade integration 验证字段映射 | alphaMind 按累计成交计算暴露；不证明真实交易所成交行为 |
| submit unknown / API timeout | fixture 输入“结果未知”观察，触发 entry fail-closed、告警和 Freqtrade operator reconcile | 不在 alphaMind 盲重试或创建第二订单状态机 |
| 重复事件 / writer 重启 | 重放同一 event ID、lease 过期和批量重试 | Audit DB 只有一个相同 hash 的事实 |
| Runtime DB 与交易所差异 | 只读 snapshot fixture | close-only，先走 Freqtrade 恢复，再补 Audit |

真实参数、权限、最小名义金额和订单查询只能由独立 Testnet Contract Harness（若可信）或 Live
Canary 证明；Dry-run 和 Replay 不得冒充这些证据。

## 8. 验收与延期边界

本任务完成了三项计划产物，并冻结所有权、只读路径、outbox 容量/背压、writer 重试/去重、
数据库恢复顺序和 Replay 权限。运行验证延期但继续阻止对应门禁：

- P3-03：SQLite WAL、50 ms callback 上限、容量/年龄/文件阈值、lease、retry 和 dead-letter；
- P3-04：SQLite/PostgreSQL 只读角色、数据库隔离、RPO/RTO、备份恢复和 Freqtrade migration；
- P3-05：partial fill、submit unknown、重复事件、进程重启和无生产凭据 Replay；
- P4/P5：真实 dry-run、Paper、可选 contract 与 Live 证据分层。

当前状态为 `READY_TO_VERIFY`，等待项目所有人独立复核；实现者不自行将 P0-07 标记为
`DONE`，也不因此进入 P0-08 或 Phase 1 门禁。
