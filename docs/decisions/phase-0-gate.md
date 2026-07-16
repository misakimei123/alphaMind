# Phase 0 Scope Frozen Gate 评审

| 元数据 | 内容 |
|---|---|
| 状态 | BLOCKED |
| 任务 | P0-08 |
| 审查日期 | 2026-07-16 |
| 审查基线 | `main@f36e6ba` |
| 实施审查 | Codex（只核对证据，不具备独立门禁批准权） |
| 独立评审人 | 项目所有人（待确认） |

## 1. 结论

Phase 0 的计划产物、G-01 至 G-10 设计缺口、capability matrix、Final Holdout 和 trial
初始合同已经具备可评审证据，但 Scope Frozen gate **尚未通过**。

阻塞原因不是测试失败，而是 P0-05、P0-06、P0-07 均由 Codex 实施且仍为
`READY_TO_VERIFY`，仓库中没有项目所有人的独立复核记录。按照责任矩阵和开发计划，Codex
不能批准自己的交付物，也不能以本次自查替代独立评审。因此 P0-08 状态为 `BLOCKED`，在解除
下述阻塞前不得进入认证交易所接入、Freqtrade adapter、Paper 或 Live。

## 2. P0 产物清单

| 任务 | 当前状态 | 主要证据 | Gate 判断 |
|---|---|---|---|
| P0-01 | DONE | `0001-project-scope.md`、`ownership-matrix.md` | 范围、资金和职责已由项目所有人确认 |
| P0-02 | DONE | `0002-exchange-selection.md`、`exchange-capabilities.yaml` | Bybit spot 与关键能力已分类 |
| P0-03 | DONE | `0003-runtime-version-lock.md`、`runtime-versions.toml` | 版本/digest 已锁定；容器实测明确迁移至 P1-02 |
| P0-04 | DONE | `0004-first-strategy.md`、Strategy Card | 策略、14 次 trial 预算和证伪条件已冻结 |
| P0-05 | READY_TO_VERIFY | `0005-data-and-holdout.md`、数据 schema、regime manifest | 产物和测试存在，缺独立复核 |
| P0-06 | READY_TO_VERIFY | `0006-risk-accounting.md`、RiskSnapshot schema、Kill runbook | 产物和测试存在，缺独立复核 |
| P0-07 | READY_TO_VERIFY | `0007-audit-and-replay.md`、AuditEvent/Experiment schema | 产物和测试存在，缺独立复核 |
| P0-08 | BLOCKED | 本文 | 等待项目所有人完成 P0-05～P0-07 与本 gate 的独立复核 |

P0-03 的 Docker 实测、P0-05 的实际数据 hash/质量报告、P0-06 的 watchdog 运行测试和 P0-07
的 outbox/recovery 演练均已绑定后续任务和禁止越过的门禁。这些是明确延期的实现证据，不是
Phase 0 设计未知项，也不能被本文误报为已经完成。

## 3. G-01 至 G-10 审查

| 缺口 | 状态 | 决策证据与残余验证 |
|---|---|---|
| G-01 交易所 | 已解决 | ADR-0002 固定 Bybit 国际版现货；Live 前重查资格与端点 |
| G-02 第一策略 | 已解决 | ADR-0004 固定 Donchian 20/10、ATR(20) × 2 stop |
| G-03 运行版本 | 已解决 | ADR-0003 固定 Freqtrade 2026.6、CCXT 4.5.61 和镜像 digest；P1-02 实测 |
| G-04 数据原始层 | 已解决 | ADR-0005 固定首次落盘 Feather snapshot、hash 与禁止原地覆盖 |
| G-05 Runtime 只读路径 | 已解决 | ADR-0007 固定 SQLite/PostgreSQL 只读路径与 Freqtrade 唯一写入权 |
| G-06 Audit 通道 | 已解决 | ADR-0007 固定本地有界 outbox、背压、幂等 writer 和 dead-letter |
| G-07 Replay 边界 | 已解决 | ADR-0007 禁止生产凭据/写权限，不建立第二订单状态机 |
| G-08 Paper 证据数 | 已解决 | ADR-0004 固定至少 90 天、12 signal、8 fill、4 independent event |
| G-09 部署与 key | 已解决 | 本文第 4 节整合 P0-01、P0-02、P0-03 的已批准方向 |
| G-10 Kill 动作 | 已解决 | ADR-0006 与 runbook 固定 fail-closed、安全退出和人工恢复 |

## 4. G-09 部署与 Secret 合同

本节不创建 key、不部署服务，只把既有决策组合为可验收合同：

- Windows 只用于无生产 key 的本地研究；Paper/Live 生产候选固定为 Linux amd64 host 与
  ADR-0003 的完整 Docker platform digest；
- research、dry-run、testnet contract、live 使用不同配置、数据库和凭据；
- Freqtrade live key 只授予必要 SpotTrade/读取权限，禁止 Withdrawal，并绑定部署 host 的
  固定出口 IP；watchdog 使用另一把 read-only key，不复用 trade key；
- key 只在对应 P3/P5 门禁批准后创建。Testnet key 不能访问 mainnet，Live key 不进入开发机、
  Git、镜像、环境变量、命令行、日志或 Audit DB；
- Linux host 上的 secret source 位于仓库和 compose 目录之外，归 root 或专用 service account
  所有，权限不宽于 `0400`，以只读文件挂载到容器 `/run/secrets/`；容器内组件只读取自己的
  最小 secret；
