# alphaMind 开发路线图

> 本文是 [完整开发计划](development-plan.md) 的阶段摘要，不维护独立任务状态。若二者冲突，以开发计划为准。

## 1. 产品目标

alphaMind 是个人 AI 加密货币交易系统：默认每 30 分钟读取 Bybit 账户、持仓、挂单、行情、合约风险和新闻，由 AI 生成结构化候选动作，经 Telegram 人工批准后自动通过 Freqtrade 执行并通知结果。

MVP 包含：

- 配置化 BTC、ETH、SOL、HYPE，并允许运行时增删标的；
- Bybit 现货与 USDT 线性永续；
- 现货 long，合约按配置支持 long/short、isolated margin 和最大杠杆；
- AI `HOLD/OPEN/ADD/REDUCE/CLOSE/CANCEL_ORDER` 动作；
- 新闻增量抓取、去重、资产关联、来源引用和不可信文本隔离；
- Telegram 白名单、TTL、nonce、幂等、批准/拒绝和结果通知；
- 批准后的价格、余额、仓位、挂单、精度、杠杆和风险重新校验；
- Freqtrade 作为唯一 Bybit 交易写入者；
- Spot/Futures 配置、Runtime DB、bot identity 和 secret 隔离。

## 2. 复用底座

以下旧 P0-P3-04 产物继续使用：

- 固定 Python、Freqtrade、CCXT 和容器环境；
- 数据质量、历史研究、Donchian/ATR 和回测基线；
- 确定性风险定仓、账户损失边界、RiskSnapshot 和 Kill Switch；
- Audit Outbox、幂等 Writer、Runtime DB 隔离和恢复工具；
- Bybit spot 基础 capability matrix。

Donchian 不再是唯一交易权威，而是 AI 的结构化特征和传统对照基线。原 Final Holdout、固定 90 天 Paper 和独立评审不再阻塞产品开发。

## 3. 目标运行链

```text
默认每 30 分钟触发
  -> 读取账户、持仓、订单、行情与合约风险
  -> 拉取并清洗配置化新闻
  -> 构建 DecisionContext
  -> LLM 生成结构化 Action
  -> Schema 与确定性风险校验
  -> Telegram 待审批
  -> 用户批准 / 拒绝 / 超时
  -> 批准后重新校验
  -> ExecutionGateway 驱动 Freqtrade
  -> Bybit 订单、成交和仓位对账
  -> Telegram 结果通知
```

30 分钟周期只负责 AI 观察与建议。硬止损、交易所托管保护、Kill Switch、只减仓状态和风险告警持续生效，不等待下一周期。

## 4. 阶段总览

| 阶段 | 核心目标 | 主要产物 | 资金权限 |
|---|---|---|---|
| 已完成底座 | 保留传统研究、风险、审计和 DB 能力 | P0-P3-04 产物 | 无新增授权 |
| R0 重新定基线 | 统一规范和配置合同 | 唯一计划、Schema、模型/新闻配置、资金参数 | 无 |
| R1 配置化与账户观察 | 去掉 BTC/ETH/spot/4h 硬编码 | Instrument Registry、市场能力、RiskSnapshot v2、调度器 | 只读/fake |
| R2 新闻与 AI 决策 | 形成可校验 AI Action | NewsItem、DecisionContext、LLM provider、Action validator | 只读 |
| R3 Telegram 授权 | 形成安全人工审批闭环 | Proposal Store、状态机、白名单、TTL、重新校验 | fake executor |
| R4 现货纵向闭环 | Telegram 批准后完成现货 dry-run | ExecutionGateway、现货动作、订单对账 | dry-run |
| R5 合约纵向闭环 | 支持 isolated futures 与杠杆风控 | long/short、leverage、liq/funding、保护单 | Demo/Testnet |
| R6 Paper 与小额 Live | 联合运行和真实资金验证 | 14–30 天指标、极小现货、合约 1x | 逐项批准 |

## 5. R0：重新定基线

- 同步 README、架构、Runtime Contract 和关键 ADR；
- 定义 Runtime、Instrument、NewsItem、DecisionContext、Action 与 Approval schema；
- 配置模型供应商、Prompt、Token/成本、超时和新闻来源；
- 由项目所有人确认真实资金、最大总损失、最大杠杆、short、亏损加仓和敞口参数。

完成标准：仓库不再同时声称“AI 不进入资金链”和“AI 是 MVP 主链”。

## 6. R1：配置化与账户观察

