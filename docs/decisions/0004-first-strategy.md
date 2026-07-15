# ADR-0004：首个策略冻结为 4h Donchian 趋势基线

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-04 |
| 制定日期 | 2026-07-15 |
| Strategy Card | `research/strategy_cards/donchian_trend_v0.1.0.yaml` |
| 冻结时点 | P0-05 数据合同和任何候选收益结果之前 |

## 1. 决策

首个候选策略固定为 `donchian_trend_v0.1.0`：

| 项目 | 冻结值 |
|---|---|
| 市场 | Bybit 国际版现货，BTC/USDT、ETH/USDT |
| 方向 | long/flat，不使用杠杆 |
| 主周期 | 4h |
| 入场 | 已完成 candle 的 close 严格大于前 20 根已完成 candle 的最高 high |
| 退出 | 已完成 candle 的 close 严格小于前 10 根已完成 candle 的最低 low |
| rolling 规则 | 阈值严格滞后一根，当前 candle 不参与阈值 |
| 初始硬止损 | 实际平均成交价减去 `2 × ATR(20)`；ATR 使用信号 candle 已知值 |
| 成交时点 | 最早为信号 candle 之后的下一可交易时点；回测默认下一根 open |
| 最大持仓时间 | 禁用；由 10-candle channel exit、硬止损或账户风险状态退出 |
| 单笔计划风险 | NAV 的 0.25%，且继续受余额、暴露和交易规则上限约束 |
| 最大同时持仓 | 2，每个交易对最多 1 个 |

`minimal_roi`、trailing stop、position adjustment、DCA、pyramiding、short、leverage 和信号强度加仓全部禁用。策略不使用 RSI、EMA、ADX、LLM、新闻或事后 regime filter。数据健康、账户健康和 Kill Switch 是外部许可条件，不构成 alpha 规则。

## 2. 选择依据与适用边界

[Original Turtle Rules](https://c.mql5.com/3/131/Curtis_Faith_-_Original_Turtle_Rules.pdf)提供了 20-period breakout、10-period opposite exit 和 2N stop 的经典结构。本项目只借用该结构作为少参数工程基线：

- 原规则主要针对日线期货和多市场组合；本项目是 4h 加密现货、两个高度相关标的；
- `20/10` 在 4h 上表示 20/10 根 candle，不是忠实复刻 20/10 个交易日；
- 原规则的跳过前次盈利信号、pyramiding、short 和约 2% 风险不进入 MVP；
- 本项目风险预算固定为 0.25%，收益有效性必须重新验证，不能引用历史 Turtle 成绩代替证据。

选择 4h `20/10` 而不是先搜索多个窗口，是为了在不读取目标结果的前提下得到规则简单、事件数量可观测的基线。1d 使用相同 `20/10` 仅检查方向一致性，不参与参数选择。Freqtrade 明确说明收盘信号通常在下一根 candle open 成交，因此 Strategy Card 禁止信号 candle close 无滑点成交。

## 3. ATR、止损与退出语义

`ATR(20)` 使用 True Range 的 Wilder/RMA 更新，输入只到已完成的信号 candle。入场成交后：

```text
initial_stop = average_entry_fill - 2 * signal_atr_20
```

- pre-trade sizing 使用保守 entry reference、同一 ATR 和成本/gap buffer；
- 实际成交价确定后只能维持或降低批准风险，禁止扩大数量；
- 初始 stop 固定，不追踪、不放宽；
- 触发 stop、channel exit 或账户 Kill Switch 时允许安全退出；
- Live 中 stop 与 channel exit 以实际最早观测到的触发为准；Backtest 遵守锁定的 Freqtrade 2026.6 排序，并用 detail timeframe 审计同 candle 歧义；
- Bybit spot 使用 bot-managed stoploss 的离线风险继续遵守 Runtime Contract。

最大持仓时间明确设为 disabled。这是趋势策略的可证伪组成部分，不允许看结果后增加任意 time exit 截断亏损或盈利。若运行安全要求 time exit，必须建立新策略版本并重新执行完整验证。

## 4. 参数试验预算

基线参数固定为 `entry=20`、`exit=10`、`ATR=20`、`stop=2.0 ATR`。只允许 one-at-a-time 稳定性扰动：

- entry window：16、18、22、24；
- exit window：8、9、11、12；
- stop multiple：1.6、1.8、2.2、2.4；
- 1d：只运行基线参数一次。

连同 4h 基线，预注册参数试验上限为 14。禁止把三组扰动做 Cartesian product，禁止 Hyperopt，禁止新增指标过滤器。成本压力、regime 切片和 BTC/ETH 分拆属于同一候选的诊断，不增加参数选择权。

任何超出预算的试验都必须增加 trial registry、解释原因并产生新 Strategy Card 版本；不得删除失败试验。

## 5. 最小证据和独立事件

Backtest Qualified 最少需要：

- 样本外完成交易不少于 40 笔；
- 独立 breakout event 不少于 12 个；
- 同方向信号在 72 小时内跨 BTC/ETH 聚类为一个事件；
- 证据不足时结论为 `INCONCLUSIVE`，只能延长数据，不能降低门槛。

Paper Qualified 除连续至少 90 天外，最少需要 12 个有效入场信号、8 个真实模拟成交入场和 4 个独立 breakout event。Paper 门槛用于验证运行链，不单独证明统计盈利能力。

## 6. 预注册证伪条件

出现任一情况时，当前候选不得晋升：

1. 多数 Walk-Forward validation fold 的成本后 expectancy 不大于零；
2. 聚合样本外在 2 倍费用和 3 倍滑点下 expectancy 不大于零；
3. one-at-a-time 相邻参数中少于 70% 保持非负成本后 expectancy；
4. 现金流调整组合回撤触发既定 5% Kill Switch；
5. 结果依赖未来数据、同 candle 成交、静默补缺或当前 candle 进入 rolling threshold；
6. 收益只能通过增加 DCA、杠杆、trailing/ROI 调参或删除失败交易维持。

交易数或独立事件数不足不等同于证伪，但会阻止晋升。BTC/ETH、regime 和 Top 5 交易贡献必须披露；集中度用于解释，不以事后新增单一百分比阈值挑选结果。

## 7. 失败路径

- 横盘和假突破导致连续小额止损；
- BTC/ETH 高相关使两个信号不等于两个独立事件；
- gap、流动性恶化和 bot 离线造成实际亏损超过计划风险；
- 长期下跌时保持 flat，可能显著落后于其他资产或现金机会；
- 低频趋势收益可能集中于少数行情，导致较长 Time Under Water。

这些是策略假设的固有风险，不通过事后加过滤器隐藏。下一任务 P0-05 必须在查看候选结果前冻结数据、Walk-Forward 和 final holdout。
