# Freqtrade user data

该目录是锁定 Freqtrade 容器唯一可写的运行时挂载点。配置模板位于 `configs/freqtrade/`，不得把 API Key、secret、生产数据库凭据或 Final Holdout 放入本目录。

- `strategies/`：P2-02 创建唯一策略；
- `data/`：P1-03 下载的版本化公开市场数据，不提交 Git；
- `db/`：各模式独立 Runtime DB，不提交 Git；
- `audit/`：独立 SQLite WAL outbox 与只写 Research/Audit DB，不提交 Git；
- `logs/`、`backtest_results/`：运行产物，不提交 Git。

`live.template.json` 仅用于静态合同验证，当前 Compose 故意没有 live service。P5 批准前不得增加真实凭据或启动 Live。
