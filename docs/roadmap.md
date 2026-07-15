# alphaMind 开发路线图

## 1. 路线图结论

alphaMind 先建设可复现的研究与 dry-run 基线，再验证交易所订单生命周期，最后才使用小额真实资金。第一条策略固定为简单趋势基线；受 Regime 约束的均值回归、多策略和衍生品均属于后续独立研究。

MVP 默认复用 Freqtrade，不默认自研完整交易引擎：

- Freqtrade：数据下载、回测、lookahead 检查、策略运行和 dry-run；
- Freqtrade 内部 CCXT 能力：第一阶段交易所统一接入；
- 直接使用 CCXT/CCXT Pro 或交易所原生 API：只在形成明确能力缺口并通过架构评审后引入；
- Freqtrade Runtime DB：`Trade`、`Order`、open position 和重启恢复的唯一内部运行状态；
- PostgreSQL 或独立 schema：alphaMind Research/Audit DB，只保存实验、风险快照、人工干预和审计事件；
- Redis：只有出现跨进程协调、锁或任务队列的真实需求后才引入；
- Parquet：版本化 OHLCV 和特征快照；
- Docker Compose：固定研究与运行环境；
- Telegram：告警和只读状态查询；
- Prometheus + Grafana 或轻量 Dashboard：运行监控。

不能一边声称“优先复用成熟框架”，一边在 MVP 默认实现另一套 Order Manager、数据库模型和交易服务。自研执行系统必须遵守 [系统架构](architecture.md) 中的单独立项门槛。

## 2. 当前基线与待决事项

已确定的默认基线：

| 项目 | 基线 |
|---|---|
| 标的 | BTC/USDT、ETH/USDT |
| 市场 | 单一交易所现货 |
| 方向 | long/flat |
| 杠杆 | 不使用 |
| 主周期 | 4h；1d 仅用于稳健性复测 |
| 第一条策略 | 4h Donchian 20/10、ATR(20) × 2 stop |
| 第二条策略 | Regime-filtered Mean Reversion，趋势基线通过后独立研究 |
| 运行框架 | Freqtrade 优先 |
| 开发目标交易所 | Bybit 国际版现货 |
| 计划资金与停止边界 | 约 450–500 USDT；项目级最大损失取资本基线 10% 与固定 45 USDT 中的更严格者 |

进入 Phase 1 前必须决定并记录：

- 锁定版本下的 Bybit/Freqtrade 兼容性；
- Bybit Testnet/Contract Harness、订单类型、限频和历史数据能力；
- 风险会计、动态精度和 bot-managed stoploss 的离线残余风险；
- 责任人、告警接收人和紧急人工操作权限；
- 部署区域、密钥方案、备份和恢复责任；
- ADR-0004 已冻结的 Donchian Strategy Card 与后续实现保持同源。

常驻地区和账户资格不是策略或风险代码分支。它们在 Live Canary 前作为外部准入检查重新核对；届时不满足只会阻止 live，不会自动改写研究标的或切换交易所。

## 3. 建议目录

```text
alphaMind/
├─ data/
│  ├─ manifests/
│  ├─ quality/
│  └─ schemas/
├─ research/
│  ├─ hypotheses/
│  ├─ notebooks/
│  ├─ experiments/
│  └─ strategy_cards/
├─ strategies/
├─ risk/
├─ monitoring/
├─ configs/
│  ├─ backtest/
│  ├─ dry_run/
│  ├─ replay/
│  ├─ testnet_contract/     # 仅在目标交易所支持时使用
│  └─ live/
├─ tests/
├─ user_data/               # Freqtrade 运行目录
├─ execution/               # 仅在自研执行系统获批后创建
├─ ai/                      # 仅在基础研究链路稳定后按需创建
└─ docker-compose.yml
```

`backtest`、`dry_run`、`replay`、可选 `testnet_contract` 和 `live` 配置、数据库和密钥必须隔离。禁止只依赖一个布尔开关把研究环境切换成实盘。

## 4. 阶段总览

