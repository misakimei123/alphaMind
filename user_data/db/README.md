# Runtime databases

backtest、dry-run、replay 和 testnet-contract 必须使用各自独立的 SQLite 文件；文件不提交 Git。
SQLite inspection 固定使用 `mode=ro` 与 `PRAGMA query_only=ON`，backup/restore 只能复制完整数据库，
不能手写修改 Freqtrade `Trade`/`Order`。

Live template 使用不可连接的 PostgreSQL 占位 DSN，并要求部署时通过 `FREQTRADE__DB_URL` 注入独立
database/schema/owner role。恢复顺序与命令见
[`docs/runbooks/runtime-db-recovery.md`](../../docs/runbooks/runtime-db-recovery.md)。
