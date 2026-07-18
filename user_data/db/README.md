# Runtime databases

backtest、spot-dry-run、futures-dry-run、replay 和 testnet-contract 必须使用各自独立的 SQLite 文件；文件不提交 Git。
SQLite inspection 固定使用 `mode=ro` 与 `PRAGMA query_only=ON`，backup/restore 只能复制完整数据库，
不能手写修改 Freqtrade `Trade`/`Order`。

两个 Live template 使用不同的不可连接 PostgreSQL 占位 DSN，并要求部署时分别从
`ALPHAMIND_SPOT_FREQTRADE_DB_URL`、`ALPHAMIND_FUTURES_FREQTRADE_DB_URL` 映射到对应容器内的
`FREQTRADE__DB_URL`，同时使用独立 database/schema/owner role。恢复顺序与命令见
[`docs/runbooks/runtime-db-recovery.md`](../../docs/runbooks/runtime-db-recovery.md)。
