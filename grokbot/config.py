"""All tunable parameters of the overnight bot, mirroring the original prompt."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UniverseConfig:
    max_age_hours: float = 6.0
    min_liquidity_usd: float = 40_000
    min_volume_5m_usd: float = 25_000
    min_holders: int = 300


@dataclass
class ScannerConfig:
    volume_spike_x: float = 5.0          # 5m volume vs. average 5m volume of the last hour
    min_buy_ratio: float = 0.70
    holder_growth_pct_15m: float = 10.0  # holder count +10% within 15 minutes
    smart_money_min_wallets: int = 3     # of the tracked wallets ...
    smart_money_window_min: int = 10     # ... buying within this window
    tracked_wallets: list[str] = field(default_factory=list)  # 40 smart-money wallets
    kol_wallets: list[str] = field(default_factory=list)


@dataclass
class RugCheckConfig:
    max_dev_pct: float = 5.0
    max_top10_pct: float = 35.0
    min_lp_lock_days: int = 30


@dataclass
class SizerConfig:
    min_pct: float = 0.20
    max_pct: float = 0.40
    max_open_positions: int = 2
    loss_streak_for_scale_down: int = 2
    scale_down_factor: float = 0.50
    min_ticket_usd: float = 2.0


@dataclass
class ExecutorConfig:
    max_slippage_pct: float = 2.5
    entry_parts: int = 2
    second_entry_max_drop_pct: float = 3.0   # 2nd half only if price held up
    # take-profit ladder: (multiple of entry price, fraction of ORIGINAL size)
    tp_ladder: list[tuple[float, float]] = field(
        default_factory=lambda: [(2.0, 0.40), (4.0, 0.30)]
    )
    trailing_stop_pct: float = 25.0   # on the remaining 30% runner
    hard_stop_pct: float = 12.0
    time_stop_min: float = 25.0        # exit if no TP hit within 25 min
    fee_pct: float = 0.3               # DEX/priority fee approximation per swap
    extra_slippage_pct: float = 1.0    # paper fills: MEV / latency penalty per swap


@dataclass
class RiskConfig:
    min_signals: int = 2
    no_chase_pump_pct: float = 300.0  # skip if +300% in last hour ...
    pullback_pct: float = 20.0         # ... unless price is >=20% under 1h high
    max_drawdown_pct: float = 30.0


@dataclass
class BotConfig:
    start_balance: float = 70.0
    session_start: str = "23:00"
    session_end: str = "07:00"
    tick_seconds: int = 30
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    rug: RugCheckConfig = field(default_factory=RugCheckConfig)
    sizer: SizerConfig = field(default_factory=SizerConfig)
    executor: ExecutorConfig = field(default_factory=ExecutorConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
