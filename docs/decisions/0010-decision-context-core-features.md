# ADR-0010：DecisionContext 核心特征计算合同

## 状态

Accepted（2026-07-21，R2-05）

## 背景

DecisionContext v1 已预留 `donchian_upper`、`donchian_lower`、`atr`、`ema_fast`、
`ema_slow` 和 `volume_ratio`，但此前只有合法 fixture，没有运行时 OHLCV 来源、冻结计算参数或
point-in-time 失败语义。R2-05 必须先建立这一核心特征层，R2-06 才能在同源输入上增加 RSI、ADX
和 K 线形态。

## 决策

### 输入与市场来源

- 运行时只从 Bybit V5 公共 `GET /v5/market/kline` 读取 OHLCV，不使用 API key，也不创建交易写路径；
- Bybit 的最后一根未闭合 candle 中 `closePrice` 只是最新成交价，必须按 `startTime + timeframe <= as_of`
  过滤后才能进入特征计算；
- DecisionContext v1 每个 instrument 只有一组 features。R2-05 对同时存在 spot/linear 的标的使用 spot
  作为规范 OHLCV 来源，只在 spot 不可用时显式选择 linear；不得把两个市场的 candle 混入同一组特征；
- 输入必须是 UTC、严格递增、无重复、无间隔缺口的固定长度 candle。future、stale、错位 timeframe 或
  超过 1000 根的输入整体 fail-closed，不产生部分可信的市场判断。

### `r2-05-v1` 参数与公式

| 字段 | 冻结定义 |
|---|---|
| `timeframe` | 运行时默认 `30m`；只接受 Bybit 官方支持的固定长度周期 |
| `donchian_upper` | 当前 candle 之前 20 根已完成 candle 的最高价 |
| `donchian_lower` | 当前 candle 之前 10 根已完成 candle 的最低价 |
| `atr` | `ATR(20)`；True Range 使用当前 high/low 与前收盘，简单均值播种后按 Wilder/RMA 递推 |
| `ema_fast` | `EMA(20)`；前 20 个 close 的简单均值播种，之后使用 `2 / (20 + 1)` 递推 |
| `ema_slow` | `EMA(50)`；前 50 个 close 的简单均值播种，之后使用 `2 / (50 + 1)` 递推 |
| `volume_ratio` | 当前已完成 candle 的 volume / 前 20 根已完成 candle 的平均 volume |

所有运算使用 `Decimal`，输出以 half-even 量化到最多 8 位小数。Donchian 明确排除当前 candle，
ATR、EMA 和 volume ratio 明确包含当前已完成 candle。输入、参数和版本形成规范化 SHA-256，保证同输入
得到相同结果。

### Warmup 与不可用语义

- 单个指标历史长度不足时仅该字段为 `null`，并保留稳定的内部 reason code；
- 任何必需字段为 `null` 时，整个核心特征快照不处于 `ready`，调用方不得据此增加风险；
- volume baseline 或当前 volume 为零时 `volume_ratio=null`，不把零伪造为 schema v1 不允许的正数；
- 时间顺序、缺口、未来数据、陈旧数据或 timeframe 不一致属于整体输入不可信，全部指标输出 `null`；
- reason code、输入 hash 和 feature version 是内部审计证据；DecisionContext v1 只接收已经冻结的七个
  features 字段，避免无版本 schema 扩张。

## 影响与边界

- 这些字段只为 AI 提供只读观察，不修改 ADR-0004 的传统 Donchian 信号，也不拥有下单、数量或审批权；
- R2-06 可以消费同一 candle 合同，但不得静默修改 `r2-05-v1` 参数或公式；变更必须产生新 feature version；
- DecisionContext v1 无法同时表达 spot 与 linear 的独立特征，也无法携带 feature source/parameter hash。
  需要多市场或多 timeframe 时必须升级 Context schema，而不是复用字段造成语义漂移；
- Bybit 公共接口可能受地区限制、限频或暂时延迟。网络/响应/身份校验失败均不复用旧 candle 伪装新快照。

## 验证

- 使用冻结 candle 覆盖确定性数值、Donchian 当前 candle 排除、ATR/EMA 当前 close 纳入和 Context binder；
- 覆盖 warmup、零 volume、缺口、future、stale、timeframe mismatch、重复 candle、错误市场身份和非法 OHLC；
- 使用 Bybit 公共主网端点完成至少一次无认证只读冒烟，记录 candle 数、最后闭合时间、特征版本和输入 hash，
  不保存或输出任何凭据。

## 官方依据

- [Bybit V5 Get Kline](https://bybit-exchange.github.io/docs/v5/market/kline)
- [Bybit V5 Rate Limit](https://bybit-exchange.github.io/docs/v5/rate-limit)
