# alphaMind 策略选型与落地顺序

## 1. 结论

alphaMind 可以从复现公开策略开始，但“抄策略”的目的应是建立可验证的研究与交易工程链路，而不是直接复制参数投入实盘。

总体实施顺序：

1. `BuyAndHoldBenchmark` 与现金基准；
2. `DonchianTrendStrategy`；
3. 趋势基线通过 Backtest、Paper 和 Live Canary 后，再独立研究 `RegimeFilteredMeanReversionStrategy`；
4. 两类策略分别通过样本外验证后，再研究确定性的组合分配器；
5. 执行系统和保证金监控成熟后，再研究 `FundingCarryStrategy`。

其中，趋势跟踪和受市场状态约束的均值回归是前两类值得研究的策略，但不应同时进入 MVP。第一阶段只用简单趋势基线建立工程与验证链路；均值回归在该链路稳定后作为独立策略研究。Funding/Basis Carry、BTC-ETH 统计套利、横截面动量和订单簿策略属于更后续方向。

任何公开策略都只能视为待证伪的研究假设。策略是否进入实盘，必须遵守 [策略研究与验证规范](strategy-research-and-validation.md)，不能依据原作者收益截图、单次回测或 AI 生成的绩效结论决定。

## 2. 策略选型原则

首批策略应满足以下条件：

- 经济或行为假设可解释；
- 规则确定、参数较少；
- 不依赖未来数据或回测结束后才能获得的信息；
- 可在 BTC/USDT、ETH/USDT 等高流动性市场验证；
- 交易频率适中，手续费、滑点和延迟容易建模；
- 能明确说明适用的 market regime 和失败路径；
- 不依赖无限补仓、取消止损或提高杠杆维持账面盈利；
- 可以通过另一个交易所或另一段时间数据进行反向验证。

选型时不能以最高年化收益率排序。优先比较样本外稳定性、尾部风险、参数稳定区间、成本敏感度和策略是否优于简单基准。

## 3. 推荐优先级

| 优先级 | 策略 | 第一阶段用途 | 主要风险 | 建议阶段 |
|---|---|---|---|---|
| 1 | Time-Series Momentum / 趋势跟踪 | 建立第一个完整策略和执行基线 | 震荡期反复止损、策略衰减 | MVP |
| 2 | Regime-filtered Mean Reversion | 建立与趋势策略不同的收益来源 | 单边行情、插针、负偏收益 | 趋势基线通过后 |
| 3 | Funding/Basis Carry | 研究市场中性和多腿执行 | 保证金、强平、两腿不同步、交易所风险 | 执行层成熟后 |
| 4 | BTC-ETH Statistical Arbitrage | 验证组合信号和多腿订单状态机 | 相关性和协整关系失效 | 第二阶段 |
| 5 | Cross-sectional Momentum | 研究多币种组合 | 幸存者偏差、流动性和换手成本 | 第二阶段 |
| 6 | Order-book Imbalance | 研究微观结构因子 | L2 数据质量、延迟、撮合模拟 | 高级阶段 |

## 4. 第一优先级：趋势跟踪

### 4.1 交易假设

趋势跟踪不预测价格目标，而是假设价格趋势在有限时间内具有延续性：趋势形成后跟随，趋势失效后退出，并通过仓位控制限制错误方向造成的损失。

趋势策略通常是正偏收益结构：频繁的小额止损换取少数较大的趋势收益。它在震荡行情中容易连续亏损，因此不能只看全周期收益，必须按 market regime 拆分。

### 4.2 Donchian Breakout 基线

建议第一版使用收盘确认，避免在一根尚未完成的 candle 内使用不稳定信号：

```text
入场：当前收盘价突破过去 N 根已完成 candle 的最高价
退出：当前收盘价跌破过去 M 根已完成 candle 的最低价
仓位：根据 ATR 或 realized volatility 计算风险仓位
标的：BTC/USDT、ETH/USDT
周期：4h；1d 仅用于稳健性复测
方向：第一阶段 long/flat
```

