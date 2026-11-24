"""Shared data structures passed between agents."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PairSnapshot:
    """Everything the agents know about one DEX pair at one point in time."""
    mint: str
    symbol: str
    dex: str
    created_at: datetime
    price: float
    liquidity_usd: float
    volume_5m: float
    volume_1h: float
    buys_5m: int
    sells_5m: int
    holders: int
    holders_15m_ago: int
    price_change_1h_pct: float
    high_1h: float
    # token safety
    mint_authority_open: bool
    freeze_authority_open: bool
    lp_locked: bool
    lp_burned: bool
    lp_lock_days: int
    dev_pct: float
    top10_pct: float
    bundled_launch: bool
    honeypot: bool
    # live events
    smart_wallet_buys_10m: int = 0
    kol_bought: bool = False
    dev_sold: bool = False
    lp_pulled: bool = False

    @property
    def buy_ratio(self) -> float:
        total = self.buys_5m + self.sells_5m
        return self.buys_5m / total if total else 0.0

    def age_hours(self, now: datetime) -> float:
        return (now - self.created_at).total_seconds() / 3600


@dataclass
class Signal:
    snapshot: PairSnapshot
    reasons: list[str]

    @property
    def count(self) -> int:
        return len(self.reasons)


@dataclass
class RugReport:
    passed: bool
    failures: list[str] = field(default_factory=list)


@dataclass
class Position:
    mint: str
    symbol: str
    opened_at: datetime
    entry_price: float           # average fill price
    target_usd: float            # planned full ticket
    invested_usd: float = 0.0    # what actually went in
    tokens: float = 0.0          # tokens still held
    initial_tokens: float = 0.0  # tokens after all entries
    realized_usd: float = 0.0
    peak_price: float = 0.0
    tp_hits: int = 0
    entries_done: int = 0
    first_entry_price: float = 0.0
    signals: list[str] = field(default_factory=list)
    sell_failures: int = 0

    @property
    def pnl_usd(self) -> float:
        return self.realized_usd - self.invested_usd


@dataclass
class ClosedTrade:
    symbol: str
    mint: str
    opened_at: datetime
    closed_at: datetime
    invested_usd: float
    returned_usd: float
    reason: str

    @property
    def pnl_usd(self) -> float:
        return self.returned_usd - self.invested_usd

    @property
    def pnl_pct(self) -> float:
        return 100 * self.pnl_usd / self.invested_usd if self.invested_usd else 0.0


class BrokerError(Exception):
    """A swap could not be executed; the bot keeps its previous state."""
