# ADR-0005：冻结首个策略的数据与 Final Holdout 合同

| 元数据 | 内容 |
|---|---|
| 状态 | DONE |
| 任务 | P0-05 |
| 制定日期 | 2026-07-15 |
| 独立评审 | 项目所有人 misakimei123，2026-07-16 |
| 评审基线 | `main@7a9d124` |
| 评审结论 | 作为 P0-08 Scope Frozen 批准的一部分，无修改接受 |
| 前置决策 | ADR-0002、ADR-0003、ADR-0004 |
| Regime Manifest | `data/manifests/regime-manifest.yaml` |
| 冻结时点 | 下载目标数据、运行候选回测或查看收益结果之前 |

## 1. 数据范围

首个策略只使用 Bybit 现货公开 OHLCV，不引入合约、Funding、Order Book、新闻或其他交易所数据：

| 项目 | 冻结值 |
|---|---|
| 交易所 | `bybit` |
| 市场和 candle type | `spot` |
| 标的 | `BTC/USDT`、`ETH/USDT` |
| 时区 | UTC |
| 主周期 | `4h` |
| 稳健性周期 | `1d`，只运行 ADR-0004 基线参数 |
| 数据起点（含） | `2022-01-01T00:00:00Z` |
| 数据终点（不含） | `2026-07-01T00:00:00Z` |
| 开发池终点（不含） | `2025-07-01T00:00:00Z` |
| Final Holdout | `[2025-07-01T00:00:00Z, 2026-07-01T00:00:00Z)` |

所有区间使用左闭右开语义。选择整年 Final Holdout 是基于预注册日历边界和 4h 低频策略的事件容量，而不是候选表现；若信号或独立事件不足，结果只能是 `INCONCLUSIVE`，不得缩短区间或降低 ADR-0004 门槛。

## 2. Source Snapshot 与下载合同

锁定的 Freqtrade `2026.6` 官方文档支持通过 `download-data` 指定 exchange、pairs、timeframes、timerange 和 OHLCV 格式，并说明默认格式为 Feather。本项目固定使用：

```powershell
freqtrade download-data --exchange bybit --trading-mode spot `
  --pairs BTC/USDT ETH/USDT --timeframes 4h 1d `
  --timerange 20220101-20260701 --data-format-ohlcv feather `
  --datadir data/source/bybit_spot/<snapshot_id>
