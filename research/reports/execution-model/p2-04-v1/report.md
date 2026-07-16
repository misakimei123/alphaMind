# P2-04 Execution Model

- Model: `p2-04-v1`
- Config SHA-256: `4d0816d838a893db2801ea501dfbf37d836b9198362ad7f97e24c3213275470f`
- Market/timeframe: `spot` / `4h`
- Fill: signal candle close -> next candle open; same-candle fill forbidden
- Limit: candle touch alone is insufficient; explicit confirmation/assumption is required

## Costs

- Maker fee: `0.001`
- Taker fee: `0.001`
- Half spread: `0.00025`
- Slippage per side: `0.0005`
- Boundary: fee 来自公开 Non-VIP spot rate；spread/slippage 是固定工程假设，不证明真实历史成交

## Scenario Matrix

| Scenario | Fee x | Slippage x | Parameter x | Daily shock | Missing | Delay | Disclosure |
|---|---:|---:|---:|---:|---|---:|---|
| baseline | 1 | 1 |  |  | False | 0 | 基础成本与 next-candle open 成交 |
| fee_2x | 2 | 1 |  |  | False | 0 | maker/taker fee 同时放大 2 倍 |
| slippage_3x | 1 | 3 |  |  | False | 0 | 每侧 slippage 放大 3 倍，spread 不变 |
| parameter_minus_20pct | 1 | 1 | 0.8 |  | False | 0 | 单次参数向下扰动 20%，禁止 Cartesian product |
| parameter_minus_10pct | 1 | 1 | 0.9 |  | False | 0 | 单次参数向下扰动 10%，禁止 Cartesian product |
| parameter_plus_10pct | 1 | 1 | 1.1 |  | False | 0 | 单次参数向上扰动 10%，禁止 Cartesian product |
| parameter_plus_20pct | 1 | 1 | 1.2 |  | False | 0 | 单次参数向上扰动 20%，禁止 Cartesian product |
| daily_drop_10pct | 1 | 1 |  | -0.10 | False | 0 | 单日价格冲击 -10%，不改变成交成本假设 |
| daily_drop_20pct | 1 | 1 |  | -0.20 | False | 0 | 单日价格冲击 -20%，不改变成交成本假设 |
| missing_candle | 1 | 1 |  |  | True | 0 | 预期成交 candle 缺失，订单保持未成交 |
| delay_one_period | 1 | 1 |  |  | False | 1 | 成交推迟一根完整 candle，并使用延迟 candle open |

## Evidence Boundary

Historical backtest only validates deterministic cost sensitivity. It does not prove real fills, partial fills, exchange acceptance, or production write permissions.