实现时必须对 `rolling_high`、`rolling_low` 做一个周期的滞后，不能让当前 candle 同时参与阈值计算和突破判断。

### 4.3 未采用的 Dual Moving Average 备选

ADR-0004 已将首个策略固定为 Donchian。以下规则只保留为未来研究说明，不允许在当前候选验证中与 Donchian 择优：

```text
入场：fast EMA 上穿 slow EMA，并超过最小确认缓冲
退出：fast EMA 下穿 slow EMA，或触发风险退出
```

缓冲区用于减少均线附近的频繁切换，但不能不断增加 RSI、MACD、ADX 等过滤器追求回测收益。每增加一个条件，都必须说明它解决的失败场景，并单独完成消融实验。

### 4.4 必须验证的问题

- 扣除完整成本后，相对现金、BTC Buy-and-Hold、ETH Buy-and-Hold 和可选初始 50/50 不再平衡组合的收益、回撤与资金暴露差异是否可以解释；
- 收益是否只来自早期牛市或少数几笔交易；
- 震荡期连续止损是否超过风险预算；
- 参数在相邻范围是否形成稳定平台；
- BTC 和 ETH、不同交易所、不同时间窗口是否给出一致方向的证据；
- 波动率目标仓位是否在极端行情中造成追涨式扩大名义仓位。

## 5. 第二优先级：受 Regime 约束的均值回归

### 5.1 交易假设

均值回归假设：在没有持续趋势、市场流动性仍然正常的条件下，短期异常偏离可能向局部均值回归。它不是“价格跌了就买”，更不能用无限补仓对抗单边行情。

### 5.2 基础信号

第一版可选择 rolling z-score、Bollinger Band deviation 或 VWAP deviation 中的一种：

```text
deviation = (close - rolling_mean) / rolling_std

入场：
  deviation < -entry_z
  AND regime == RANGE_OR_RECOVERY
  AND data_quality == HEALTHY

退出：
  deviation 回到 exit_z 附近
  OR 达到最大持仓时间
  OR 触发硬止损
  OR regime 变为 TREND_DOWN / EXTREME_VOLATILITY / LIQUIDITY_STRESS
```

策略只能输出交易意图，最终仓位、日亏限制和 Kill Switch 由确定性风险引擎处理。

### 5.3 Regime Filter

至少区分：

- 正常震荡；
- 上涨趋势；
- 下跌趋势；
- 极端高波动；
- 流动性异常；
- 数据或交易所异常。

趋势强度、波动率分位数和流动性状态可以用于分类，但第一版应保持规则简单。分类器本身也必须遵守 point-in-time 原则，不能用未来完整行情回头标记当前状态。

### 5.4 风险重点

均值回归经常表现为大量小盈利和少数巨大亏损。评估时必须重点检查：

- 最大单笔亏损；
- 收益偏度和 Expected Shortfall；
- 最差 1% 交易；
- 单次亏损相当于多少笔平均盈利；
- 单边下跌、插针和流动性枯竭场景；
- 最大持仓时间失效后的退出质量；
- 手续费和滑点占毛收益的比例。

高胜率不能抵消负偏尾部风险。禁止使用 Martingale、亏损后扩大仓位、取消止损或无限延长持仓时间改善账面胜率。

## 6. 第三优先级：Funding/Basis Carry

### 6.1 交易假设

当永续合约 funding 或期现基差足以覆盖全部成本和尾部风险时，可以建立接近 delta-neutral 的组合，例如现货做多 BTC、永续合约做空等值 BTC。

研究判断必须基于净收益：

```text
expected_net_return =
    expected_funding
  + expected_basis_convergence
  - spot_fee
  - futures_fee
  - spread
  - slippage
  - borrow_cost
  - rebalance_cost
  - expected_tail_loss
```

### 6.2 为什么不作为 MVP

delta-neutral 不代表无风险。该策略至少需要：

