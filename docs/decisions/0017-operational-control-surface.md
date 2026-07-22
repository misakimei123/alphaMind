# ADR-0017：运行控制面分离暂停 AI、停止新开仓与紧急模式

- 状态：Accepted
- 日期：2026-07-22
- 关联任务：R3-06

## 背景

R3-01 至 R3-05 已完成候选动作、Telegram 授权、执行前重新校验和结果通知合同，但系统仍缺少可持久化、
可审计的运行控制入口。单一“暂停”布尔值会混淆三个不同意图：停止模型网络请求、阻止扩大风险，以及在
紧急情况下要求撤销待处理入场并转入人工复核。

调度器同时承担只读观察和风险快照采集，直接停止调度会让风险监控一起失效。R4 ExecutionGateway 尚未
落地，因此 Telegram 紧急入口也不能声称已经撤单、平仓或改变交易所状态。

## 决策

1. 新增独立 `OperationalControlSnapshot v1`，区分 `ai_paused`、`entry_stopped` 和 `emergency`。
   任一状态都固定保留 `safe_exit_allowed=true`；紧急模式必须同时暂停 AI、停止新开仓、标记待处理入场
   需要取消并要求人工复核；
2. 控制事实写入独立 SQLite FULL/WAL store。每个 Telegram update 使用稳定幂等键；事件正文和投影保存
   SHA-256，并在每次读取时从不可变事件顺序重放。异文幂等冲突、历史篡改或数据库不可读均 fail-closed；
3. Telegram 控制入口只接受普通 `Update.message` 的精确命令：`/status`、`/pause_ai`、`/resume_ai`、
   `/stop_entries`、`/resume_entries` 和 `/emergency`。user 与 chat 必须同时命中当前环境 allowlist；原始 ID
   不落控制库；
4. `/resume_entries` 必须重新读取可信 RiskSnapshot，且同时满足 `entry_allowed=true`、
   `close_only=false`、`kill_switch=false`。紧急模式不能通过普通 Telegram resume 命令解除，必须完成
   Runbook 的人工对账与复核；
5. AI CLI 在创建 provider 或发出网络请求前读取控制状态。`ai_paused=true` 时返回稳定 `paused` 结果，
   明确 `network_request_sent=false`；配置检查仍可离线执行；
6. 调度器不停止只读周期，而是在快照中同时发布原始控制投影和叠加后的有效风控状态。停止新开仓会使
   有效 `entry_allowed=false`、`close_only=true`；紧急模式还使有效 `kill_switch=true`；
7. 执行前重新校验为 OPEN/ADD 接受控制读取器：停止新开仓或紧急模式会取消候选；控制状态不可读时同样
   fail-closed。REDUCE/CLOSE 等安全退出不因该读取失败被禁止；
8. R3-06 不拥有订单写接口。紧急回执必须明确“交易指令未发送”；`cancel_pending_entries` 是交给 R4
   ExecutionGateway 的强制处置意图，不是已经撤单的事实。

## 后果

- 暂停模型不会停止风险观察或安全退出，停止新开仓也不会被误解释为停止整个进程；
- 控制状态跨进程重启保持，并能检测事件或投影篡改；
- R4 必须在提交任何风险增加订单前读取同一控制状态，并将紧急模式的撤销意图映射为可对账的真实订单
  事实；
- 紧急模式恢复当前保持 fail-closed。后续恢复入口必须绑定人工复核证据、最新 RiskSnapshot 和 R4 对账
  结果，不能复用普通 Telegram resume 命令。

## 被否决方案

- **停止整个调度器**：会同时停止只读观察和风险监控；
- **只使用进程内布尔值**：重启后丢失，也不能审计重复命令或检测篡改；
- **紧急命令直接调用交易所**：R4 尚无 ExecutionGateway 与对账事实，会制造第二个订单写入者；
- **紧急模式自动恢复**：不能证明未成交入场、部分成交和持仓已经完成对账。