| 阶段 | 参考周期 | 核心目标 | 是否允许资金 |
|---|---:|---|---|
| Phase 0：决策冻结 | 约 1 周 | 固定范围、风险预算、交易所和验收口径 | 否 |
| Phase 1：研究底座 | 2–4 周 | 数据、基准和实验可复现 | 否 |
| Phase 2：趋势策略 | 3–6 周 | 形成第一条 Backtest Qualified 策略 | 否 |
| Phase 3：Paper-ready 工程 | 3–6 周 | dry-run、订单验证、监控和恢复能力就绪 | 否 |
| Phase 4：冻结版本 Paper | 至少 90 天 | 验证连续运行和预注册事件门槛 | 否 |
| Phase 5：Live Canary | 60–90 天 | 小额真实资金验证执行与运维 | 少量 |
| Phase 6：第二策略与组合 | 不预设 | 均值回归和确定性组合 | 单独审批 |
| Phase 7：高级扩展 | 不预设 | 微观结构、衍生品和多交易所 | 单独审批 |

周期只用于容量规划，不是上线承诺。若策略被证伪、候选版本发生实质变更或压力状态尚未覆盖，对应阶段自然延长。

## 5. Phase 0：决策冻结

参考周期：约 1 周。

交付物：

- 一份版本化的项目约束记录；
- Bybit 国际版、BTC/USDT、ETH/USDT、现货和策略选择记录；
- 完成并评审 [Freqtrade MVP Runtime Contract](freqtrade-mvp-runtime-contract.md)，明确 callback、watchdog、Runtime DB 和 Audit DB 的所有权；
- 目标交易所 capability matrix，包括 Testnet/test-order、`client_order_id`、订单查询、历史保留、精度、限频和手续费币种；
- 总资金、单笔、单日、单周和回撤风险预算；
- Historical Backtest、Dry-run、Sandbox/Shadow、Live Canary 的验证矩阵；
- 策略晋升状态机和变更后重新计时规则；
- 实验记录 schema 和 Strategy Card 模板；
- NAV、mark price、UTC 日周边界、外部现金流、高水位和熔断动作的合同定义；
- API Key 权限、密钥轮换和紧急撤销规范；
- 责任人、告警升级路径和人工干预边界；
- final holdout、数据起止日期、regime manifest 和基准组合定义；
- Freqtrade MVP 与自研执行系统的职责决策。

完成标准：所有待决事项有明确结论、责任人和可复核文档；不能只以“团队没有歧义”作为完成证据。

## 6. Phase 1：研究底座

参考周期：2–4 周。

工作项：

- 固定 Python、Freqtrade、CCXT、依赖包和容器版本；
- 按 Phase 0 冻结的起止日期下载 BTC/USDT、ETH/USDT 4h 历史 OHLCV，并建立 1d 复测数据；
- 保存数据来源、请求范围、下载时间、文件哈希和交易所元数据；
- 将下载后的源数据作为不可变快照，清洗数据和特征数据另行版本化；
- 检查重复、缺口、乱序、时间戳、异常价格和异常成交量；
- 建立现金、BTC Buy-and-Hold、ETH Buy-and-Hold、可选初始 50/50 不再平衡组合，以及简单均线基准；
- 建立预注册 regime manifest，固定 stress slices 的日期和分类规则；
- 建立最小实验记录和报告，记录 commit、配置、数据和环境 hash；
- 使用固定环境重复运行基准，检查指标和交易结果一致性。

完成标准：给定相同代码、配置、数据和锁定环境，可以重复生成一致的基准结果；数据问题不得被静默填充。

## 7. Phase 2：趋势策略与审计

参考周期：3–6 周。

工作项：

- 实现 Phase 0 选定的一个趋势基线，不并行搜索多个策略后择优汇报；
- 对 rolling high/low 或 moving average 做 point-in-time 和 candle 完成检查；
- 冻结 `T candle 完成 -> T+ε 生成信号 -> 下一根 candle/下一次报价成交`，并增加 signal candle 不成交测试；
- 运行 Freqtrade `lookahead-analysis`、逐列未来信息检查和必要的 recursive analysis；
- 使用 rolling/expanding train + validation 进行开发；候选冻结后只运行一次预注册 final holdout；
- 使用预注册 stress slices 检查失败路径，不通过事后选择改变 final holdout；
- 建立手续费、spread、slippage 和参数扰动压力测试；
- 保存全部策略版本、参数组合、失败实验和人工筛选次数；
- 报告 bootstrap 置信区间、盈利集中度和多重测试校正结果；
- 定义风险引擎定仓输入、硬退出和最大持仓时间。

