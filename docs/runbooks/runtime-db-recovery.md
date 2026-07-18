# Runtime DB 隔离、备份与恢复 Runbook

## 1. 不变量与适用范围

Freqtrade Runtime DB 是 `Trade`、`Order`、open position、filled/cancelled order 和重启状态的
唯一内部权威。只有锁定的 Freqtrade 2026.6 进程可以创建、迁移或解释这些表；alphaMind 只能：

- 通过 SQLite `mode=ro` + `PRAGMA query_only=ON` 或 PostgreSQL watchdog 只读角色检查登记的
  schema/SELECT；
- 在 Freqtrade 停止或 SQLite online-backup 一致性边界内复制完整数据库；
- 原子切换已经通过完整性和 schema 验证的整库备份。

禁止用 Audit DB、手写 `INSERT/UPDATE/DELETE`、SQLAlchemy model 副本或数据修补脚本恢复
Runtime DB。Live 配置不保存 DSN 或口令；spot/futures 的 DSN、owner/watchdog 凭据必须由部署
secret 分别注入，并在各自容器内映射为 `FREQTRADE__DB_URL`。R6 批准前仍没有 Live Compose service。

## 2. 环境隔离

规范清单位于 `configs/common/runtime-db-contract.toml`：backtest、spot-dry-run、futures-dry-run、
replay 和 testnet-contract 分别使用独立 SQLite 文件；spot/futures Live Canary 也分别使用独立
PostgreSQL database、schema、Freqtrade owner role 与 watchdog role。不得按市场或 `is_live` 复用数据库。

Live template 内的 `<spot-set-at-runtime>` / `<futures-set-at-runtime>` 是故意不可连接的 fail-closed
值，避免环境变量缺失时回退创建默认 SQLite。部署分别读取
`ALPHAMIND_SPOT_FREQTRADE_DB_URL` / `ALPHAMIND_FUTURES_FREQTRADE_DB_URL`，再注入对应容器内的
`FREQTRADE__DB_URL`；database 分别为 `alphamind_spot_live` / `alphamind_futures_live`。锁定
Freqtrade 基础镜像不内置 PostgreSQL driver；R6 构建 Live 专用镜像时必须固定并复核 driver/hash，
不能在容器启动时联网安装。角色由 DBA/secret manager 创建后，再对两个隔离 schema 分别应用
`configs/postgres/live-runtime-roles.sql`。

## 3. 启动与恢复顺序

任何异常退出、主机恢复、DB restore 或版本升级都严格执行：

1. **Exchange facts**：用独立 read-only 交易所凭据获取余额、open orders、近期 fills 和可用持仓事实；
2. **Runtime DB**：验证完整性、锁定 schema fingerprint、备份时间和目标环境 identity；失败时保持停机；
3. **Freqtrade reconcile**：只启动同一锁定版本 Freqtrade，由其执行 migration/恢复并对账；
4. **Safe disposition**：不一致时保持 close-only，只允许 Freqtrade/operator 执行止损、撤销未知挂单或减仓；
5. **Audit backfill**：Runtime/Exchange 已一致且安全处置完成后，最后启动 Audit Writer 补写。

在步骤 1–4 完成前禁止新入场。Audit DB 离线不能反向阻止安全退出；outbox 是否阻止新入场仍按
P3-03 的独立容量/年龄背压判断。

## 4. SQLite backup、restore 与 rollback

本地/dry-run 使用仓库脚本；所有路径必须属于同一实例，禁止跨 spot/futures 或把 backtest backup
恢复到 dry-run。以下以 spot 实例为例：

```powershell
uv run python scripts/manage_runtime_db.py backup `
  --source user_data/db/spot.dry-run.sqlite `
  --destination backups/spot-dry-run/20260717T120000Z.sqlite

uv run python scripts/manage_runtime_db.py verify `
  --database backups/spot-dry-run/20260717T120000Z.sqlite
```

恢复前停止 Freqtrade，确认没有 `target-wal`/`target-shm`，再执行：

```powershell
uv run python scripts/manage_runtime_db.py restore `
  --backup backups/spot-dry-run/20260717T120000Z.sqlite `
  --target user_data/db/spot.dry-run.sqlite `
  --rollback-backup backups/spot-dry-run/pre-restore.sqlite `
  --confirm-freqtrade-stopped
```

命令会先完整备份现库作为 rollback，再验证候选 backup 的 `quick_check` 和锁定 schema，最后在
同一目录原子替换。恢复失败不得删除原库。回退时交换 `--backup` 与上一轮生成的
`--rollback-backup`，仍需停机确认。

## 5. PostgreSQL base backup、WAL 与 PITR

Paper/Live 目标为 `RPO <= 300 秒`、`RTO <= 60 秒`。在隔离的 PostgreSQL 17 环境启用
`archive_mode=on`、可靠的 `archive_command`，并将 `archive_timeout` 设为不大于 300 秒。每次
base backup 使用单独目录：

```bash
pg_basebackup --dbname="$ALPHAMIND_PG_BACKUP_URL" \
  --pgdata="$BACKUP_DIR" --format=plain --wal-method=stream \
  --checkpoint=fast --manifest-checksums=SHA256 --progress
pg_verifybackup "$BACKUP_DIR"
```

恢复演练必须在隔离端口/主机执行，不能覆盖正在服务的 cluster：复制已验证 base backup，配置
只读 WAL archive 的 `restore_command`，创建 `recovery.signal`，按需要设置
`recovery_target_time`/timeline，启动后验证 Runtime schema、open trades/orders 与 Exchange facts。
只有实测恢复时间小于 60 秒且最新已提交事实落后不超过 300 秒，才允许把候选 cluster 提升并
切换 Freqtrade DSN；否则保持旧 cluster 和 close-only。Audit Writer 始终最后恢复。

## 6. 升级与 migration rollback

1. 冻结新入场并等待/处置 open order；保存 Exchange facts、整库 backup 和 schema fingerprint；
2. 在隔离副本中启动目标 Freqtrade 版本，让 **Freqtrade 自身**执行 migration；
3. 运行锁定 callback、schema、open trade/order/fill 恢复和 Exchange reconcile 验证；
4. 失败时停止候选，恢复升级前整库/cluster 和原镜像；禁止手写 down migration；
5. 成功后记录版本、source commit、backup hash、RPO/RTO、操作者和审计事件，再解除 close-only。

## 7. 演练证据

P3-04 的锁定容器合同测试使用 Freqtrade 2026.6 真实 model/schema，在 commit 后以非正常进程退出
留下 open trade、partially-filled open order 和 filled order，再由新 persistence session 恢复；随后
执行 SQLite online backup、schema/integrity 校验和新路径 restore。该证据只证明锁定版本与本地
SQLite 恢复链，不代替 Paper/Live 的 PostgreSQL 规模、存储和真实 Exchange reconcile 演练。
