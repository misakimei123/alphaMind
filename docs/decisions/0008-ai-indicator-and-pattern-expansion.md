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

### 2.1 R2-06 冻结参数与版本边界

R2-06 将本 ADR 从方向性设计收口为以下可复算合同：

- `DecisionContext.schema_version` 从 `1` 升级为 `2`；v2 强制存在新增字段，但 RSI、ADX、EMA
  alignment 和形态字段允许在 warmup 或输入不可用时为 `null`。运行时 binder 只接受 v2，旧 v1
  fixture 不会被静默补默认值；R2-07 尚未建立持久化，因此当前不存在需要原地迁移的生产记录。
- Prompt 新建 `trade-decision-v2.md`，AI profile 固定 `version: 2`、新路径和 SHA-256；v1 文件只保留为
  历史审计证据，不能再由活动 profile 引用。
- RSI 周期固定为 `14`，使用 Wilder/RMA：先对前 14 个 close delta 的 gain/loss 分别取简单均值，
  后续按 `(previous * 13 + current) / 14` 平滑。平均 loss 为零且 gain 大于零时 RSI 为 `100`；平均
  gain 为零且 loss 大于零时 RSI 为 `0`；二者同时为零时指标不可用并输出 `null`，不得伪造 `50`。
- ADX 周期固定为 `14`，使用 Wilder 的 TR、`+DM`、`-DM` 与 RMA。需要 28 根 candle 才能得到
  首个 ADX：先用 14 个相邻 candle transition 播种平滑 TR/DM，得到首个 DX，再累计 14 个 DX 取
  简单均值；其后继续使用 Wilder 平滑。平滑 TR 为零时 ADX 不可用；DI 之和为零但 TR 有效时 DX
  为合法的 `0`。RSI 与 ADX 均规范化为 `[0, 100]` 内、8 位小数 half-even 的 Decimal 字符串。
- EMA alignment 使用当前已完成 candle 的 `close`、EMA(20)、EMA(50) 和 ATR(20)：
  `close > ema_fast > ema_slow` 为 bullish，`close < ema_fast < ema_slow` 为 bearish；对应快慢 EMA
  间距达到 `0.5 * ATR` 时升级为 `strong_bullish` / `strong_bearish`，其他情况为 `mixed`。ATR
  为零或任一依赖不可用时输出 `null`。
- 关键位置容差固定为 `0.25 * ATR`。当前 candle 的 close/high 与 Donchian upper 的最小距离不超过
  容差时视为 resistance；close/low 与 Donchian lower 的最小距离不超过容差时视为 support；当前
  close 在 EMA(50) 上方时 EMA(50) 只作为 support，在其下方时只作为 resistance。若 support 与
  resistance 同时成立，使用距离较小者；距离相同则不赋予方向位置，避免任意偏置。
- 只有位于上述 support/resistance 的形态才可报送。双 candle 吞没要求相反方向且当前实体完整包含前一
  实体；孕线除相反方向和完整包含外，还要求前一实体至少为 `1 * ATR`、当前实体不超过前一实体的
  `0.5`。大阳/大阴严格使用当前实体 `> 2 * ATR`；doji 严格使用非零振幅且实体 `< 0.1 * range`。
  零振幅不识别任何形态，零实体只可能成为 doji，不参与吞没、孕线或影线比例判断。
- 多形态冲突只输出一个结果，固定优先级为：`bullish_engulfing`、`bearish_engulfing`、
  `bullish_harami`、`bearish_harami`、`big_bullish`、`big_bearish`、方向位置对应的 hammer / hanging
  man / shooting star / inverted hammer、`doji`。形态语义使用受控代码映射，不接收自由文本。
- Bollinger Bands 继续延期；本任务不因其在 P1 可选列表中出现而实现。

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
    required:
      - timeframe
      - donchian_upper
      - donchian_lower
      - atr
      - ema_fast
      - ema_slow
      - volume_ratio
      - rsi
      - adx
      - ema_alignment
      - candlestick_pattern
      - pattern_semantic
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
      # --- v2 新增原始数值特征，字符串数值必须在 [0, 100] ---
      rsi:
        $ref: "#/$defs/nullable_indicator_decimal"
      adx:
        $ref: "#/$defs/nullable_indicator_decimal"
      # --- v2 新增受控语义特征 ---
      ema_alignment:
        type: [string, "null"]
        enum: [strong_bullish, bullish, strong_bearish, bearish, mixed, null]
      candlestick_pattern:
        type: [string, "null"]
        enum: [big_bullish, big_bearish, hammer, hanging_man, shooting_star, inverted_hammer, bullish_engulfing, bearish_engulfing, bullish_harami, bearish_harami, doji]
      pattern_semantic:
        type: [string, "null"]
        enum: [bullish_attack, bearish_attack, bullish_support_rejection,
          bearish_exhaustion, bearish_resistance_rejection, bullish_support_test,
          bullish_reversal, bearish_reversal, bearish_momentum_exhaustion,
          bullish_momentum_exhaustion, indecision, null]
```

### 4.2 AI Prompt 改动
新建 [trade-decision-v2.md](../../prompts/ai/trade-decision-v2.md)，并让活动 AI profile 固定其版本、
路径和 SHA-256。v1 只保留历史审计用途。v2 的模型行为约束如下：

```markdown
## Analysis and reasoning guidelines

1. Cross-check Donchian, RSI, ADX, EMA alignment and volume instead of treating one signal as authority.
2. Treat a location-filtered candle pattern as an observation, never as a guaranteed reversal or order command.
3. Prefer HOLD when indicators conflict, an expanded indicator is unavailable, or alignment is mixed; summarize
   observable conflicts without requesting or disclosing hidden chain-of-thought.
4. Start rationale with a controlled confidence label, while keeping deterministic validation and Telegram approval
   mandatory outside the model.
```

---

## 5. 验证方案（Verification Plan）

### 5.1 契约验证（Contract Tests）
- 更新测试套件 [test_ai_mvp_schemas.py](file:///d:/workspace/alphaMind/tests/contract/test_ai_mvp_schemas.py)，构造包含 `rsi`、`adx`、`ema_alignment` 和 `candlestick_pattern` 的新型 `DecisionContext` 固件。
- 确保经过扩展的 JSON 固件可以通过本地 YAML 解析和 schema 规则平衡，没有触发任何 `SCHEMA_VALIDATION_FAILED`。

### 5.2 决策质量验证（AI Dry-run Analysis）

- 冻结趋势、震荡、暴跌、阴跌、关键位置反转、非关键位置噪音及全部边界类 Context，按 v1/v2
  Prompt 约束制作离线 policy fixture 对照；每个输出都必须重新通过 v2 Schema、binder 和 R2-04
  业务校验，并明确说明该对照没有实际执行模型。
- 对照报告必须披露 Schema 合法率、应 HOLD 场景命中率、冲突引用率、非法/越权动作率，以及明确标为
  估算值的 Token/成本变化。没有 provider usage 时，禁止把估算值描述为实际计费。
- 离线 fixture 对照只证明合同和预期决策政策可复核，不证明真实 provider 稳定遵循 Prompt，更不证明胜率提升；
  后续真实 provider、R4 dry-run 和 R6 前向运行必须继续按各自证据层验证。
