# 策略研究与验证规范

## 1. 研究方法

alphaMind 不从“哪个指标最赚钱”开始，而从可解释的市场现象开始：

```text
前提 -> 数据证据 -> 可交易假设 -> 因子代理 -> 策略规则 -> 样本外验证 -> 风险边界
```

每个研究假设必须回答：

1. 谁在为什么付钱；
2. 收益来自 alpha、beta 还是风险溢价；
3. 为什么这个现象不会被交易成本完全吃掉；
4. 什么市场状态下会失效；
5. 最坏亏损如何发生；
6. 哪项数据能证伪该假设。

## 2. 首批候选策略与顺序

第一阶段固定研究一个简单趋势基线，优先选择 4h Donchian Breakout；如果最终选择 Dual Moving Average，必须在 Phase 0 记录替代理由，不能同时搜索两个策略后只保留表现更好的结果。

### 2.1 趋势基线

Donchian 基线使用已完成 candle，并对滚动窗口滞后一个周期：

```text
入场：当前已完成 candle 的收盘价突破过去 N 根已完成 candle 的最高价
退出：当前已完成 candle 的收盘价跌破过去 M 根已完成 candle 的最低价
方向：现货 long/flat
周期：4h；1d 仅用于稳健性复测
仓位：由风险引擎根据 NAV、止损距离、波动率和风险预算确定
```

第一条策略的目的不是最大化收益，而是用最少参数验证数据、回测、风险、dry-run 和实验复现链路。进入下一阶段前必须说明：收益是否集中于少数行情、震荡期连续止损是否可接受、成本压力后期望是否仍为正，以及相邻参数是否给出同方向证据。

信号和成交时序必须冻结为：

```text
T：4h candle 完成
T + ε：使用已完成 candle 生成信号并进行风险审批
Backtest：最早按下一根 candle 的 open 或明确的下一时点成交模型进入
Dry/Live：在下一根 candle 到来并完成计算后，按下一次可交易报价提交订单
```

禁止使用产生信号的同一 candle close 作为无滑点成交价。自动测试必须证明 rolling high/low 已滞后一根、signal candle 不成交，并检查 custom pricing callback 不会重新引入同 candle 成交偏差。

### 2.2 第二条候选策略

只有趋势基线完成独立验证后，才研究“受 regime 约束的流动性事件均值回归”：

1. BTC/ETH 在短时间内发生显著下跌；
2. 成交量和波动率同步放大；
3. 卖压开始衰减；
4. 价格停止创新低并出现右侧确认；
5. 当前不处于强趋势下跌或极端流动性枯竭；
6. 在有限持仓时间内博取流动性恢复。

候选特征：

- 短期收益率 z-score；
- 成交量冲击；
- Realized volatility；
- 与 VWAP 或移动均线的距离；
- 下跌速度和加速度；
- Funding Rate；
- Mark Price 与 Index Price 偏差；
- 后续阶段再引入 Order Book Imbalance 和 Open Interest。

模型从线性打分、Logistic Regression 或 Ridge 开始。简单模型不能形成稳健结果时，不允许直接用更复杂模型掩盖问题。

这些特征只是待验证候选项。Funding Rate、Mark Price 和 Index Price 属于衍生品数据，不能成为第一阶段现货趋势基线的强制依赖。

### 2.3 Regime 分类

至少区分：

- 正常震荡；
- 上涨趋势；
- 下跌趋势；
- 极端高波动；
- 流动性异常；
- 数据或交易所异常。

均值回归策略只允许在经过验证的震荡和流动性恢复状态中运行。强趋势下跌、数据异常和交易所异常时必须输出 `HALT`。

## 3. 数据要求

第一阶段：

- BTC/USDT、ETH/USDT；
- 趋势基线使用 4h，1d 用于稳健性复测；
- 15m、1h 只在后续均值回归研究中引入；
- Spot OHLCV；
- 合约研究立项后才额外引入 Mark Price、Index Price 和 Funding Rate；
- Phase 0/1 在查看策略结果前冻结具体起止日期、数据来源和文件 hash；
- 使用预注册的 regime manifest 描述趋势、震荡、崩盘和流动性异常区间，不使用事后主观的“两个牛熊周期”作为数据合同；
- 原始数据、清洗数据和特征数据分开存储。