```

该命令由 P1-03 在新的空目录执行，不使用 `--erase`，也不增量覆盖既有 snapshot。实际命令、锁定运行时、下载时间、交易所 metadata、文件清单、文件大小、首末 candle、行数和 SHA-256 写入符合 `data-manifest.schema.yaml` 的 manifest。

MVP 的 source snapshot 定义为“Freqtrade 从 Bybit OHLCV API 下载后首次落盘的 Feather 文件集合及其 manifest”。不额外保存每次 REST 响应的原始 payload，避免开发第二套采集器；若未来要求原始 payload，必须新建数据版本和决策，不能补写到既有 snapshot。

目录和写入规则：

```text
data/source/bybit_spot/<snapshot_id>/    # 只追加、原始字节不改写
data/clean/<dataset_id>/                 # 质量通过后的规范化输出
data/features/<dataset_id>/<version>/    # 可重建特征
data/final_holdout/<dataset_id>/         # 一次性执行时才物化/挂载
```

`source`、`clean`、`features` 和 `final_holdout` 不能使用同一路径、硬链接或原地覆盖。仓库只提交 schema 和 manifest 模板/登记，不提交实际市场数据。

## 3. Hash 与不可变策略

- 单文件 hash：对落盘原始字节计算小写十六进制 SHA-256；
- snapshot hash：将 manifest 中按 UTF-8 路径字典序排列的 `relative_path + ':' + file_sha256 + '\n'` 串联后计算 SHA-256；
- clean dataset hash：对规范化分区文件使用同一算法，并在 manifest 中引用 source snapshot hash；
- manifest 自身规范化为 UTF-8、LF、键序稳定的 JSON 后计算 `manifest_content_sha256`，计算时排除该字段自身；
- hash、大小、行数或首末时间任一不符即视为新版本或损坏，禁止就地修补；
- source snapshot 只允许创建新 ID，不允许覆盖、删除异常行或重新保存 Feather 文件。

`snapshot_id` 格式固定为 `bybit-spot-ohlcv-YYYYMMDDTHHMMSSZ-<sha256前12位>`。实际 hash 只能在 P1-03 下载后填写，P0-05 冻结的是算法、字段和不可变规则。

## 4. 清洗与质量处置

Freqtrade `2026.6` 的 `load_pair_history`/`load_data` 默认 `fill_up_missing=True`，会使用前收盘价生成零成交量 candle。该行为与本项目证伪合同冲突，因此：

- source QA 和 clean 构建必须以 `fill_missing=False` 读取；
- 任何 backtest 前必须证明目标区间无缺口，禁止依赖 Freqtrade 默认补缺；
- 缺失 candle、重复时间戳、乱序、非 UTC、非网格对齐、非有限数值、非正价格、负成交量或非法 OHLC 关系均记录为 `ERROR`；
- source 永不修改；有 `ERROR` 的 pair/timeframe 分区不得进入 clean、feature 或 backtest；
- 不自动去重、不前向填充、不插值、不用其他交易所补洞；重新下载只能生成新 snapshot 并保留旧证据；
- `volume == 0` 可以保留为观测值，但必须标记 `WARNING` 并进入流动性诊断；
- 当前尚未结束的最后一根 candle 必须丢弃并记录，不算数据缺口；
- 异常跳变只报告，不静默 winsorize、截断或删除。

OHLCV 语义由 `data/schemas/ohlcv.schema.yaml` 固定；跨字段 OHLC 关系和时间网格由 P1-04 质量检查器验证。

## 5. Walk-Forward

开发池采用 expanding train、固定 6 个月 validation、每次前移 6 个月：

| Fold | Train（左闭右开） | Validation（左闭右开） |
|---|---|---|
| WF-01 | 2022-01-01 ～ 2024-01-01 | 2024-01-01 ～ 2024-07-01 |
| WF-02 | 2022-01-01 ～ 2024-07-01 | 2024-07-01 ～ 2025-01-01 |
| WF-03 | 2022-01-01 ～ 2025-01-01 | 2025-01-01 ～ 2025-07-01 |

- 训练窗口用于 one-at-a-time 参数选择、成本假设和风险估计；
- validation 只能用于已登记 trial 的样本外评价；
- 4h 与 1d 使用相同日历边界，1d 不产生额外参数选择权；
- 指标 warm-up 可以读取 fold 边界前的数据，但不得把 validation 或未来 candle 反向带入 train；
- 禁止随机拆分；后续模型若引入重叠标签，必须另行冻结 purge/embargo。

## 6. Final Holdout 隔离

Final Holdout 固定为 `[2025-07-01, 2026-07-01)`，初始状态为 `SEALED_UNREAD`：

- P1/P2 的数据质量、开发、参数扰动和成本标定只能读取开发池；
- 只有 P2-01 至 P2-06 完成、候选/参数/仓位/stop/成本模型冻结并获得项目所有人批准后，P2-07 才能读取一次；
- 首次访问必须记录 commit、strategy/config/data/environment hash、命令、操作者和 UTC 时间；
- holdout 结果不能选择参数、过滤器、交易对或成本假设；
- 若结果用于任何实质修改，该区间永久标记 `DEGRADED_TO_DEVELOPMENT`，原结果失去 final 资格，新候选必须预注册另一段从未访问的连续数据；
- 不允许删除失败结果、重置访问计数或以“修复报告”为名重复运行。

## 7. Regime 与 Stress Slice

`data/manifests/regime-manifest.yaml` 同时预注册：

1. 仅使用当时及更早 4h candle 的确定性分类规则；
2. 开发池内基于日历的诊断区间；
3. `rapid_crash > liquidity_stress > bear_trend > bull_trend > sideways_whipsaw > unclassified` 的冲突优先级；
4. holdout 中只允许在一次性执行时应用同一规则，不能根据结果修改阈值。

OHLCV 的成交量只能形成 liquidity proxy，不能证明订单簿深度或可成交性。若某个 ADR-0004 必需 regime 在样本外不存在，结论为 `INCONCLUSIVE`，不得事后移动日期制造覆盖。

## 8. 官方依据与残余风险

- [Freqtrade 2026.6 数据下载文档](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/docs/data-download.md)：下载参数、timerange 和 Feather 默认格式；
- [Freqtrade 2026.6 history loader](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/freqtrade/data/history/history_utils.py)：默认补缺行为及 `fill_up_missing=False` 入口；
- [Freqtrade 2026.6 OHLCV converter](https://raw.githubusercontent.com/freqtrade/freqtrade/2026.6/freqtrade/data/converter/converter.py)：字段顺序、去重和补缺语义。

当前未下载、读取或回测目标数据，因此尚无实际文件 hash、质量报告或收益证据。文件 hash 和质量报告分别属于 P1-03、P1-04 的强制产物，不被 P0-05 的合同冻结替代。Bybit 可能无法返回完整历史、早期 candle 可能存在缺口，任何一种情况都必须产生新证据或阻塞研究，不能放宽本合同。