- 撤销、轮换、异常告警、host 加固、只读根文件系统和实际 secret mount 由 P3-07/P3-08 的
  runbook 与部署测试验证；Live Canary 前重新核对账户资格、服务条款、端点和 key 权限。

Docker Compose 的文件挂载本身不是加密 Secret Manager。若目标 host 不能提供受控文件权限、
磁盘保护和备份排除，P3 部署门禁必须改用外部 Secret Manager；不得退化为把 key 写入 `.env`
或仓库配置。

## 5. Capability Matrix 审查

影响下单或对账的关键项均有明确分类：

- Market/Limit、Post-only/IOC/FOK、`orderLinkId`、订单/成交查询与私有订单流已标记支持；
- create/cancel ack 非终态、partial fill 累计成交与历史保留窗口已有保守处理；
- SpotTrade/Withdrawal 权限分离和 IP binding 已确认；
- Freqtrade Bybit spot `stoploss_on_exchange` 明确不支持，采用 bot-managed stoploss 并保留
  离线高风险，不存在“未知即按支持处理”；
- tick size、minOrderAmt、数量上限和 rate limit 是运行时动态值，明确禁止硬编码，由 P3-06
  Contract Harness 和 Live preflight 查询。

结论：Phase 0 capability matrix 没有未分类的下单/对账关键项。该结论只证明设计信息完整，
不证明锁定容器、Testnet 或生产写路径已实测。

## 6. Final Holdout 与 Trial 初始状态

Final Holdout：

- 固定区间 `[2025-07-01T00:00:00Z, 2026-07-01T00:00:00Z)`；
- `regime-manifest.yaml` 状态为 `SEALED_UNREAD`、`access_count=0`；
- 本 gate 没有下载目标数据、读取 holdout 或查看候选收益；
- 只有 P2-01～P2-06 完成并获得独立批准后，P2-07 才能读取一次。

Trial 初始状态：

- Strategy Card 固定 4h baseline 1 次、entry perturbation 4 次、exit perturbation 4 次、stop
  perturbation 4 次、1d baseline robustness 1 次，共 14 次上限；
- `cartesian_product_allowed=false`，禁止 Hyperopt，失败 trial 必须保留；
- 当前已执行/登记结果为 0，仓库不存在回测结果或 experiment result artifact；
- P2-05 使用 `experiment.schema.yaml` 建立逐 trial 实例，不得改变上述预算或删除失败记录。

机器可复核事实由 `tests/unit/test_phase0_gate.py` 覆盖。

## 7. 反方检查与残余风险

### 7.1 “所有文档和测试都存在，因此 gate 可以通过”

不成立。文档完整性是必要条件，不是独立评审。P0-05～P0-07 的作者与本 gate 自查者均为
Codex，违反“实现者不能自行批准”的职责约束。

### 7.2 “后续已有验证任务，因此当前没有 blocker”

只部分成立。Docker、数据下载、watchdog、outbox 和 Testnet 实测可以按计划延期，因为任务、
失败动作和后续门禁已明确；独立评审不能延期到进入 Phase 1 之后，否则 Scope Frozen 失去意义。

### 7.3 仍需关注但不属于当前设计未知

- Bybit 资格、API、精度与限频会变化，必须在对应阶段重新核对；
- bot-managed stoploss 在主机/网络离线期间无交易所托管保护；
- PostgreSQL、secret mount、RPO/RTO 和 outbox 阈值尚未运行实测；
- 任何 review 后修改风险、holdout、strategy 或 Runtime/Audit 所有权的意见都会使相关产物重新
  回到 `IN_PROGRESS`，不能直接批准旧版本。

## 8. 阻塞事实与解除条件

| ID | 阻塞事实 | 解除条件 | 解除后记录 |
|---|---|---|---|
| B-01 | P0-05 无独立复核记录 | 项目所有人逐项确认数据区间、split、regime 与 holdout 合同，或提出修改 | 在 ADR-0005 记录 reviewer、日期、commit 和结论 |
| B-02 | P0-06 无独立复核记录 | 项目所有人确认 NAV/mark、阈值、三态、TTL 与 Kill runbook，或提出修改 | 在 ADR-0006 记录 reviewer、日期、commit 和结论 |
| B-03 | P0-07 无独立复核记录 | 项目所有人确认 DB 所有权、outbox 阈值、writer/replay 边界，或提出修改 | 在 ADR-0007 记录 reviewer、日期、commit 和结论 |
| B-04 | Phase 0 总门禁无独立结论 | B-01～B-03 解除后，项目所有人确认本文没有扩大 MVP 范围 | 本文更新为 DONE，记录 reviewer、日期和审查 commit |

解除必须是针对固定 commit 的明确结论。“继续开发”或普通提交授权不自动等于批准风险、数据、
Audit 或 Scope Frozen 门禁。任一评审意见要求实质修改时，先修改对应 ADR/schema、重新测试并
再次评审。

## 9. 当前允许与禁止事项

允许继续已有授权范围内的无密钥、无网络、无交易写权限的确定性核心与文档/测试工作。

P0-08 解除前禁止：

- 把 P0-05、P0-06、P0-07 或 P0-08 标记为 DONE；
- 启动认证交易所接入、创建真实/Testnet key 或保存 secret；
- 实现可执行 Freqtrade adapter、Paper/Live 配置或第二交易写路径；
- 下载/读取 Final Holdout，或运行未登记参数 trial；
- 用本次自查声称 Phase 0 已独立评审通过。
