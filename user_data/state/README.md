# Scheduler state

`cycle-scheduler.sqlite` 只记录周期触发、开始、超时、完成和快照引用。`ai-usage.sqlite` 记录模型调用的
预算预留、token usage、成本、脱敏 provider ID 与版本 hash；不保存 API Key、raw prompt、
DecisionContext 或 raw response。两者都不成为交易、订单或账户事实权威，运行文件不提交 Git。
