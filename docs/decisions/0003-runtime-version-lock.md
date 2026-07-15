# ADR-0003：运行环境与版本锁定

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-03 |
| 制定日期 | 2026-07-15 |
| 配置权威 | `configs/common/runtime-versions.toml` |

## 1. 决策

alphaMind 本地研究环境和 Freqtrade 运行环境分别锁定：

| 组件 | 锁定值 |
|---|---|
| 本地研究 Python | 3.12.9 |
| uv | 0.11.7 |
| alphaMind Python 兼容范围 | `>=3.12,<3.15` |
| Windows 3.14 兼容性测试 | 3.14.4 |
| Freqtrade | 2026.6 |
| Freqtrade source commit | `b604e2fd70539f7f73d3c62c16ce0b155bbab319` |
| Freqtrade container Python | 3.14.6 |
| CCXT | 4.5.61 |
| 生产候选平台 | linux/amd64 |
| Docker platform digest | `sha256:1e9298ae0895531fd47c4f13d10e5708b3b8b6e5241292f364fc23f201b5acaa` |
| Docker multi-arch manifest | `sha256:d451af021d5e08b70580c0eea5848534e9846b57391b34821c0a5814416397e6` |

运行配置必须使用完整平台 digest：

```text
freqtradeorg/freqtrade@sha256:1e9298ae0895531fd47c4f13d10e5708b3b8b6e5241292f364fc23f201b5acaa
```

禁止使用会漂移的 `stable`、`latest` 或仅版本 tag 作为 Paper/Live 实际运行引用。

## 2. 依据

- [官方安装文档](https://www.freqtrade.io/en/stable/installation/)要求 Python 3.11+，并建议 Windows 使用 Docker；
- [2026.6 source tag](https://github.com/freqtrade/freqtrade/tree/2026.6)解析到上述完整 commit；
- [2026.6 pyproject](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/pyproject.toml)声明 Python `>=3.11`；
- [2026.6 requirements](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/requirements.txt)固定 `ccxt==4.5.61`；
- [2026.6 Dockerfile](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/Dockerfile)使用 `python:3.14.6-slim-trixie`；
- [官方 Docker tag](https://hub.docker.com/r/freqtradeorg/freqtrade/tags?name=2026.6)提供多架构和 linux/amd64 digest。

选择官方镜像而不是自建 Python 3.12 Freqtrade 镜像，减少系统依赖、TA-Lib 和运行时组合的维护面。alphaMind 纯函数以最低支持版本 Python 3.12 开发，并额外在 Python 3.14 验证兼容性。Windows 上 `uv` 当前只能提供 3.14.4，已通过全量单元测试；官方镜像内精确 3.14.6 仍由下述 Docker 命令验证。

## 3. 验证命令

本地研究环境：

```powershell
uv run python scripts/verify_runtime_lock.py --target research
uv run pytest
```

Docker daemon 可用后执行只读容器验证：

```powershell
docker run --rm --platform linux/amd64 --entrypoint python `
  -v "${PWD}:/workspace" -w /workspace `
  freqtradeorg/freqtrade@sha256:1e9298ae0895531fd47c4f13d10e5708b3b8b6e5241292f364fc23f201b5acaa `
  scripts/verify_runtime_lock.py --target freqtrade
```

该命令只读取版本，不加载交易配置、不访问交易所，也不创建订单。

## 4. 延后但不可跳过的验证

当前 Docker Desktop daemon 未运行，因此尚未实际拉取 digest 或在容器内执行 Freqtrade/CCXT 版本检查。经项目所有人于 2026-07-15 明确决定，该项迁移为 P1-02 的强制验收，不再阻塞 P0-03、P0-04、P0-05 或其他无密钥离线开发，因此 P0-03 标记为 `DONE`。

此延期不等于接受未知运行时：固定 digest 容器验证完成前，P1-02 不得标记 DONE，不得启动 Paper/Live，也不得用 Windows Python 3.14.4 的兼容测试冒充容器内 Python 3.14.6、Freqtrade 2026.6 和 CCXT 4.5.61 的实际证据。验证通过后必须保存命令输出和镜像 inspect 证据。

## 5. 升级规则

- 升级 Freqtrade 时必须同时重新核对 Python、CCXT、callback 签名、Bybit spot 支持和 stoploss 能力；
- 任何 digest 变化都视为运行环境变更，即使 tag 名未变化；
- Backtest、Paper 或 Live 期间不得静默升级；
- 升级后必须重新执行单元、adapter、lookahead、recursive 和 dry-run 验证。
