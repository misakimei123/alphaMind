# alphaMind

alphaMind 是面向现货 long/flat 策略研究与受控运行的个人量化交易项目。首个工程基线是 BTC/USDT、ETH/USDT 的 4h Donchian 20/10 趋势策略。

当前实现只包含无密钥、无网络、无交易写权限的确定性核心：

- point-in-time Donchian 信号；
- 基于风险预算和多重暴露上限的仓位计算；
- 从 `configs/common/risk-limits.toml` 加载的账户级绝对损失门禁；
- `Freqtrade 2026.6`、`CCXT 4.5.61` 与官方 Docker digest 的版本锁；
- 对应单元测试与静态检查。

本地验证（Windows PowerShell）：

```powershell
uv sync --locked --extra dev
uv run python scripts/check_repository.py
uv run mypy
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check
```

完整阶段、门禁和开发边界见 [开发计划](docs/development-plan.md)。

## 锁定的 Freqtrade 环境

Docker Desktop 切换到 Linux containers 后，可以只运行无密钥、无交易写权限的 P1-02 验证：

```powershell
docker compose --profile tools run --rm runtime-check
docker compose --profile tools run --rm contract-check
docker compose --profile tools run --rm freqtrade-cli --version
docker compose --profile tools run --rm freqtrade-cli list-exchanges --all
```

Compose 使用 `configs/common/runtime-versions.toml` 锁定的 `linux/amd64` platform digest。所有服务都必须显式选择 profile；`live.template.json` 没有凭据，也没有对应 Compose service，P5 批准前不能通过本项目 Compose 启动 Live。

P1-03 只使用 Bybit 公开 OHLCV 接口创建新的不可变 source snapshot：

```powershell
docker compose --profile data run --rm data-snapshot
```

命令不使用 API Key、不运行策略，也不覆盖已有 snapshot。实际 Feather 文件由 Git 忽略；可复核 manifest、公开市场 metadata 和结构扫描报告保存在 `data/manifests/source/`。

已有快照可在不访问网络的情况下重新计算全部不可变证据：

```powershell
docker compose --profile data run --rm data-snapshot /workspace/scripts/create_source_snapshot.py `
  --project-root /workspace `
  --verify-manifest /workspace/data/manifests/source/<snapshot_id>.manifest.json
```

P1-04 使用无网络容器，只将当前可用开发数据的 OHLCV 载入质量流水线。holdout 仍密封时截止
`2025-07-01`；本项目原 holdout 已严格降级为开发数据，因此当前报告覆盖
`[2022-01-01, 2026-07-01)`：

```powershell
docker compose --profile data run --rm data-quality
```

流水线不填补、不去重、不插值、不重排 source。任何 ERROR 都拒绝发布 `data/clean/`；
零成交量和固定阈值的 close 跳变作为 WARN 原样保留。JSON/Markdown 证据写入
`data/manifests/quality/`，实际 clean Feather 继续由 Git 忽略。

生成后使用同一无网络容器独立复核报告与 clean 文件：

```powershell
docker compose --profile data run --rm data-quality /workspace/scripts/build_clean_dataset.py `
  --project-root /workspace `
  --verify-report /workspace/data/manifests/quality/<dataset_id>/report.json
```
