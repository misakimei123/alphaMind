# ADR-0008：AI 行情分析特征指标与 K 线形态体系扩展设计

## 1. 背景与问题（Context & Problem）

当前项目正在从只读周期观察向 `R2-05` 特征接入（将 Donchian/ATR/EMA 等特征接入 Context）阶段演进。在现有设计中，[decision-context.schema.yaml](file:///d:/workspace/alphaMind/data/schemas/decision-context.schema.yaml) 中定义的特征主要为原始技术指标数值（如 `donchian_upper`、`ema_fast`、`atr` 等）。

然而，基于纯浮点数裸送的设计在实际行情决策中存在以下问题：
1. **动量与趋势强度的盲区**：缺乏评估超买超卖的动量指标（如 RSI）和评估趋势强弱的指标（如 ADX），导致 AI 容易在动量衰竭的震荡市或单边末期发生假突破追多。
2. **大模型的数值推理限制**：大模型（LLM）由于 Token 切分机制，对“浮点数大小比对”和“多维数值心算”极其迟钝，容易在复杂的数值交叉验证中产生逻辑幻觉。
3. **Price Action 微观支撑阻力判断的缺失**：系统具备宏观趋势（4h Donchian），但缺乏微观反转形态（如吞没形态、锤子线）的结构化输入，无法在回踩关键均线或支撑位时做出最高胜率的即时决策。

---

## 2. 决策结论与设计原则（Decision & Design Principles）

为了让 AI 更清晰、准确地分析行情特征，我们决定对 AI 的特征指标输入和 K 线形态分类体系进行扩展，遵循以下三条核心设计原则：

### 原则一：指标维度正交化（避免多重共线性）
只选择与现有指标互补、提供全新信息维度的指标，避免同质指标堆砌（例如，有了 Donchian 和 EMA 就不再全量引入 Bollinger Bands，防止冗余）。
- **新增 RSI (P0级)**：提供动量和超买超卖维度，用以评估趋势的可持续性。
- **新增 ADX (P1级)**：提供趋势强度信息，判定是否处于强单边或横盘。
- **新增 Bollinger Bands (P1级，可选)**：提供波动率带偏离和挤压状态。

### 原则二：“冷计算（量化端） + 暖推理（AI端）”双轨并行
- **冷计算（量化侧规则计算）**：所有指标的精确数学变换（如 RSI 值的计算）以及 K 线形态的硬规则识别（如 `body > 2 * ATR`），全部由底层的 Python 逻辑（量化引擎）基于硬性代码完成。大模型不承担任何代数运算和模式图形判别。
- **暖推理（AI侧语义化判断）**：量化侧将计算出的**原始数值**与提炼好的**语义化状态标签（Semantic Labels）**一同打包，由 `DecisionContext` 输入给 AI。AI 只进行逻辑推理、风险评估和新闻交叉验证。

### 原则三：关键位置形态过滤（降噪机制）
为防止 30 分钟等中短周期中高频出现的 K 线形态带来过载噪音，形态特征报送必须结合**价格相对位置**过滤：
- 仅当收盘价逼近/突破 Donchian 上下轨、或者贴近慢速均线等“关键支撑阻力区”时，才将识别出的形态标签报送给 AI。
- 处于震荡中轨区间的普通 K线形态，一律忽略并标记为 `null`。

---

## 3. 形态分类与语义定义（Pattern Classification & Semantics）

根据预计算原则，底层数据层需要根据以下条件识别 K 线形态，并转化为相应的语义标签输入给 AI：

### 3.1 第一层：单根 K 线形态

| 形态名称 (`candlestick_pattern`) | 识别规则（Python 侧硬编码） | 语义化标签描述 (`pattern_semantic`) | 交易指导意义 |
| :--- | :--- | :--- | :--- |
| `big_bullish` (大阳线) | `body > 2 * ATR` 且 `close > open` | 多头强势进攻 | 强劲买盘，支持跟单 |
| `big_bearish` (大阴线) | `body > 2 * ATR` 且 `close < open` | 空头强势进攻 | 强劲卖盘，偏向规避/减仓 |
| `hammer` (锤子线) | `lower_shadow > 2 * body` 且 `upper_shadow < 0.1 * body`，且处于 EMA 下方/通道下轨附近 | 下方有强支撑（低位看涨） | 探底回升，考虑多头入场 |
| `hanging_man` (上吊线) | 识别规则同锤子线，但处于 EMA 上方高位区 | 上方乏力（高位看跌） | 警惕多头动量衰竭 |
| `shooting_star` (射击之星) | `upper_shadow > 2 * body` 且 `lower_shadow < 0.1 * body`，且处于高位区 | 上方有强压力（高位看跌） | 冲高回落，考虑减仓/平多 |
| `inverted_hammer` (倒锤子) | 识别规则同射击之星，但处于低位区 | 下方试探（低位看涨） | 见底信号，注意后续买盘 |
| `doji` (十字星) | `body < 0.1 * range` | 多空平衡，犹豫震荡 | 观望，等待方向选择 |

### 3.2 第二层：双根 K 线组合

| 形态名称 (`candlestick_pattern`) | 识别规则（Python 侧硬编码） | 语义化标签描述 (`pattern_semantic`) | 交易指导意义 |
| :--- | :--- | :--- | :--- |
| `bullish_engulfing` (看涨吞没) | 前一根为阴线，当前阳线 `body` 完全包住前一根 `body` | 多头反扑，强反转信号 | 极强买入参考 |
| `bearish_engulfing` (看跌吞没) | 前一根为阳线，当前阴线 `body` 完全包住前一根 `body` | 空头反扑，强反转信号 | 极强卖出/规避参考 |
| `bullish_harami` (看涨孕线) | 前一根为大阴线，当前小阳线 `body` 完全被前一根包含 | 下跌动能衰竭 | 停止盲目开空，观察反弹 |
| `bearish_harami` (看跌孕线) | 前一根为大阳线，当前小阴线 `body` 完全被前一根包含 | 上涨动能衰竭 | 停止盲目加多，注意保护 |

---

## 4. 技术改造方案与影响（Proposed Changes）

### 4.1 Schema 合同改动
在 [decision-context.schema.yaml](file:///d:/workspace/alphaMind/data/schemas/decision-context.schema.yaml) 的 `features` 定义中，扩展指标和语义状态字段：

```yaml
  features:
    type: object
    additionalProperties: false
    required: [timeframe, donchian_upper, donchian_lower, atr, ema_fast, ema_slow, volume_ratio]
    properties:
      timeframe:
        type: string
        pattern: "^[1-9][0-9]*[mhdwM]$"
      # --- 既有原始数值特征 ---
      donchian_upper:
        $ref: "#/$defs/nullable_positive_decimal"
      donchian_lower:
        $ref: "#/$defs/nullable_positive_decimal"
      atr:
        $ref: "#/$defs/nullable_positive_decimal"
      ema_fast:
        $ref: "#/$defs/nullable_positive_decimal"
      ema_slow:
        $ref: "#/$defs/nullable_positive_decimal"
      volume_ratio:
        $ref: "#/$defs/nullable_positive_decimal"
      # --- 推荐新增原始数值特征 ---
      rsi:
        $ref: "#/$defs/nullable_positive_decimal"
      adx:
        $ref: "#/$defs/nullable_positive_decimal"
      # --- 推荐新增预计算语义化特征 ---
      ema_alignment:
        type: string
        enum: [strong_bullish, bullish, strong_bearish, bearish, mixed]
      candlestick_pattern:
        type: [string, "null"]
        enum: [big_bullish, big_bearish, hammer, hanging_man, shooting_star, inverted_hammer, bullish_engulfing, bearish_engulfing, bullish_harami, bearish_harami, doji]
      pattern_semantic:
        type: [string, "null"]
        maxLength: 100
```

### 4.2 AI Prompt 改动
在 [trade-decision-v1.md](file:///d:/workspace/alphaMind/prompts/ai/trade-decision-v1.md) 中，新增行情综合分析指南与推理要求：

```markdown
## Analysis and reasoning guidelines

Before proposing any action, you must perform a step-by-step assessment of the market using the provided features:
1. Multi-dimensional confirmation: Cross-verify the breakout status (Donchian) with the momentum trend (RSI, EMA alignment) and volume expansion (Volume Ratio). Do not chase breakouts if RSI indicates extreme overbought/oversold divergence.
2. Micro-macro resonance (Price Action):
   - Pay special attention to `candlestick_pattern` and `pattern_semantic` when the price is near `donchian_upper/lower` or `ema_slow`.
   - A bullish pattern (e.g., `bullish_engulfing`, `hammer`) near support signals a high-probability buy. A bearish pattern (e.g., `shooting_star`, `bearish_engulfing`) near resistance confirms a sell.
3. Conflict resolution: If indicators conflict (e.g., Donchian breakout occurs, but ADX indicates non-trending range bound market, or candlestick patterns signal exhaustion), you must explain this divergence in `decision_summary` and default to a HOLD action.
4. Output formatting:
   - In each action's `rationale`, start with `[Confidence: HIGH|MEDIUM|LOW]` and detail your logic.
   - Map key indicators conflicts and potential traps directly into `global_risks` or `action.risks`.
```

---

## 5. 验证方案（Verification Plan）

### 5.1 契约验证（Contract Tests）
- 更新测试套件 [test_ai_mvp_schemas.py](file:///d:/workspace/alphaMind/tests/contract/test_ai_mvp_schemas.py)，构造包含 `rsi`、`adx`、`ema_alignment` 和 `candlestick_pattern` 的新型 `DecisionContext` 固件。
- 确保经过扩展的 JSON 固件可以通过本地 YAML 解析和 schema 规则平衡，没有触发任何 `SCHEMA_VALIDATION_FAILED`。

### 5.2 决策质量验证（AI Dry-run Analysis）
- 使用真实的历史极端行情数据（如多空暴跌、阴跌、均线缠绕震荡）生成多个离线 context 固件。
- 调用 `run-ai-decision` 脚本，人工对比**引入语义化标签前后**，AI 逻辑推理（`rationale`）以及交易提案（`actions`）的胜率与风控质量：
  - 预期目标一：在均线纠缠（`ema_alignment: "mixed"`）且缺乏动量的情况下，AI 能够主动过滤无效突破并输出 `HOLD`。
  - 预期目标二：在回踩关键位置时，AI 能够结合 `candlestick_pattern` 正确给出带有 `[Confidence: HIGH]` 的入场提案。
