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