Research Agent、Code Agent 和 Audit Agent 不属于本阶段完成条件。确定性脚本与人工评审足以完成的工作不为了展示 AI 而增加 Agent。

完成标准：满足 [策略研究与验证规范](strategy-research-and-validation.md) 的 `Backtest -> Paper` 门槛，最终 holdout 未参与任何策略选择或调参。

## 8. Phase 3：Paper-ready 工程

参考周期：3–6 周。

工作项：

- 启动使用独立 Runtime DB 和配置的 Freqtrade dry-run；
- 验证实时信号与回测信号在相同已完成 candle 上一致；
- 按 Runtime Contract 映射 `custom_stake_amount`、entry confirmation、stoploss、protections 和最大持仓时间；
- 实现只读 risk watchdog 和版本化 RiskSnapshot；callback 只读取缓存，快照陈旧或缺失时拒绝新入场；
- 验证 NAV、外部现金流调整、日亏、周亏和回撤熔断；触发后禁止新入场但保留安全退出；
- 隔离 Freqtrade Runtime DB 与 alphaMind Audit DB，验证单一写入者和恢复顺序；
- 明确 Freqtrade dry-run 不能证明的交易所行为；
- 使用 Deterministic Replay/Fault Injection 验证超时后下单结果未知、partial fill、到期、撤单拒绝、重启、重复事件和状态对账；
- 目标交易所支持时，使用独立 Testnet Contract Harness 验证 API Key、下单参数、精度、`client_order_id` 和订单查询；Freqtrade 本身不运行 sandbox account；
- 没有可信 Testnet 时，记录 Live Canary 首次验证生产写路径的残余风险和审批人；
- 建立心跳、数据新鲜度、API 限频、订单异常和状态差异告警；
- 完成密钥最小权限、备份恢复、时钟同步和操作 runbook；
- 固定 Paper 候选的策略 commit、配置 hash、数据版本和依赖环境。

如果 Freqtrade 无法满足已确认的执行需求，先形成能力缺口与迁移评审，再决定是否实现 `ExchangePort` 和自研 Order Manager。自研完成后必须重新执行本阶段全部验证。

完成标准：候选版本在故障注入和重启测试中无重复风险暴露、无不可解释状态差异，并达到开始 90 天 Paper 计时的条件。

## 9. Phase 4：冻结版本 Paper

观察期：至少 90 天，从候选版本冻结且 Phase 3 通过后开始。

要求：

- 不修改信号、参数、仓位、退出和成本逻辑；实质修改后重新计时；
- 达到 Strategy Card 预注册的最小有效信号、成交和独立市场事件数量；
- 至少经历一次预定义的明显波动状态；没有覆盖则延长观察期；
- 分开保存 Freqtrade dry-run、Replay/Fault Injection 和可选 Testnet Contract Harness 证据；不得用前两者宣称真实交易写路径已通过；
- 没有重复下单、丢仓或无法自动解释的状态差异；
- 保存成交率、模拟滑点、费用、告警、重启和所有人工干预；
- 除 Kill Switch 和安全减仓外，人工干预会使当前评估窗口失效。

完成标准：时间门槛、预注册事件门槛、运行一致性和压力状态覆盖同时满足，才可申请 Live Canary。

## 10. Phase 5：Live Canary

观察期：60–90 天，不包含准备和修复时间。

工作项：

- 使用独立子账户；
- API Key 禁止提现并设置 IP 白名单；
- 初始仅投入计划资金的 5%–10%；
- 不使用杠杆；
- 入场时计划风险预算不超过 NAV 的 0.25%，不把它表述成最大实际亏损保证；
- 按 Runtime Contract 的 UTC、mark-to-market 和外部现金流调整口径，单日亏损达到 1% 时禁止新入场；
- 同一口径下单周亏损达到 3% 时进入人工复核；
- 相对现金流调整后高水位的回撤达到 5% 时触发 Kill Switch 和预定义安全处置；
- 固定策略 commit、配置 hash 和依赖环境；
- 启用全量审计、交易所对账和 Kill Switch；
- 比较实盘与 Paper 的成交率、费用、滑点、延迟和收益差异。

完成标准：连续 60–90 天稳定运行，所有资金变化均能由策略、费用、滑点或已记录运行事件解释；任何未解释差异都会阻止扩容。

