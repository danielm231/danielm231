"""Agent 1 - SCANNER: watches pairs and flags signals. It never trades."""
from __future__ import annotations

from datetime import datetime

from ..config import ScannerConfig, UniverseConfig
from ..models import PairSnapshot, Signal


class Scanner:
    name = "SCANNER"

    def __init__(self, universe: UniverseConfig, cfg: ScannerConfig):
        self.universe = universe
        self.cfg = cfg
        self.status = "idle"

    def universe_filter(self, s: PairSnapshot, now: datetime) -> str | None:
        """Return a reason string if the pair is outside the universe, else None."""
        u = self.universe
        if s.age_hours(now) > u.max_age_hours:
            return f"too old ({s.age_hours(now):.1f}h)"
        if s.liquidity_usd < u.min_liquidity_usd:
            return f"liquidity ${s.liquidity_usd:,.0f} < ${u.min_liquidity_usd:,.0f}"
        if s.volume_5m < u.min_volume_5m_usd:
            return f"5m volume ${s.volume_5m:,.0f} < ${u.min_volume_5m_usd:,.0f}"
        if s.holders < u.min_holders:
            return f"holders {s.holders} < {u.min_holders}"
        if s.freeze_authority_open:
            return "freeze authority open"
        if s.mint_authority_open:
            return "mint authority open"
        if not (s.lp_locked or s.lp_burned):
            return "LP unlocked"
        return None

    def signals(self, s: PairSnapshot) -> list[str]:
        c = self.cfg
        out: list[str] = []
        avg_5m = s.volume_1h / 12 if s.volume_1h else 0
        if avg_5m and s.volume_5m >= c.volume_spike_x * avg_5m:
            out.append(f"vol spike {s.volume_5m / avg_5m:.1f}x")
        if s.buy_ratio > c.min_buy_ratio:
            out.append(f"buy ratio {s.buy_ratio:.0%}")
        if s.holders_15m_ago:
            growth = 100 * (s.holders - s.holders_15m_ago) / s.holders_15m_ago
            if growth >= c.holder_growth_pct_15m:
                out.append(f"holders +{growth:.0f}%/15m")
        if s.smart_wallet_buys_10m >= c.smart_money_min_wallets:
            out.append(f"smart money {s.smart_wallet_buys_10m} wallets")
        if s.kol_bought:
            out.append("KOL bought")
        return out

    def scan(self, snapshots: list[PairSnapshot], now: datetime
             ) -> tuple[list[Signal], list[tuple[PairSnapshot, str]]]:
        """Return (flagged signals, filtered pairs with reason)."""
        flagged: list[Signal] = []
        filtered: list[tuple[PairSnapshot, str]] = []
        for s in snapshots:
            reason = self.universe_filter(s, now)
            if reason:
                filtered.append((s, reason))
                continue
            sig = self.signals(s)
            if sig:
                flagged.append(Signal(s, sig))
        flagged.sort(key=lambda x: x.count, reverse=True)
        self.status = f"{len(snapshots)} pairs, {len(flagged)} flagged"
        return flagged, filtered
