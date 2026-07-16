# P2-05 Walk-Forward report

- Model: `p2-05-v1`
- Trials: `13` preregistered OAT candidates
- Folds: expanding train, three contiguous six-month validation windows
- Final holdout: not used; all validation ends before `2025-07-01`
- Assessment: `PASS`
- Selection: blocked until independent review and P2-06 anti-cheat checks

| Trial | Parameters | OOS expectancy R | Trades | Events | MDD | DSR probability |
|---|---|---:|---:|---:|---:|---:|
| baseline | 20/10/ATR 20 x 2.0 | 0.123626724896 | 107 | 61 | 0.044717683460 | 0.643399807168 |
| entry_16 | 16/10/ATR 20 x 2.0 | 0.085183146727 | 122 | 68 | 0.048026703777 | 0.586750184201 |
| entry_18 | 18/10/ATR 20 x 2.0 | 0.085886264736 | 115 | 64 | 0.045944504153 | 0.584005516874 |
| entry_22 | 22/10/ATR 20 x 2.0 | 0.103954468261 | 105 | 58 | 0.045447904388 | 0.605767280091 |
| entry_24 | 24/10/ATR 20 x 2.0 | 0.121652942184 | 100 | 56 | 0.045433115675 | 0.631591586152 |
| exit_8 | 20/8/ATR 20 x 2.0 | 0.142516034773 | 110 | 61 | 0.043050781973 | 0.707633240337 |
| exit_9 | 20/9/ATR 20 x 2.0 | 0.144507214787 | 108 | 61 | 0.044218683434 | 0.687170218064 |
| exit_11 | 20/11/ATR 20 x 2.0 | 0.169695807335 | 105 | 60 | 0.044218683434 | 0.711584931136 |
| exit_12 | 20/12/ATR 20 x 2.0 | 0.164157235779 | 103 | 59 | 0.042837201047 | 0.695577351691 |
| stop_1_6 | 20/10/ATR 20 x 1.6 | 0.142502670639 | 111 | 61 | 0.051208679518 | 0.646037338632 |
| stop_1_8 | 20/10/ATR 20 x 1.8 | 0.144775950244 | 108 | 62 | 0.047866409875 | 0.662790787823 |
| stop_2_2 | 20/10/ATR 20 x 2.2 | 0.142333212764 | 103 | 61 | 0.042945908676 | 0.685090241389 |
| stop_2_4 | 20/10/ATR 20 x 2.4 | 0.152913778322 | 100 | 61 | 0.040953112455 | 0.711395127255 |

## Assessment

- Neighbor nonnegative ratio: `1.000000000000`
- Falsification reasons: `[]`
- Evidence gaps: `[]`
- Profit concentration, Top 5 trades, pair contribution, fold metrics, bootstrap CI and calendar slices are retained in each trial `metrics.json`.
