# Audit runtime data

`outbox.sqlite` 是 Freqtrade callback 唯一可写的本地 SQLite WAL outbox；
`research-audit.sqlite` 只由 Audit Writer sidecar 写入。两者都属于运行时数据，不提交 Git。

启动 Freqtrade 前必须通过 `ALPHAMIND_PROJECT_COMMIT` 传入实际部署的 40 位 Git commit，缺失或
不合法时策略启动失败，避免产生不可追溯的审计事实。Audit DB 不能用于恢复或修改 Freqtrade
`Trade`/`Order`。
