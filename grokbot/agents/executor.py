"""Agent 4 - EXECUTOR: entries, take-profit ladder, stops.

Default broker is a paper broker (simulated fills with price impact + fees).
No real funds are touched.
"""
from __future__ import annotations

from datetime import datetime

from ..config import ExecutorConfig
from ..models import BrokerError, ClosedTrade, PairSnapshot, Position, Signal
from .equity import EquityTracker

from .logger import TradeLogger


class PaperBroker:
    """Simulated swaps against a constant-product pool."""

    def __init__(self, fee_pct: float, extra_slippage_pct: float = 0.0):
        self.fee_pct = fee_pct
        self.extra = extra_slippage_pct

    @staticmethod
    def impact_pct(usd: float, liquidity_usd: float) -> float:
        # liquidity_usd counts both sides of the pool -> one side is half
        side = max(liquidity_usd / 2, 1.0)
        return 100 * usd / (side + usd)

    def buy(self, usd: float, s: PairSnapshot) -> tuple[float, float, float]:
        """Return (tokens received, avg fill price, usd spent)."""
        impact = self.impact_pct(usd, s.liquidity_usd)
        fill = s.price * (1 + (impact + self.extra) / 100)
        tokens = usd * (1 - self.fee_pct / 100) / fill
        return tokens, fill, usd

    def sell(self, tokens: float, s: PairSnapshot) -> tuple[float, float]:
        """Return (usd received, avg fill price)."""
        if s.lp_pulled or s.honeypot:
            return 0.0, 0.0
        gross = tokens * s.price
        impact = self.impact_pct(gross, s.liquidity_usd)
        fill = s.price * (1 - (impact + self.extra) / 100)
        return tokens * fill * (1 - self.fee_pct / 100), fill


