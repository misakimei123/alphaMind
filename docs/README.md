# alphaMind 设计文档

> **项目目标已于 2026-07-18 重新定基线**：alphaMind 的 MVP 改为“每 30 分钟读取账户、行情与新闻，由 AI 生成结构化交易建议，经 Telegram 人工批准后自动执行”，并增加配置化 BTC/ETH/SOL/HYPE、现货和 Bybit USDT 永续合约支持。旧研究内容已明确标记为历史基线；任务、状态和下一步始终以 [完整开发计划](development-plan.md) 为唯一依据。

alphaMind 的目标是构建一套**AI 生成交易动作、项目所有人通过 Telegram 授权、确定性程序负责风控并由 Freqtrade 自动执行**的个人加密货币交易系统。AI 可以进入资金决策链提出开仓、加仓、减仓和平仓建议，但不能绕过人工授权或确定性风险边界。

本文档目录记录项目当前的目标架构和实施规范。仓库已经包含离线 Donchian、风险定仓、账户损失门禁、版本锁，以及冻结的数据/Final Holdout、风险会计和 Audit/Replay 合同，但尚未包含可运行的 Freqtrade bot、认证交易所接入或订单写路径。

## 核心原则

1. **AI 提议，用户授权，系统执行。** Telegram 批准是普通 AI 交易动作进入执行链的授权点。
2. **AI 不持有交易权限。** AI 与 Telegram Bot 都不能持有 Bybit key，也不能绕过风险引擎或直接下单。
3. **决策、风控和执行分层。** AI 生成结构化 Action，确定性风控计算数量和杠杆，Freqtrade 负责订单生命周期。
4. **CCXT 是交易所适配层，不是完整交易系统。** 普通能力优先使用 CCXT/CCXT Pro，高级功能通过交易所原生适配器补充。
5. **不同验证环境解决不同问题。** 历史回测、Freqtrade dry-run、确定性事件回放、可选 Testnet Contract Harness 和小额实盘不能互相替代。
6. **风险指标优先于收益率。** 高胜率、短期盈利和漂亮回测都不能证明策略长期有效。
7. **所有状态可恢复、所有行为可审计。** 重启后必须对账，AI建议、人工操作、配置变化和订单事件必须留痕。

## 文档导航

- [完整开发计划](development-plan.md)：后续开发的规范性执行基准，包含任务依赖、产物路径、验收门禁、验证矩阵和变更流程。
- [系统架构](architecture.md)：模块边界、CCXT适配、AI职责、风控、执行状态机和安全设计。
- [Freqtrade MVP 运行合同](freqtrade-mvp-runtime-contract.md)：第一阶段组件映射、单一运行所有权、风险会计、数据库和交易所能力矩阵。
- [策略选型与顺序](strategy.md)：策略假设、优先级、失败环境和分阶段研究顺序。
- [策略研究与验证](strategy-research-and-validation.md)：第一条策略、数据、回测、样本外、压力测试和晋升门槛。
- [开发路线图](roadmap.md)：阶段规划、建议目录、交付物、完成标准和禁止事项。
- [ADR-0003 运行环境与版本锁定](decisions/0003-runtime-version-lock.md)：Python、Freqtrade、CCXT、source commit 和 Docker digest。
- [ADR-0004 首个策略](decisions/0004-first-strategy.md)：冻结 Donchian 参数、风险、试验预算、证据门槛和证伪条件。
- [ADR-0005 数据与 Final Holdout](decisions/0005-data-and-holdout.md)：冻结 Bybit OHLCV、Walk-Forward、不可变快照和一次性留出集合同。
- [ADR-0006 风险会计与 Kill Switch](decisions/0006-risk-accounting.md)：冻结 NAV、保守 mark、UTC 日周边界、RiskSnapshot 和停止/恢复动作。
- [Kill Switch 操作手册](runbooks/kill-switch.md)：定义 fail-closed、close-only、人工复核、持仓处置和恢复证据。
- [Runtime DB 恢复手册](runbooks/runtime-db-recovery.md)：定义环境/权限隔离、SQLite 整库备份恢复、PostgreSQL WAL/PITR、升级回退和恢复顺序。
- [ADR-0007 Audit 与 Replay](decisions/0007-audit-and-replay.md)：冻结 Runtime/Audit 所有权、outbox 背压、writer 幂等和 Replay 权限边界。
- [ADR-0010 DecisionContext 核心特征](decisions/0010-decision-context-core-features.md)：冻结 R2-05 OHLCV 来源、Donchian/ATR/EMA/成交量参数、point-in-time 和 fail-closed 合同。
- [Phase 0 Scope Frozen Gate](decisions/phase-0-gate.md)：逐项审查 P0 产物、设计缺口、capability、holdout 和 trial，并记录独立评审阻塞。

