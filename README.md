# alphaMind

alphaMind 是个人 AI 加密货币交易系统。目标运行方式是：默认每 30 分钟读取 Bybit 账户、持仓、挂单、行情、合约风险和加密货币资讯，由 AI 生成结构化交易动作，经 Telegram 人工批准后自动通过 Freqtrade 执行并回报结果。

当前 MVP 范围包括：

- 配置化 BTC、ETH、SOL、HYPE，不在业务代码中固定币种；
- Bybit 现货与 USDT 线性永续合约；
- 现货 long，合约按配置支持 long/short 和最大杠杆；
- `HOLD/OPEN/ADD/REDUCE/CLOSE/CANCEL_ORDER` 等 AI 候选动作；
- Telegram 白名单、审批 TTL、幂等、执行前复核和结果通知；
- Freqtrade 继续作为唯一 Bybit 交易写入者，AI 与 Telegram Bot 均不直接持有交易密钥。

仓库在旧 4h Donchian 研究、风险、审计和 Runtime DB 底座上，已经完成 R0/R1 的产品合同、
配置化市场与只读周期观察，以及 AI 决策合同绑定、新闻采集和模型 provider。R2-04 已完成逐动作
业务校验、拒绝报告与审批候选过滤；Telegram、ExecutionGateway 和批准后交易闭环仍按
[唯一开发计划](docs/development-plan.md) 推进。

仓库不包含凭据，也没有可启动 Live 的 Compose service；当前实现包括：

- point-in-time Donchian 信号；
- Freqtrade 2026.6 strategy adapter：本地 RiskSnapshot 缓存、P2-03 同源定仓、常数时间入场确认、
  ATR(20) x 2 固定 bot-managed stoploss 与 fail-closed；
- 现金、BTC/ETH Buy-and-Hold、50/50 与 SMA(200) 工程基准及统一绩效指标；
- maker/taker fee、spread、slippage、next-candle fill 与压力场景模型；
- 三个 expanding validation fold、13 个 OAT trial、bootstrap 与 Deflated Sharpe 报告；
- 基于风险预算和多重暴露上限的仓位计算；
- 从 `configs/common/risk-limits.toml` 加载的账户级绝对损失门禁；
- Runtime/AI/新闻/Instrument Registry/Market Capability 有效配置 Loader，以及由 Registry
  确定性生成的 Freqtrade 现货 pairlist；
- 通过 Bybit 无认证公开接口刷新 spot/linear 市场规则快照；当前风险适配器从快照派生
  BTC/ETH/SOL/HYPE 的价格精度、数量步长、最小名义金额和杠杆上限，不再维护静态 pair 约束；
- RiskSnapshot v2 离线只读风险会计，覆盖现货、合约 long/short、挂单、mark/liq、
  保证金与 funding，并保留原子发布和 fail-closed 读取；
- 默认 30 分钟 UTC 对齐、基于 `filelock` 跨进程不可重叠的只读周期调度器；支持手工触发、超时、SQLite
  状态记录和原子 JSON 快照，RiskSnapshot 缺失时明确保持 close-only；
- NewsItem、DecisionContext、ModelDecision 与 TradeAction 的严格运行时绑定：拒绝不支持的版本、
  配置或周期不匹配、伪造新闻来源、非本周期引用、重复 ID、不可用市场及不允许的动作；绑定结果
  使用规范化 JSON 和 SHA-256 固化，并绑定生成 decision 的精确 Context hash；
- 确定性 Action 业务校验：逐动作检查审批 TTL、新闻相关性、方向/杠杆、仓位状态、亏损加仓、价格
  漂移与 tick、long/short 止损止盈几何和保护单不可放宽；输出稳定拒绝 code，只有通过全部规则的动作
  才能成为后续审批候选，全部拒绝时 provider fail-closed 为 `HOLD_ONLY`；
- 配置化 Bybit V5 公告与 RSS/Atom 新闻适配器：`httpx` 处理 HTTP，`feedparser` 统一 Feed，
  BeautifulSoup 清洗 HTML；项目叠加 HTTPS 同源、Content-Type、响应大小、DTD/entity 和请求超时
  边界，使用 ETag/Last-Modified、发布时间高水位和原子状态文件增量抓取，按 canonical
  URL/title/content 跨周期去重，并从 Instrument Registry 关联资产；
