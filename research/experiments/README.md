# Experiment registry

本目录保存 P1-06 的 append-only 实验记录。当前
[`trial-registry.json`](trial-registry.json) 只冻结 Donchian Strategy Card、14 次试验预算和
“失败 trial 必须保留”规则；在 P2-05 创建首个预注册 trial 前，`entries` 必须保持为空。

## Lifecycle

1. 使用 [Experiment schema](../../data/schemas/experiment.schema.yaml) 创建
   `PRE_REGISTERED` JSON spec；commit、config/data/environment hash、seed、成本模型版本和四类
   slice 必须完整。
2. 运行 `uv run python scripts/manage_experiment.py register --spec <spec.json>`。命令创建一次性
   `registration.json`、固定章节的预注册报告和 artifact manifest，并追加 registry entry。
3. 运行实验后使用 `finalize` 追加 completion、交易列表、指标、最终报告和 manifest。`FAIL`、
   `REJECTED`、`INCONCLUSIVE`、`INVALIDATED` 与通过结果走相同路径，不允许删除或复用 trial index。
4. 独立评审使用 `review` 追加评审文件；只有 `COMPLETED + PASS + APPROVED` 且全部 hash 复核通过的
   experiment 才能进入策略选择。

## Verification

`uv run python scripts/manage_experiment.py verify <experiment-id>` 会从 registry 定位当前报告和
manifest，复核 registration semantic hash、manifest content hash 以及所有输入输出的逐字节 SHA-256。
固定报告始终分别列出 `train`、`validation`、`holdout` 和 `stress`，不可用或未授权的 slice 明确写为
`None`，不得与开发证据混报。

结构合同分别由 [Hypothesis schema](../../data/schemas/hypothesis.schema.yaml)、
[Strategy Card schema](../../data/schemas/strategy-card.schema.yaml)、
[Trial Registry schema](../../data/schemas/trial-registry.schema.yaml) 和
[Artifact Manifest schema](../../data/schemas/artifact-manifest.schema.yaml) 定义。
