"""Agent 6 - EQUITY: updates balance and the equity curve on every close."""
from __future__ import annotations

from datetime import datetime

from ..models import ClosedTrade


class EquityTracker:
    name = "EQUITY"

    def __init__(self, start_balance: float):
        self.start_balance = start_balance
        self.cash = start_balance
        self.peak = start_balance
        self.curve: list[tuple[datetime, float]] = []
        self.trades: list[ClosedTrade] = []
        self.status = f"${start_balance:.2f}"

    def mark(self, ts: datetime, equity: float) -> None:
        self.peak = max(self.peak, equity)
        self.curve.append((ts, equity))
        self.status = f"equity ${equity:.2f} (peak ${self.peak:.2f})"

    def on_close(self, trade: ClosedTrade, equity: float) -> None:
        self.trades.append(trade)
        self.mark(trade.closed_at, equity)

    def drawdown_pct(self, equity: float) -> float:
        return 100 * (self.peak - equity) / self.peak if self.peak else 0.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl_usd > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl_usd <= 0)