- 多腿订单状态机；
- partial fill 和单腿暴露处理；
- 保证金率、标记价格和强平距离监控；
- funding 反转和基差扩张处理；
- 重启后的仓位与订单对账；
- 交易所宕机、拒单、限频和提现暂停处理；
- 紧急减仓及 Kill Switch。

在这些能力完成前，回测中假设两条腿同时按 candle 价格成交会严重低估执行风险。

## 7. 后续研究方向

### 7.1 BTC-ETH Statistical Arbitrage

可使用动态 hedge ratio 构造价差：

```text
spread = log(BTC_price) - hedge_ratio * log(ETH_price)
zscore = (spread - rolling_mean) / rolling_std
```

该策略可以验证组合交易能力，但不能把高相关性直接解释为均值回归。必须检查 rolling cointegration、structural break、funding cost 和单腿成交风险。

### 7.2 Cross-sectional Momentum

只允许在 point-in-time universe 中研究，并限制为上市时间足够长、成交额足够高、可实际交易的标的。必须保存历史上市、退市、改名和合约可用状态，避免用今天的币种列表回测过去。

重点风险包括：

- 幸存者偏差；
- 小币种不可成交的虚假收益；
- short 端缺少合约或借币成本过高；
- 高频调仓导致手续费和滑点吞噬收益；
- 少数币种贡献全部组合收益。

### 7.3 Order-book Imbalance

只有具备可靠 L2 数据、增量 order book 重建、延迟记录和撮合仿真后才研究。简单地用历史最优买卖价假设限价单成交，无法衡量 queue position、逆向选择、撤单失败和 partial fill。

## 8. 暂时禁止采用的策略形态

第一阶段不采用：

- Martingale、无限补仓和无硬风险上限的 Grid；
- tick 或 1m 级 Scalping；
- 做市和延迟套利；
- 多交易所、多腿期权或三角套利；
- LLM 阅读新闻后直接决定并执行交易；
- Copy Trading 和无法解释的封闭黑盒；
- 堆叠大量技术指标后反复 Hyperopt；
- 以指定年化收益率为目标持续搜索参数；
- 只展示胜率、年化收益或收益曲线，不披露回撤和成本的策略。

这些限制不是判断相关策略永远无效，而是因为它们对数据、执行、资本和风险系统的要求超出了 alphaMind 第一阶段的验证能力。

## 9. 公开策略复现流程

### 9.1 先复现，后修改

正确顺序：

1. 保存论文、帖子或开源实现的来源和版本；
2. 明确交易假设和原作者的适用范围；
3. 在可获得的数据上原样复现规则；
4. 检查能否大致重现原结果及差异原因；
5. 加入真实费用、滑点、延迟和交易规则；
6. 进行 point-in-time、lookahead 和 survivorship 检查；
7. 完成 Walk-Forward 和最终 holdout；
8. 每次只改变一个维度并完成消融实验；
9. 通过门槛后进入 dry-run，不直接进入实盘。

不能一边复现一边更换指标、周期、过滤器、仓位和止损，否则无法判断策略收益来自原假设还是参数搜索。

### 9.2 Strategy Card

每个候选策略必须建立版本化的 Strategy Card：

```yaml
name: donchian_trend
version: 0.1.0
source:
  type: paper_or_open_source
  url: ""
  source_version: ""
hypothesis: 趋势在有限时间内具有延续性
instruments:
  - BTC/USDT
  - ETH/USDT
timeframe: 4h
direction: long_flat
entry: 突破过去 N 根已完成 candle 的最高价
exit: 跌破过去 M 根已完成 candle 的最低价
position_sizing: volatility_target
expected_regime:
  - trend_up
failure_regime:
  - range
  - volatility_shock
cost_model_version: ""
known_biases:
  - lookahead
  - survivorship
  - parameter_overfit
minimum_evidence:
  paper_days: 90
  min_signals: "Phase 0 预注册"
  min_fills: "Phase 0 预注册"
  min_independent_market_events: "Phase 0 预注册"
trial_registry:
  strategy_variants_tried: 0
  parameter_sets_tried: 0
invalidating_evidence:
  - 多数样本外窗口扣除压力成本后为负
```