质量检查：

- 时间戳严格递增；
- 无重复 candle；
- 缺失区间有明确标记；
- 价格和成交量异常不能被静默填充；
- 不同来源数据的发布时间必须遵守 point-in-time 原则；
- 研究期间下架的交易对不能被简单删除。

## 4. 时间序列验证

禁止随机拆分金融时序。开发阶段与最终确认阶段必须严格分开。

### 4.1 开发阶段 Walk-Forward

```text
rolling 或 expanding train：建议从 24 个月起步
validation：建议从 6 个月起步
每次滚动：建议 6 个月
```

24/6/6 只是容量规划起点，不是所有策略的固定答案。规则型趋势策略如果没有训练过程，train 窗口主要用于参数选择和风险估计；均值回归或统计模型则必须考虑标签重叠、purge/embargo 和模型重新训练频率。所有 Walk-Forward 结果都属于开发证据，可以用于选择，但每次选择必须进入 trial registry。

### 4.2 一次性 Final Holdout

在查看候选策略结果前预先冻结一个连续、完全未见的 final holdout，并记录起止日期、选择规则和文件 hash。候选策略、参数、仓位规则和成本模型全部冻结后只运行一次。

final holdout 的长度由 4h 信号数、独立市场事件数和统计不确定性决定，不机械固定为 6 或 12 个月。结果一旦被用于修改当前策略，该区间永久降级为开发数据；新候选必须使用另一段此前未见且预注册的数据，不能反复滚动“最终测试集”。

### 4.3 Stress Slices

牛市、熊市、横盘、快速崩盘和流动性异常使用预注册 regime manifest 形成 stress slices。它们用于检查失败路径，不用于事后挑选一个表现更好的 holdout。若 stress slice 结果参与调参，该 slice 属于开发/验证数据；只有在候选冻结后首次运行的未见 slice 才具有最终确认意义。高资金费率阶段只适用于合约策略。

## 5. 成本和成交模型

历史回测至少加入：

- Maker/Taker Fee；
- Bid-ask Spread；
- Slippage；
- 最小下单量、价格精度和数量精度。

Funding Fee 只适用于合约研究。标准 candle 回测无法可靠证明以下执行行为，必须按证据类型分别验证：

- 延迟和 API 超时；
- 部分成交；
- 限价单未成交；
- 撤单失败或取消被拒；
- 下单结果未知和重启后对账。

Deterministic Replay/Fault Injection 只能验证内部状态转换和恢复逻辑；独立 Testnet Contract Harness 只在目标交易所支持时验证真实 API 参数和订单查询。Shadow/Replay 不能声称已经验证交易写权限或真实接单。若没有可信 Testnet，相关生产写路径只能由受限 Live Canary 首次验证。

挂单免手续费不等于零成本。挂单可能承担逆向选择，因此必须按真实限价单成交逻辑验证，不能假设每个触价订单都能成交。

## 6. 反作弊检查

必须自动执行：

- Freqtrade `lookahead-analysis` 或等价检查；
- 信号 candle 与成交 candle 分离测试；
- 特征逐列的未来信息扫描；
- 禁止全样本归一化；
- 禁止使用回测结束后才能确定的名单；
- 训练、验证、测试数据隔离；
- 参数和实验次数留痕；
- 不同交易所同标的数据复测；
- 参数 ±10% 和 ±20% 扰动；
- 费用 2 倍、滑点 3 倍压力测试；
- 延迟、缺失行情、API 超时和拒单模拟；
- 单日下跌 10% 和 20% 的尾部情景。

## 7. 评估指标

禁止只报告年化收益率和胜率。每份报告至少包含：

