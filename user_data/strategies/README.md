# Strategies

`DonchianTrendStrategy.py` 是唯一的 Freqtrade strategy adapter，锁定接口版本 3、adapter 版本
`0.2.0`、4h Donchian 20/10、long/flat 和稳定 entry/exit tag。

P3-02 已接入本地 RiskSnapshot、P2-03 同源风险定仓、常数时间 `confirm_trade_entry`、entry fill
后的固定 ATR stop 持久化和 bot-managed `custom_stoploss`。快照缺失、过期、损坏、版本不支持，
或 callback 任一步异常时均返回零 stake/拒绝新入场；该状态不能阻止 channel exit、stoploss 或
其他安全退出。运行时快照位于 `user_data/risk/`，不得提交 Git。