除规则外，还必须记录开源许可证，不能直接复制许可证不兼容或来源不明的代码。

## 10. 多策略组合边界

趋势和均值回归应先作为两个独立策略分别验证。只有二者都具备独立的样本外证据后，才允许研究组合。

第一版组合器应为确定性规则，例如：

```text
TREND_UP / TREND_DOWN -> 允许趋势策略，关闭逆势均值回归
RANGE                  -> 允许均值回归，降低趋势策略预算
EXTREME_VOLATILITY     -> 两者降仓或 HALT
DATA_OR_EXCHANGE_ERROR -> 全部 HALT
```

不能因为两个弱策略组合后的历史曲线更平滑，就认为组合产生了有效 alpha。组合层仍需检查共同暴露、相关性在压力期上升、重复信号和总方向风险。

## 11. alphaMind 实施顺序

### Phase 0：决策冻结

- 选择一个目标交易所和一个趋势基线；
- 固定现货 long/flat、4h 主周期和无杠杆边界；
- 记录风险预算、晋升规则和 Freqtrade/自研组件边界。

### Phase 1：基准与数据可信度

- 建立现金、BTC Buy-and-Hold、ETH Buy-and-Hold、可选初始 50/50 不再平衡组合和简单均线基准；
- 获取 BTC/USDT、ETH/USDT 的 point-in-time 数据；
- 完成数据质量、费用、滑点和实验复现链路。

### Phase 2：趋势策略

- 实现 ADR-0004 固定的 Donchian 基线；
- 完成 lookahead、Walk-Forward、最终 holdout、多重测试校正和压力成本测试；
- 建立风险定仓、硬退出，并保持 ADR-0004 明确冻结的“最大持仓时间退出禁用”规则。

### Phase 3：Paper-ready 工程

- 完成 Freqtrade dry-run；
- 使用 Deterministic Replay/Fault Injection 验证状态机；目标交易所支持时再使用独立 Testnet Contract Harness 验证 API 契约；
- 完成监控、故障注入、重启恢复和对账；
- 固定 Paper 候选版本。

### Phase 4：冻结版本 Paper

- 候选版本连续运行至少 90 天；
- 同时满足预注册的有效事件、成交和压力状态门槛；
- 实质修改后重新计时。

### Phase 5：Live Canary

- 只使用计划资金的 5%–10%，不使用杠杆；
- 固定策略、配置和环境版本；
- 连续验证 60–90 天，任何未解释资金差异都会阻止扩容。

### Phase 6：均值回归与组合

- 将均值回归作为独立策略验证，重点检查单边行情、插针、极端波动和负偏尾部；
- 禁止通过补仓改善回测胜率；
- 只有趋势与均值回归分别具备独立证据后，才研究确定性组合分配器。

### Phase 7：高级策略

- 具备可靠 Order Book、Open Interest 和逐笔成交数据后，再研究微观结构策略；
- 完成多腿订单状态机和保证金监控；
- 研究 Funding/Basis Carry；
- 在故障注入和单腿暴露测试通过前，Carry、统计套利和多交易所策略均不得进入实盘。

## 12. 最终判断

alphaMind 不应选择收益截图最漂亮、交易次数最多或指标最复杂的方案。合理的分阶段研究顺序是：

```text
简单趋势基线
    -> 独立通过完整晋升流程
受 Regime 约束的均值回归
    -> 两者分别具备独立证据后
确定性组合分配器
```

所有阶段共用同一套成本、风控、执行和验证口径；组合层不会替代单策略晋升。

趋势跟踪是第一条工程策略；均值回归是趋势策略完成既定晋升后才启动的第二条独立研究，不允许并行开发后择优汇报。对这一研究顺序的判断置信度为中高；对任何具体参数未来仍能盈利的判断置信度均为低。参数和策略只有在 point-in-time 数据、Walk-Forward、一次性 final holdout、压力成本、dry-run 和小额实盘中持续通过验证，才能逐步提高置信度。
