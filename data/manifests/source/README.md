# Source snapshot manifests

P1-03 在此保存每个不可变 source snapshot 的 manifest、规范化公开 exchange metadata 和只读结构扫描报告。实际 Feather 文件位于 `data/source/` 并由 Git 忽略。

`*.quality.*` 只记录 source 结构，不代表 P1-04 clean 数据质量门禁已经完成。manifest 和 metadata 一经创建不得覆盖；重新下载必须生成新的 snapshot ID。若不可变 manifest 内的 holdout 状态后来发生变化，使用独立的 `*.holdout-access.json` 追加事件覆盖其资格判断，不改写原 manifest。

## 提交与备份规则

- Git 只提交 manifest、metadata 和质量报告；`data/source/` 中的 Feather 由 `.gitignore` 排除；
- 备份必须按完整 `<snapshot_id>` 目录复制到仓库外的受控存储，禁止合并或覆盖已有同名目录；
- 备份或恢复后必须使用 `--verify-manifest` 复算文件大小、文件 SHA-256、snapshot hash、manifest hash 和 metadata hash；
- 在至少一份仓库外备份通过复核前，不得删除本地 source snapshot。备份介质与生命周期由 P3 运维门禁配置，本仓库不保存其凭据。

## 当前快照

`bybit-spot-ohlcv-20260716T070451Z-ef232b839406` 已完成 4 个分区和全部不可变证据复核。源下载器额外返回了请求右边界后的 212 根 candle；这些原始字节保留不改写，P1-04 必须在新 clean 版本中严格执行 `[2022-01-01, 2026-07-01)` 边界并阻止越界 candle 进入实验。

首次 P1-03 扫描曾读取完整分区的 OHLC/volume 列，违反“P1/P2 数据质量只能读取开发池”的合同。项目所有人选择严格处置：原 Final Holdout 已记录为 `DEGRADED_TO_DEVELOPMENT`，不能再用于 P2-07；后续必须预注册新的未见区间。该 snapshot 仍可作为不可变 source 和开发池输入。