class Executor:
    name = "EXECUTOR"
    MAX_SELL_FAILURES = 10

    def __init__(self, cfg: ExecutorConfig, equity: EquityTracker, log: TradeLogger,
                 broker: PaperBroker | None = None):
        self.cfg = cfg
        self.equity = equity
        self.log = log
        self.broker = broker or PaperBroker(cfg.fee_pct, cfg.extra_slippage_pct)
        self.positions: dict[str, Position] = {}
        self.status = "flat"

    # ------------------------------------------------------------------ entries
    def _slippage_ok(self, usd: float, s: PairSnapshot) -> tuple[bool, float]:
        imp = self.broker.impact_pct(usd, s.liquidity_usd)
        return imp <= self.cfg.max_slippage_pct, imp

    def _buy_part(self, pos: Position, usd: float, s: PairSnapshot, now: datetime,
                  part: int) -> bool:
        ok, imp = self._slippage_ok(usd, s)
        if not ok:
            self.log.log(now, "FILTERED", s.symbol,
                         f"entry part {part}: slippage {imp:.2f}% > {self.cfg.max_slippage_pct}%")
            return False
        try:
            tokens, fill, usd = self.broker.buy(usd, s)
        except BrokerError as e:
            self.log.log(now, "FILTERED", s.symbol, f"entry part {part}: Kauf fehlgeschlagen: {e}")
            return False
        self.equity.cash -= usd
        total_cost = pos.entry_price * pos.tokens + fill * tokens
        pos.tokens += tokens
        pos.initial_tokens = pos.tokens
        pos.entry_price = total_cost / pos.tokens
        pos.invested_usd += usd
        pos.entries_done += 1
        pos.peak_price = max(pos.peak_price, s.price)
        self.log.log(now, "ENTER", s.symbol,
                     f"part {part}/{self.cfg.entry_parts}; signals: {', '.join(pos.signals)}; "
                     f"slippage {imp:.2f}%", size=usd, price=fill)
        return True

    def open(self, sig: Signal, usd: float, now: datetime) -> Position | None:
        s = sig.snapshot
        pos = Position(mint=s.mint, symbol=s.symbol, opened_at=now, entry_price=0.0,
                       target_usd=usd, signals=sig.reasons, first_entry_price=s.price)
        part_usd = usd / self.cfg.entry_parts
        if not self._buy_part(pos, part_usd, s, now, 1):
            return None
        self.positions[s.mint] = pos
        self._update_status()
        return pos

    # --------------------------------------------------------------- management
    def _sell(self, pos: Position, tokens: float, s: PairSnapshot, now: datetime,
              reason: str) -> bool:
        """Sell; on failure the position stays untouched and is retried next tick."""
        tokens = min(tokens, pos.tokens)
        try:
            usd, fill = self.broker.sell(tokens, s)
        except BrokerError as e:
            pos.sell_failures += 1
            self.log.log(now, "ERROR", s.symbol, f"{reason}: {e} "
                         f"(Versuch {pos.sell_failures}/{self.MAX_SELL_FAILURES})")
            return False
        pos.tokens -= tokens
        pos.realized_usd += usd
        self.equity.cash += usd
        self.log.log(now, "EXIT" if pos.tokens <= 1e-12 else "PARTIAL", s.symbol,
                     reason, size=usd, price=fill or None,
                     pnl=pos.pnl_usd if pos.tokens <= 1e-12 else None)
        return True

    def _close(self, pos: Position, s: PairSnapshot, now: datetime, reason: str
               ) -> ClosedTrade | None:
        if pos.tokens > 0 and not self._sell(pos, pos.tokens, s, now, reason):
            if pos.sell_failures < self.MAX_SELL_FAILURES:
                return None                    # still holding - retry next tick
            # unsellable (rug / no liquidity): write off, tokens stay in the wallet
            self.log.log(now, "EXIT", s.symbol, f"abgeschrieben nach {pos.sell_failures} "
                         "fehlgeschlagenen Verkäufen - Token bleibt in der Wallet",
                         size=0.0, pnl=pos.realized_usd - pos.invested_usd)
            reason = f"abgeschrieben ({reason})"
        del self.positions[pos.mint]
        self._update_status()
        return ClosedTrade(pos.symbol, pos.mint, pos.opened_at, now,
                           pos.invested_usd, pos.realized_usd, reason)

    def manage(self, pos: Position, s: PairSnapshot, now: datetime
               ) -> ClosedTrade | None:
        c = self.cfg
        # 1) emergency: dev sells or LP pulled -> out immediately
        if s.lp_pulled:
            return self._close(pos, s, now, "LP pulled - emergency exit")
        if s.dev_sold:
            return self._close(pos, s, now, "dev sold - emergency exit")

        pos.peak_price = max(pos.peak_price, s.price)
        mult = s.price / pos.entry_price

        # 2) second entry part if the trade holds up
        if pos.entries_done < c.entry_parts and pos.tp_hits == 0:
            drop = 100 * (1 - s.price / pos.first_entry_price)
            remaining = pos.target_usd - pos.invested_usd
            if drop <= c.second_entry_max_drop_pct and remaining > 0:
                self._buy_part(pos, min(remaining, self.equity.cash), s, now,
                               pos.entries_done + 1)
                mult = s.price / pos.entry_price
            else:
                pos.entries_done = c.entry_parts  # cancel: price dropped
                self.log.log(now, "FILTERED", s.symbol,
                             f"2nd entry cancelled: price -{drop:.1f}% vs 1st fill")

        # 3) hard stop
        if mult <= 1 - c.hard_stop_pct / 100:
            return self._close(pos, s, now, f"hard stop -{c.hard_stop_pct:.0f}%")

        # 4) take-profit ladder
        while pos.tp_hits < len(c.tp_ladder) and mult >= c.tp_ladder[pos.tp_hits][0]:
            level, frac = c.tp_ladder[pos.tp_hits]
            if not self._sell(pos, pos.initial_tokens * frac, s, now,
                              f"TP{pos.tp_hits + 1} at {level:g}x"):
                break
            pos.tp_hits += 1

        # 5) trailing stop on the runner (after first TP)
        if pos.tp_hits > 0:
            trail = pos.peak_price * (1 - c.trailing_stop_pct / 100)
            if s.price <= trail:
                return self._close(pos, s, now,
                                   f"trailing stop {c.trailing_stop_pct:.0f}% from peak")

        # 6) time stop: no TP within 25 min
        age_min = (now - pos.opened_at).total_seconds() / 60
        if pos.tp_hits == 0 and age_min >= c.time_stop_min:
            return self._close(pos, s, now, f"time stop {c.time_stop_min:.0f} min")

        if pos.tokens <= 1e-12:
            return self._close(pos, s, now, "fully sold")
        return None

    def position_value(self, pos: Position, s: PairSnapshot | None) -> float:
        if s is None:
            return pos.tokens * pos.entry_price
        return pos.tokens * s.price

    def _update_status(self) -> None:
        self.status = (", ".join(p.symbol for p in self.positions.values())
                       if self.positions else "flat")
