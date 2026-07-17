# P2-06 自动反作弊报告

- Assessment: **PASS**
- Freqtrade lookahead bias: `False`
- Lookahead checked signals: `100`
- Recursive maximum variance: `0.0%`
- Prefix-invariance mismatches: `0` across Bybit and OKX
- Signal/next-candle trades checked: `1397`
- Common cross-exchange candles: `{'BTC/USDT': 7662, 'ETH/USDT': 7662}`
- Final Holdout access count: `0`
- Parameter selection: still blocked by independent review; this report does not select parameters

官方命令的完整 command、stdout、stderr、exit code 和 lookahead CSV 保存在 `raw/`。
OKX 只用于同标的数据路径/未来依赖复测，不以收益或信号数量筛选参数。