- OpenAI 官方 Python SDK 驱动的 OpenAI Responses 与 DeepSeek Chat Completions provider：OpenAI
  使用服务端严格 JSON Schema，DeepSeek 使用 JSON Output 后再执行完整本地 schema/binder；两条路径均
  不启用 tools，OpenAI 固定 `store=false`，DeepSeek 不发送其 Chat Completions 未定义的 storage 参数；
  90 秒超时、最多两次项目级重试、失败时 `HOLD_ONLY`，并以 SQLite WAL 在请求前原子预留成本；
- 仓库质量门禁使用 `markdown-it-py` 解析 CommonMark 本地链接，并使用 Yelp `detect-secrets` 插件与
  指纹 baseline 检测新增凭据；不回显疑似 secret；
- `Freqtrade 2026.6`、`CCXT 4.5.61` 与官方 Docker digest 的版本锁；
- 对应单元测试与静态检查。

本地验证（Windows PowerShell）：

```powershell
uv sync --locked --extra dev
uv run python scripts/check_repository.py
uv run python scripts/render_instrument_configs.py --check
uv run python scripts/refresh_market_capabilities.py --check
uv run mypy
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv lock --check
git diff --check
```

首次启动 dry-run 或能力快照超过 24 小时后，先通过 Bybit 公共市场接口刷新快照：

```powershell
uv run python scripts/refresh_market_capabilities.py
uv run python scripts/refresh_market_capabilities.py --check
```

刷新操作不读取 API Key，也不会访问账户或订单接口。一次完整请求或分页失败时不会覆盖旧快照；
单个配置市场缺失或规则非法时，仅在新快照中将该市场标记为不可用。快照是时点数据，交易所的
最大下单数量、精度和状态发生变化后必须重新刷新。

手工执行一次只读新闻采集：

```powershell
$env:ALPHAMIND_NEWS_USER_AGENT = "alphaMind/0.1 your-contact@example.com"
uv run collect-news --pretty
```

游标和跨周期去重指纹原子写入 `user_data/state/news-cursors.json`。外部标题、摘要和标签始终作为
不可信数据清洗；单个来源失败不会污染其他来源，但健康来源数不足时
`risk_increase_news_available=false`。该命令不读取 Bybit API Key，不抓取文章全文，也不调用模型、
Telegram 或交易接口。

离线校验模型配置、Prompt、严格输出 schema 和密钥状态（不发请求、不打印密钥）：

```powershell
uv run run-ai-decision --check --pretty
```

真实只读模型调用还需要当期已绑定且未过期的 DecisionContext；usage 与成本写入
`user_data/state/ai-usage.sqlite`。该调用只产生 HOLD 或候选 Action，不持有 Bybit 交易密钥，也不创建、
修改或取消订单：

```powershell
$env:OPENAI_API_KEY = "<development-key>"
uv run run-ai-decision --context <current-decision-context.yaml> --pretty
```

使用版本化 DeepSeek 测试 profile 时，base URL、模型 ID、non-thinking 和官方价格均来自配置文件；
`ALPHAMIND_AI_PROFILE_PATH` 只允许切换到仓库审核过的 profile：

```powershell
$env:DEEPSEEK_API_KEY = "<development-key>"
$env:ALPHAMIND_AI_PROFILE_PATH = "configs/alphamind/ai-profile.deepseek-test.yaml"
uv run run-ai-decision --check --pretty
uv run run-ai-decision --context <current-decision-context.yaml> --pretty
```

DeepSeek 的 JSON Output 只保证合法 JSON，不等价于 OpenAI Responses 的服务端严格 JSON Schema；所有
返回仍必须通过本地 `ModelDecision/TradeAction` 完整绑定，否则重试或 fail-closed 为 `HOLD_ONLY`。

手工生成一次只读周期快照、查看状态或持续按配置调度：

```powershell
uv run run-cycle-scheduler
uv run run-cycle-scheduler --status
uv run run-cycle-scheduler --daemon
```

成功快照写入 `user_data/cycles/`，调度元数据写入 `user_data/state/cycle-scheduler.sqlite`。周期只读取
有效配置、Market Capability 和已有 RiskSnapshot，不调用新闻、模型、Telegram 或交易接口。手工触发
与定时周期冲突时记录 `SKIPPED_OVERLAP`；任务超时后保留锁直到实际 worker 退出，避免下一周期重叠。

唯一任务状态、下一任务、完整阶段和开发边界见 [开发计划](docs/development-plan.md)。其他文档不维护独立进度。

