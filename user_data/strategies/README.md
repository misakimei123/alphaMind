# Strategies

`DonchianTrendStrategy.py` 是唯一的 Freqtrade strategy adapter，锁定接口版本 3、adapter 版本
`0.3.0`、4h Donchian 20/10、long/flat 和稳定 entry/exit tag。

P3-02 已接入本地 RiskSnapshot、P2-03 同源风险定仓、常数时间 `confirm_trade_entry`、entry fill
后的固定 ATR stop 持久化和 bot-managed `custom_stoploss`。快照缺失、过期、损坏、版本不支持，
或 callback 任一步异常时均返回零 stake/拒绝新入场；该状态不能阻止 channel exit、stoploss 或
其他安全退出。运行时快照位于 `user_data/risk/`，不得提交 Git。

P3-03 要求每个非零风险批准在返回 stake 前先写入 `user_data/audit/outbox.sqlite`；outbox
不可写、事件超限或 backlog 达冻结停止阈值时拒绝新入场。`confirm_trade_entry` 仍只读取已审计
的内存批准，不访问文件、网络或数据库。
