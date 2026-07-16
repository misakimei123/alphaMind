# Data quality report: bybit-spot-development-ef232b839406-p1-04-v1

- Status: `ACCEPTED`
- Source snapshot: `bybit-spot-ohlcv-20260716T070451Z-ef232b839406`
- Scope: `2022-01-01T00:00:00Z` to `2026-07-01T00:00:00Z`
- Errors: `0`
- Warnings: `0`
- Clean published: `true`
- Report SHA-256: `06cb68e83a5cdccc5bafdffead91cfe22531d586d06848eb5d68fc7e9c7ae33c`

质量流水线不填补、不插值、不去重、不重排 source。ERROR 阻止 clean 发布；零成交量和固定阈值跳变只作为 WARN 保留。

| Pair | Timeframe | Rows | Expected | Errors | Warnings | Result |
|---|---:|---:|---:|---:|---:|---|
| BTC/USDT | 4h | 9852 | 9852 | 0 | 0 | ACCEPTED |
| BTC/USDT | 1d | 1642 | 1642 | 0 | 0 | ACCEPTED |
| ETH/USDT | 4h | 9852 | 9852 | 0 | 0 | ACCEPTED |
| ETH/USDT | 1d | 1642 | 1642 | 0 | 0 | ACCEPTED |