后续开发严格按 [完整开发计划](development-plan.md) 的 R0-R6 推进。Runtime Contract、策略规范、ADR 和审核意见只提供支撑语义；发生冲突时以开发计划为准。

## 项目定位

第一阶段的 alphaMind 是人工授权的 AI 交易系统：

- 默认每 30 分钟生成一个决策周期，周期可配置；
- 读取账户、spot/futures 持仓、订单、行情、funding、强平风险和配置化新闻；
- 初始 Instrument Registry 配置 BTC、ETH、SOL、HYPE，实际可用市场在启动时由 Bybit 动态确认；
- 支持 Bybit 现货和 USDT 线性永续，Spot/Futures 使用独立 Freqtrade 配置与 Runtime DB；
- AI 输出结构化动作，Telegram 按动作批准，ExecutionGateway 驱动 Freqtrade 自动执行；
- Donchian、ATR、EMA 等现有逻辑作为 AI 特征和传统对照基线；
- 止损、Kill Switch、最大损失、最大杠杆和敞口由确定性逻辑控制，不等待 30 分钟 AI 周期。

## 当前决策基线

以下内容作为后续文档和开发的统一默认口径：

| 项目 | 当前基线 |
|---|---|
| 市场 | Bybit 现货 + USDT 线性永续；Spot/Futures 隔离运行 |
| 标的 | Instrument Registry 配置；初始 BTC、ETH、SOL、HYPE |
| 方向 | 现货 long；合约按配置 long/short |
| 决策周期 | 默认 30 分钟，可配置；硬止损和风险动作持续生效 |
| 决策权威 | AI 结构化 Action + Telegram 人工授权 + 确定性风控 |
| 传统策略 | Donchian/ATR/EMA 作为特征和对照基线 |
| 研究和 dry-run | Freqtrade 2026.6；生产候选使用已固定 digest 的官方镜像 |
| 交易所接入 | Freqtrade 内部 CCXT 能力优先；确有差异时才增加原生适配 |
| AI | MVP 主链；不持有 key、不决定最终数量/杠杆、不绕过 Telegram 和风控 |
| 计划资金 | 约 450–500 USDT |
| 项目级最大损失 | 现金流调整资本基线的 10% 与固定 45 USDT，达到任一边界即停止 |

Bybit 国际版是当前开发和验证目标；常驻地区不参与代码路径选择。实际 Live Canary 前仍必须重新确认届时账户资格、服务条款、API 可用性和部署端点，未通过时只阻止 live，不回溯污染通用研究与风险逻辑。Freqtrade 版本、首个候选数据区间、风险会计和 Audit/Replay 边界已经冻结；部署细节与 Phase 0 总门禁仍需按后续任务形成版本化记录。

## 验证环境

alphaMind 将验证环境明确拆分为五层：

1. **Historical Backtest**：验证历史数据上的规则、成本敏感度和统计稳健性，不证明真实成交；
2. **Freqtrade Dry-run**：验证实时信号、配置和模拟订单行为，不证明交易所真实接单、partial fill 或离线期间状态变化；
3. **Deterministic Replay/Fault Injection**：验证内部状态机、超时、重复事件、partial fill、撤单竞争和恢复逻辑，不证明真实交易所写路径；
4. **独立 Testnet Contract Harness**：仅在目标交易所支持时验证 API Key、下单参数、精度和订单查询契约；Freqtrade 本身不运行 sandbox account；
5. **Live Canary**：验证小额真实资金下的生产写路径、成交和运行风险，不能用于继续调参美化策略。

如果目标交易所没有可信 Testnet 或测试下单端点，必须明确记录 Live Canary 是第一次完整验证生产写路径；Shadow 或 Replay 不能替代这项证据。

## 非目标

alphaMind 第一阶段不做以下事情：

- 跳过 Telegram 审批的完全自主实盘；
- AI 直接持有 Bybit key、取消止损、提高风险上限或自动提高杠杆；
- 山寨币高频或亚分钟交易；
- 马丁、无限补仓或亏损后扩大风险；
- 多交易所多腿套利；
- 多交易所、多腿、复杂期权和跨所组合；
- 为追求展示效果优先开发复杂前端；
- 以指定年化收益率为目标反复搜索参数。

这些能力只有在基础策略、风险控制、状态一致性和模拟盘验证全部成熟后，才允许作为独立研究方向评估。