- 样本内和样本外净收益；
- 现金基准；BTC Buy-and-Hold、ETH Buy-and-Hold 分别计算；可选增加初始 50/50、期间不再平衡的组合基准；
- 简单均线策略基准；
- Maximum Drawdown；
- Sharpe、Sortino、Calmar；
- Profit Factor；
- 每笔期望收益；
- 平均盈利、平均亏损和盈亏比；
- 最大单笔亏损；
- 最差 1% 交易或 CVaR；
- Turnover；
- Time Under Water；
- 资金暴露比例；
- 手续费和资金费率占毛收益比例；
- Top 5 单笔交易和单一标的对总收益的贡献；
- 按 market regime 拆分的表现。

负偏态策略必须重点报告尾部亏损，不能用高胜率掩盖一次亏损吞噬多周利润的问题。

## 8. 策略晋升门槛

下面的阈值是保守工程起点，项目可以根据实际风险预算调整，但调整必须留下决策记录。

### 8.1 Backtest -> Paper

- Lookahead 和数据泄漏检查通过；
- 多个 Walk-Forward 窗口给出同方向证据；窗口数量、正收益比例和置信区间必须随报告披露；
- 扣除 2 倍费用和更高滑点后仍为正期望；
- 参数在合理区间形成稳定平台，而非只有一个最优点；
- 披露 Top 5 交易贡献、最大单笔贡献和盈利集中度，但不使用统一的 30% 否决线；
- 根据策略频率报告样本外交易数、独立市场事件数、bootstrap 置信区间和 Minimum Track Record Length；不把 150–200 笔作为所有策略的统一充分条件；
- 记录全部候选策略、参数组合和人工筛选次数，并使用 Probabilistic Sharpe、Deflated Sharpe 或等价方法校正多重测试和非正态收益偏差；
- 不包含马丁、无限补仓或取消止损；
- 最大回撤在预设风险预算内；
- 相对现金和 Buy-and-Hold 的收益、回撤和资金暴露差异可以解释；不强制要求低暴露策略在每个牛市窗口都跑赢 Buy-and-Hold。

### 8.2 Paper -> Live Canary

- 候选版本冻结后连续运行至少 90 天；任何影响信号、仓位、退出或成本的修改都会重新开始计时；
- 达到 Strategy Card 预先规定的最小有效信号数、成交数和独立市场事件数；低频策略不使用统一的 100 笔门槛；
- 没有重复下单和重启丢仓；
- Freqtrade dry-run 无不可解释的内部状态差异；Replay/Fault Injection 的对账场景全部通过；存在 Testnet Contract Harness 时，其真实订单查询差异为零或能按预定流程处置；
- 实际滑点、成交率和费用进入数据库；
- 除 Kill Switch 外不允许人工改变策略；
- 任何人工干预都会使当前评估窗口失效；
- 至少经历一次明显波动行情；如果 90 天内没有覆盖预设压力状态，则继续运行而不是自动晋升；
- Freqtrade dry-run、Deterministic Replay/Fault Injection、可选 Testnet Contract Harness 的证据必须分别保存；没有 Testnet 时必须明确 Live Canary 首次验证生产写路径的残余风险。

### 8.3 Live Canary -> Scale

风险会计、仓位公式、UTC 边界、外部现金流调整和熔断动作必须遵守 [Freqtrade MVP Runtime Contract](freqtrade-mvp-runtime-contract.md)。`0.25%` 是入场时的计划风险预算，不是最大实际亏损保证。

初始只投入计划资金的 5%–10%：

- 第一阶段不使用杠杆；
- 单笔风险不超过 NAV 的 0.25%；
- 单日亏损达到 1% 自动暂停；
- 单周亏损达到 3% 进入人工复核；
- 从历史峰值回撤达到 5% 自动停止策略；
- 不允许逆势扩大原始风险预算；
- 不允许为了避免强平临时追加风险；
- 连续稳定 60–90 天后才允许分阶段扩容。

## 9. 实验可复现性

每次实验必须保存：

```text
experiment_id
hypothesis_id
strategy_commit
strategy_config_hash
data_version
feature_version
train_timerange
validation_timerange
test_timerange
cost_model_version
random_seed
metrics
artifacts
review_result
```

AI生成的实验与人工实验使用完全相同的记录要求。任何无法复现的漂亮结果都不能进入下一阶段。