- 删除风险适配器和 watchdog 中的 BTC/ETH 固定集合；
- 由 Instrument Registry 加载币种、spot/futures pair、方向和最大杠杆；
- 启动时通过 Bybit instruments endpoint 读取市场状态、精度、最小金额和交易所最大杠杆；
- RiskSnapshot v2 覆盖 spot、long/short、挂单、mark、liq 和 funding；
- 建立 Spot/Futures 独立 Freqtrade 配置、DB 和身份；
- 实现无重叠的默认 30 分钟调度器和手工触发。

完成标准：没有 LLM 时也能生成完整只读周期快照。

## 7. R2：新闻与 AI 决策

- 至少两个可配置新闻/公告适配器；
- 新闻增量抓取、去重、时间校验、资产关联和来源分类；
- 外部文本作为不可信数据，不能改变系统规则或工具权限；
- 构建包含账户事实、行情特征、新闻和风险状态的 DecisionContext；
- 实现 LLM provider、结构化输出、有限重试和成本记录；
- 校验动作、价格关系、新闻引用、有效期和允许市场。

完成标准：模型稳定返回合法 HOLD 或候选 Action；非法输出不能进入审批。

## 8. R3：Telegram 人工授权

- Proposal/Action Store 与状态机；
- user/chat 白名单、nonce、TTL 和重复点击幂等；
- `[批准] [拒绝] [详情]`、暂停 AI、停止新开仓和紧急入口；
- 批准后重新检查价格、仓位、挂单、余额、市场规则和风险；
- 成功、部分成交、未执行、失败和风险事件通知。

完成标准：fake executor 中一次批准只产生一次执行。

## 9. R4：现货执行闭环

- 先验证 Freqtrade `forceenter/forceexit/adjust_trade_position/cancel` 的锁定版本行为；
- ExecutionGateway 只消费已批准 Action，只驱动 Freqtrade，不直接调用 Bybit 写接口；
- 支持配置化现货 OPEN/ADD/REDUCE/CLOSE/CANCEL；
- Action 与 trade/order/exchange order 关联；
- 覆盖 submit unknown、partial fill、取消竞争和重启对账；
- 至少运行 7 天个人 dry-run，不以收益率作为功能门禁。

完成标准：用户只在 Telegram 批准，即可完成一笔 dry-run 现货交易并收到最终结果。

## 10. R5：合约与杠杆闭环

- Bybit USDT linear perpetual、isolated、one-way；
- long/short、leverage callback 和三层最大杠杆；
- 单标的/组合名义敞口、保证金、强平缓冲和 funding 风险；
- futures on-exchange stop、reduce-only 和保护单更新；
- Bybit Demo/Testnet 验证配置资产中实际存在的合约；
- 演练 position mode、杠杆设置、保护单、funding 和强平距离异常。

完成标准：AI 不能越过配置或交易所上限；测试合约可以自动执行、保护和对账。

## 11. R6：联合 Paper 与小额上线

- Spot/Futures 联合运行 14–30 天；
- 按 model、prompt、asset 和 action 统计批准率、执行率、PnL、拒绝原因和新闻引用；
- API Key 禁止提现并绑定 IP；
- 先上线极小现货，再单独批准合约 1x；
- 只有 1x 合约稳定后才提高配置最大杠杆；
- 新增币种或杠杆只执行对应市场 smoke test，不重走全部传统研究阶段。

完成标准：所有资金、订单和仓位变化都能由批准动作、风险动作、成交、费用、funding 或外部现金流解释。

## 12. 保留的安全底线

- AI 和 Telegram Bot 不持有 Bybit key；
- API Key 无提现权限；
- 用户批准前不增加市场风险；
- Action、审批回调和执行请求幂等；
- 计划过期或价格漂移后不执行；
- AI 不决定最终数量，不解除止损、Kill Switch 或 Close-Only；
- 有效杠杆不得超过全局、标的和交易所三层上限；
- 未知下单结果先查询对账，不盲目重试；
- 真实合约先从 1x 开始；
- 管理 API 不暴露到公网。

## 13. 非目标

- 跳过人工审批的完全自主实盘；
- 多模型辩论和多 Agent 委员会；
- 自动 Prompt 优化或自动提高杠杆；
- 多交易所、跨所、多腿和期权；
- 未清洗的 Twitter/Telegram 群全文输入；
- 高频和亚分钟交易；
- 绕过 Freqtrade 的第二 Bybit 交易写入者。
