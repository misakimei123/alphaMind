"""仅供 P2-06 官方分析命令使用的 research-only strategy。"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from user_data.strategies.DonchianTrendStrategy import DonchianTrendStrategy  # noqa: E402


class DonchianTrendAnalysisStrategy(DonchianTrendStrategy):
    """继承生产信号，只在隔离研究命令内解除 P3-02 前的成交拒绝。"""

    # lookahead-analysis 官方流程会强制 market order；显式声明可避免继承默认 limit 产生歧义。
    order_types: ClassVar[dict[str, str | bool]] = {
        "entry": "market",
        "exit": "market",
        "stoploss": "market",
        "stoploss_on_exchange": False,
    }

    def version(self) -> str:
        return f"{super().version()}-p2-06-analysis"

    def confirm_trade_entry(
        self,
        pair: str,
        order_type: str,
        amount: float,
        rate: float,
        time_in_force: str,
        current_time: datetime,
        entry_tag: str | None,
        side: str,
        **kwargs: Any,
    ) -> bool:
        """只允许官方历史分析形成交易；该类不在 live user_data strategy 路径。"""

        del pair, order_type, amount, rate, time_in_force, current_time, entry_tag, side, kwargs
        return True
