# Strategies

`DonchianTrendStrategy.py` 是唯一的 Freqtrade strategy adapter，锁定接口版本 3、策略版本
`0.1.0`、4h Donchian 20/10、long/flat 和稳定 entry/exit tag。

P2-02 只映射 point-in-time 信号。P3-02 接入 RiskSnapshot、风险定仓和硬止损 callback 前，
`confirm_trade_entry` 固定返回 `False`；该阶段门禁禁止把当前 adapter 的 signal 输出误当成可执行
Backtest、Dry-run 或 Live 策略结果。