## 11. Phase 6：第二策略与组合

只有前述阶段通过后，才按以下顺序评估：

1. Regime-filtered Mean Reversion；
2. 趋势与均值回归的确定性组合和风险预算；
3. 按实际重复劳动逐个引入 Research/Audit/Review Agent。

每项扩展都建立独立 Strategy Card，并重新通过完整晋升流程。不得继承其他策略的样本外、Paper 或 Live 结论。

## 12. Phase 7：高级扩展

只有 Phase 6 中的单策略和组合能力分别通过验证后，才按以下顺序评估：

1. Order Book、Open Interest 和逐笔成交特征；
2. 永续合约及 Funding/Basis Carry；
3. BTC-ETH 统计套利和多腿状态机；
4. 多交易所、跨所和期权策略。

这些能力不能继承现货 MVP 的 execution、margin、数据或风险结论，必须重新完成 capability matrix、故障注入和完整晋升流程。

## 13. 里程碑验收表

| 里程碑 | 核心证据 | 是否允许资金 |
|---|---|---|
| Scope Frozen | Runtime Contract、capability matrix、交易所、风险预算、数据和 holdout 已决策 | 否 |
| Research Ready | 数据可复现、基准可运行 | 否 |
| Backtest Qualified | 样本外、成本、集中度和多重测试审计通过 | 否 |
| Paper Ready | dry-run、订单验证、故障恢复和告警通过 | 否 |
| Paper Qualified | 冻结版本至少 90 天，并满足事件与压力状态门槛 | 否 |
| Live Canary | 小额、无杠杆、严格熔断 | 少量 |
| Scale Qualified | 60–90 天稳定实盘、无未解释差异 | 分阶段增加 |

## 14. 禁止事项

- 提示 AI“持续修改直到年化达到指定比例”；
- 根据同一测试集反复修改策略；
- 只展示最好参数、最好策略或最好时间窗口；
- 只看胜率和累计收益；
- 让 LLM 实时自由下单或持有提现权限；
- 马丁、无限补仓和亏损后扩大杠杆；
- Freqtrade dry-run 或测试网盈利后直接切换实盘；
- 因没有成交而放宽成交假设；
- 把人工救仓后的收益记为策略收益；
- 在趋势基线尚未验证前实现复杂 Regime 模型、多腿套利或复杂 UI；
- 在没有能力缺口和迁移测试时自研完整交易引擎；
- 在 Paper 观察期内修改候选逻辑却不重新计时。

## 15. 参考资料

社区讨论：

- [量化交易实盘记录与答疑](https://linux.do/t/topic/2208615)
- [Binance 与 Deribit 跨所套利项目讨论](https://linux.do/t/topic/2323516)
- [Agent 编写量化策略的过拟合讨论](https://linux.do/t/topic/2260697)
- [AI量化回测成功但模拟盘失败案例](https://linux.do/t/topic/1950154)
- [基于 Backtrader 的量化平台](https://linux.do/t/topic/1397763)
- [QuantDinger AI量化平台](https://linux.do/t/topic/1507020)
- [AI辅助生成 Freqtrade 策略的实盘记录](https://linux.do/t/topic/2146442)
- [OpenClaw 交易 Skill](https://linux.do/t/topic/1671332)
- [LLM自主交易 day1 记录](https://linux.do/t/topic/2147706)
- [BTCUSDT 开源工具讨论](https://linux.do/t/topic/1806868)

官方与研究资料：

- [CCXT Manual](https://github.com/ccxt/ccxt/wiki/manual)
- [CCXT Pro](https://docs.ccxt.com/docs/pro)
- [Freqtrade 数据下载](https://www.freqtrade.io/en/stable/data-download/)
- [Freqtrade Lookahead Analysis](https://www.freqtrade.io/en/stable/lookahead-analysis/)
- [Freqtrade Backtesting](https://www.freqtrade.io/en/latest/backtesting/)
- [Freqtrade Configuration 与 Dry-run](https://docs.freqtrade.io/en/latest/configuration/)
- [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [Binance-Deribit 套利项目](https://github.com/beijingcao/binance-deribit-btc)
- [Backtrader 平台项目](https://github.com/faryhuo/backtrader)
- [QuantDinger 项目](https://github.com/brokermr810/QuantDinger)
