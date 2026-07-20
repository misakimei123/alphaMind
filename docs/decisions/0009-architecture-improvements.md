# ADR-0009：alphaMind 架构与工程改进设计建议

## 1. 背景（Context）

根据项目最新基线（自 2026-07-18 起），项目定位已转变为以 AI 为大脑的多指标及新闻基本面行情决策系统，由 AI 生成结构化交易动作并交由 Telegram 审批后执行。目前项目已完成了 R0 至 R2-04 阶段的底座建设，正处于 `R2-05` 特征接入的研发期。

为了指导后续 `R3`（Telegram 授权与控制）、`R4`（现货纵向闭环）以及 `R5/R6`（Demo/Testnet/Live）阶段的架构演进，防范大模型在生产环境中的脆弱性以及量化交易中的工程风险，本文件整理并输出了五大维度的架构改进路线。

---

## 2. 改进维度一：特征数据层——多时间框架、深度特征与链上资金（Data & Features）

### 2.1 引入多时间框架（Multi-Timeframe）共振支持
- **现状**：[decision-context.schema.yaml](file:///d:/workspace/alphaMind/data/schemas/decision-context.schema.yaml) 的 `features` 中每个标的仅支持映射单一的 `timeframe`（如 `30m`）。
- **隐患**：AI 会在 30 分钟微观尺度上看到向上突破，但忽视了日线级别（1d）的强阻力位，导致盲目追多。
- **方案**：扩展 `features` 结构，支持传入多周期特征（如 30m 局部细节 + 4h 趋势方向 + 1d 大市背景）。AI 在决策时，必须检查短周期信号是否得到长周期趋势的确认。

### 2.2 引入订单簿（Orderbook）及流动性指标
- **现状**：当前 AI 接收的行情仅有价格和简单的 `volume_ratio`。
- **隐患**：在市场流动性极差或点差（Spread）极大的黑天鹅时期，AI 可能会盲目建议大额限价/市价单，导致滑点（Slippage）吃掉全部利润。
- **方案**：在 `features` 中引入**盘口点差百分比（Spread %）**和**买卖盘不平衡度（Orderbook Imbalance）**。若点差超限，限制 AI 只能提案 `limit` 类型订单，并相应降低开仓仓位比例。

### 2.3 引入“聪明钱”链上交易跟踪（Smart Money Flow Tracking）
- **现状**：当前特征数据仅来源于中心化交易所的 OHLCV，缺乏链上 DEX 筹码分布和巨鲸资金流动特征。
- **隐患**：
  1. **入场滞后（Lagging）**：链上科学家或机器人的建仓往往在秒级完成，如果直接以 30 分钟的 AI 周期去盲目跟单，极易在高位“接盘”。
  2. **链上钓鱼（Wash Trading）**：庄家常使用已知聪明钱包倒手代币进行虚假造势，或通过空投将代币打给名人钱包伪造巨鲸持仓。
  3. **对冲套利误导**：部分钱包买入现货其实是为了在 CEX 建立等额空头进行期现套利，仅观察买入会产生误判。
- **方案**：
  1. **冷计算聚合与洗净**：由 Python 监控端过滤微额流水（如仅统计单笔 `> 10,000 USD` 交易），并在 30m 周期内进行统计聚合（计算累计净流入额 `net_flow_usd` 以及参与交易的独立聪明钱包数 `active_smart_wallets`），降噪后写入 Context。
  2. **Schema 属性扩展**：新增 `on_chain_flows` 结构体，提供资金流状态标签（如 `heavy_accumulation` 强建仓，`heavy_distribution` 强派发）。
  3. **Prompt 交叉校验指引**：在 [trade-decision-v1.md](file:///d:/workspace/alphaMind/prompts/ai/trade-decision-v1.md) 中添加防钓鱼校验（例如，若价格突破上轨但聪明钱处于重度派发状态，判定为背离诱多陷阱，限制开多；必须满足活跃聪明钱包数 $\ge 3$ 才确认群体性建仓）。

---

## 3. 改进维度二：执行与风控层——防御价格漂移与夹子攻击（Risk & Execution）

### 3.1 引入“执行前复核”的时间偏离校验（Pre-execution Revalidation）
- **现状**：AI 生成提案到用户在 Telegram 上点击“批准”存在数分钟延迟（TTL 限制内）。
- **隐患**：在价格快速波动的市场中，用户点击批准时，价格可能已经严重偏离了 AI 决策时的进场范围，此时执行会导致追高，甚至被三明治夹子攻击。
- **方案**：在 [validation.py](file:///d:/workspace/alphaMind/src/alphamind/decision/validation.py) 阶段或执行网关层，必须计算“时效价格偏离度”。如果当前最新价格相比 AI 决策时点价格的偏差超过了 `0.5 * ATR`，即使人工在 Telegram 上点击了批准，执行网关（ExecutionGateway）也必须强行拒绝并触发 `FAIL_CLOSED` 报警。

### 3.2 僵尸订单与部分成交（Partial Fill）处理
- **现状**：限价单执行是异步的，常发生部分成交。
- **隐患**：未成交部分会长期占用保证金，且偏离了原有的风险暴露控制。
- **方案**：执行网关必须支持超时自动撤单（如挂单超过 5 分钟未完全成交，则自动撤销未成交部分），并将已成交事实重新反馈给 AI，由 AI 在下一周期评估是否需要平仓或对冲。

---

## 4. 改进维度三：大模型调用层——弹性降级与成本防御（LLM Reliability）

### 4.1 备用模型提供商的“动态热降级（Fallback Provider）”
- **现状**：目前 [provider.py](file:///d:/workspace/alphaMind/src/alphamind/ai/provider.py) 仅能通过静态的配置文件切换模型 Profile。
- **隐患**：大模型 API（如 DeepSeek 或 OpenAI）发生大面积限流（429）、响应超时或网络阻断时，调度周期会被强行卡死，导致仓位失去 AI 的监护。
- **方案**：引入**双中心热备降级机制**。当主 Provider（如 DeepSeek）连续 2 次请求超时或失败时，底座调度器应能自动秒级降级到备用 Provider（如本地轻量级大模型 Llama 3 运行节点或 OpenAI 节点），确保系统至少能输出 `HOLD`。

### 4.2 推理字数（Reasoning Effort）与成本的弹性调节
- **现状**：像 DeepSeek R1 这样的推理模型，每次决策可能会输出极长的 Thinking tokens，单次调用成本较高。
- **隐患**：在横盘震荡行情下，高频调用深度推理模型会造成严重的 API 成本浪费。
- **方案**：在 Python 侧评估波动率（ATR）或新闻密度。在平稳横盘期，切换为低成本的普通模型或短推理 Profile；仅在市场剧烈波动或有重大公告时，才激活长推理模型以进行深度逻辑剖析。

---

## 5. 改进维度四：人机交互层——交互防重放与紧急锁死（Telegram Control）

### 5.1 Telegram 回调防重放攻击（Nonce 验证）
- **现状**：Telegram Bot 通过 Webhook 或轮询接收点击指令。
- **隐患**：如果网络请求被拦截、或者用户在手机上因网络卡顿重复点击“批准”，可能导致同一笔开仓动作在 Freqtrade 中被重复执行多次，造成超额暴露。
- **方案**：在 [contracts.py](file:///d:/workspace/alphaMind/src/alphamind/decision/contracts.py) 定义的 Action 中绑定唯一的一次性 `Nonce` 标识与 `cycle_id`。网关执行一次后立刻将 Nonce 标记为已失效，任何重复 Telegram 点击回调均被静默丢弃。

### 5.2 Telegram 一键全平仓（Emergency Panic Button）
- **现状**：目前系统缺乏越过 AI 进行全局处置的紧急接口。
- **隐患**：在突发系统级崩盘或 API Key 泄露嫌疑时，等待 30 分钟的 AI 周期或繁琐的 Telegram 多步骤审批会错失平仓时机。
- **方案**：在 Telegram 侧白名单中实现越过 AI 的直接物理干预指令 `/panic_all`。触发后，网关绕过 AI 决策，直接调用 API 撤销所有挂单、并以市价全平现货与合约，随后将系统状态强行锁死为 `KILLED_MANUAL_REVIEW`。

---

## 6. 改进维度五：测试与仿真层——故障注入仿真（Chaos Engineering）

### 6.1 模拟交易所网络异常与 API 锁死测试
- **现状**：目前项目的单元测试多属于静态的 Schema 比对和纯函数验证。
- **隐患**：在 live 状态下，交易所常常返回 `502 Bad Gateway`、`504 Gateway Timeout` 或账户被临时限流（`rate limit exceeded`）。
- **方案**：在进入 `R5` 前，引入**故障注入测试（Fault Injection Tests）**。在 mock 交易所返回 504 错误或网络超时的情况下，测试系统的调度器是否能保证不产生重复挂单、并且能够平稳记录状态日志。
