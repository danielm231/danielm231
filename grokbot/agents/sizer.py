"""Agent 3 - SIZER: sizes from current equity, never a fixed ticket."""
from __future__ import annotations

from ..config import SizerConfig


class Sizer:
    name = "SIZER"

    def __init__(self, cfg: SizerConfig):
        self.cfg = cfg
        self.loss_streak = 0
        self.status = "ready"

    def record_result(self, pnl_usd: float) -> None:
        self.loss_streak = self.loss_streak + 1 if pnl_usd < 0 else 0

    @property
    def scaled_down(self) -> bool:
        return self.loss_streak >= self.cfg.loss_streak_for_scale_down

    def size(self, cash: float, equity: float, signal_count: int, open_positions: int
             ) -> tuple[float, str]:
        """Return (usd size, explanation). 0 means: don't trade."""
        c = self.cfg
        if open_positions >= c.max_open_positions:
            return 0.0, f"max {c.max_open_positions} open positions"
        # 2 signals -> 20 %, 3 -> 30 %, 4+ -> 40 %  (compounding: based on equity)
        pct = min(c.max_pct, max(c.min_pct, 0.10 * signal_count))
        note = f"{pct:.0%} of equity ${equity:.2f}"
        if self.scaled_down:
            pct *= c.scale_down_factor
            note += f", halved after {self.loss_streak} losses"
        usd = min(equity * pct, cash)
        if usd < c.min_ticket_usd:
            return 0.0, f"size ${usd:.2f} below minimum ticket"
        self.status = f"last size ${usd:.2f} ({note})"
        return usd, note