## 锁定的 Freqtrade 环境

Docker Desktop 切换到 Linux containers 后，可以只运行无密钥、无交易写权限的 P1-02 验证：

```powershell
docker compose --profile tools run --rm runtime-check
docker compose --profile tools run --rm contract-check
docker compose --profile tools run --rm freqtrade-cli --version
docker compose --profile tools run --rm freqtrade-cli list-exchanges --all
```

Compose 使用 `configs/common/runtime-versions.toml` 锁定的 `linux/amd64` platform digest。所有服务都必须显式选择 profile。R1-05 提供隔离的 `spot-dry-run` 与 `futures-dry-run` 服务：

```powershell
docker compose --profile spot-dry-run up spot-dry-run
docker compose --profile futures-dry-run up futures-dry-run
# 同时启动两个隔离实例
docker compose --profile dry-run up spot-dry-run futures-dry-run
```

两个实例分别消费 spot/futures API key 环境变量、bot identity、pairlist 和 Runtime DB；默认均以 `initial_state=stopped` 启动，且不暴露 Freqtrade API 或 Telegram。`spot.live.template.json` 与 `futures.live.template.json` 都没有凭据，也没有对应 Compose service，R6 批准前不能通过本项目 Compose 启动 Live。

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

P1-05 使用同一 P1-04 clean snapshot、统一成本配置和时间边界生成基准报告：

```powershell
docker compose --profile research run --rm benchmark-report
```

报告同时包含现金、BTC/ETH Buy-and-Hold、初始 50/50 不再平衡组合，以及严格按
“已完成 candle close 产生信号、下一根 candle open 执行”的 SMA(200) long/flat 工程基准。
Bybit Non-VIP spot fee 与固定点差/滑点假设集中保存在
`configs/research/benchmark-v1.toml`；后两项只是 candle 数据下的工程假设，不冒充真实成交。

生成后使用同一无网络容器重新计算 clean 数据结果并独立复核报告：

```powershell
docker compose --profile research run --rm benchmark-report `
  /workspace/scripts/build_benchmark_report.py `
  --project-root /workspace `
  --verify-report /workspace/research/reports/benchmarks/<report_id>/report.json
```

P2-04 的成本、成交和 11 项压力假设集中保存在
`configs/research/execution-model-v1.toml`。以下命令只读取版本化配置并生成确定性报告，不访问网络、
钱包或交易所：

```powershell
uv run python scripts/build_execution_model_report.py --project-root .
```

报告写入 `research/reports/execution-model/p2-04-v1/`，明确区分 historical backtest、dry-run
和 Live Canary 的证据边界；candle touch 不会被自动视为真实 limit fill。

P2-05 已在无网络锁定容器中一次性预注册并运行 13 个 OAT 参数 trial。当前 registry 非空，禁止再次
执行 `--build`；以下命令重新读取 clean Feather，逐项复算交易、指标、artifact hash 和汇总报告：

```powershell
docker compose --profile research run --rm walk-forward-report `
  /workspace/scripts/build_walk_forward_report.py `
  --project-root /workspace `
  --verify
```

汇总产物位于 `research/reports/walk-forward/p2-05-v1/`。P2-05 统计检查通过不等于参数已获选择；
13 个 experiment 的 `review_result` 均保持 `PENDING`，P2-06 和独立评审完成前 selection 固定为空。

P2-06 已使用锁定 Freqtrade 2026.6 运行官方 `lookahead-analysis`、`recursive-analysis`，并在
Bybit/OKX 的同标 4h 数据上逐列执行 prefix-invariance 扫描。构建会联网读取公开市场 metadata
和首次下载 OKX snapshot，但不会挂载凭据。报告及 manifest 永久绑定生成时的 strategy source hash；
只有在 `main@cb86fc9` 对应 checkout 中，以下无网络、全只读复核才会原样通过：

```powershell
docker compose --profile research run --rm anti-cheat-verify
```

报告位于 `research/reports/anti-cheat/p2-06-v1/`。P2-06 PASS 只解除自动反作弊阻塞；所有参数
仍保持未选择，必须等待独立评审，且本报告不构成 Backtest Gate、Paper 或 Live 晋升。P3-02
修改当前 adapter 后，旧 verifier 在当前 checkout 报 strategy hash mismatch 是预期的历史证据边界；当前
entry/exit 信号与 P2-01 纯函数的一致性由锁定镜像 `contract-check` 重新验证。
