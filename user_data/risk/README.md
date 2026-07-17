# Risk snapshot cache

P3-01 watchdog 将原子发布的 `risk-snapshot.json` 写入此运行时目录；P3-02 strategy 只读该文件。
实际快照包含账户标识和资金状态，不提交 Git。快照缺失、损坏、过期或版本不支持时只禁止新入场，
不能阻止止损、channel exit 或其他安全退出。
