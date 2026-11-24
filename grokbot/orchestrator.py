"""Runs the night: wires the six agents together and enforces the RULES."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from .agents import EquityTracker, Executor, RugChecker, Scanner, Sizer, TradeLogger
from .config import BotConfig
from .models import PairSnapshot, Signal


class Feed(Protocol):
    def snapshots(self, now: datetime) -> list[PairSnapshot]: ...
    def get(self, mint: str, now: datetime) -> PairSnapshot | None: ...


def session_window(cfg: BotConfig, day: datetime) -> tuple[datetime, datetime]:
    """Session start on `day` and end on the following morning if it wraps midnight."""
    sh, sm = map(int, cfg.session_start.split(":"))
    eh, em = map(int, cfg.session_end.split(":"))
    start = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = day.replace(hour=eh, minute=em, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


class Orchestrator:
    def __init__(self, cfg: BotConfig, feed: Feed, log_path: Path | None = None,
                 echo: bool = True, broker=None, kill=None):
        """broker: PaperBroker (default) or JupiterBroker.
        kill: (start_wallet_usd, max_loss_usd) - real money kill switch."""
        self.cfg = cfg
        self.feed = feed
        self.log = TradeLogger(log_path, echo=echo)
        self.equity = EquityTracker(cfg.start_balance)
        self.scanner = Scanner(cfg.universe, cfg.scanner)
        self.rug = RugChecker(cfg.rug)
        self.sizer = Sizer(cfg.sizer)
        self.executor = Executor(cfg.executor, self.equity, self.log, broker)
        self.kill = kill
        self.stopped = False
        self._ticks = 0
        self.paused = False
        self.pause_reason = ""
        self._seen_filtered: set[tuple[str, str]] = set()
        self._rug_failed: set[str] = set()
        self.stats = defaultdict(int)
        self.last_snaps: dict[str, PairSnapshot] = {}
        self.now: datetime | None = None

    # --------------------------------------------------------------- helpers
    @property
    def agents(self):
        agents = [self.scanner, self.rug, self.sizer, self.executor, self.log, self.equity]
        tracker = getattr(self.feed, "tracker", None)
        if tracker is not None:
            agents.insert(1, tracker)
        return agents

    def total_equity(self) -> float:
        return self.equity.cash + sum(
            self.executor.position_value(p, self.last_snaps.get(m))
            for m, p in self.executor.positions.items())

    def _filtered(self, now: datetime, s: PairSnapshot, reason: str) -> None:
        self.stats["filtered"] += 1
        key = (s.mint, reason.split(" ")[0])
        if key not in self._seen_filtered:      # one line per coin + reason type
            self._seen_filtered.add(key)
            self.log.log(now, "FILTERED", s.symbol, reason)

    # ----------------------------------------------------------------- tick
    def tick(self, now: datetime) -> None:
        self.now = now
        self._ticks += 1
        if self.stopped:
            return
        # 1) manage open positions
        for mint, pos in list(self.executor.positions.items()):
            snap = self.feed.get(mint, now)
            if snap is None:
                continue
            self.last_snaps[mint] = snap
            closed = self.executor.manage(pos, snap, now)
            if closed:
                self.sizer.record_result(closed.pnl_usd)
                self.equity.on_close(closed, self.total_equity())
                self.stats["trades"] += 1

        equity = self.total_equity()
        self.equity.mark(now, equity)

        # 1b) real-money kill switch, checked against the actual wallet every 5 min
        if self.kill and self._ticks % 10 == 1:
            self._check_kill(now)
            if self.stopped:
                return

        # 2) daily drawdown limit
        dd = self.equity.drawdown_pct(equity)
        if not self.paused and dd >= self.cfg.risk.max_drawdown_pct:
            self.paused = True
            self.pause_reason = (f"drawdown {dd:.1f}% from peak ${self.equity.peak:.2f} "
                                 f">= {self.cfg.risk.max_drawdown_pct:.0f}% limit")
            self.log.log(now, "PAUSE", "-", self.pause_reason)
        if self.paused:
            return

        # 3) scan for new entries
        if len(self.executor.positions) >= self.cfg.sizer.max_open_positions:
            return
        snaps = self.feed.snapshots(now)
        flagged, outside = self.scanner.scan(snaps, now)
        self.stats["outside_universe"] += len(outside)
        for sig in flagged:
            if len(self.executor.positions) >= self.cfg.sizer.max_open_positions:
                break
            self._consider(sig, now)

    def _consider(self, sig: Signal, now: datetime) -> None:
        s = sig.snapshot
        r = self.cfg.risk
        if s.mint in self.executor.positions or s.mint in self._rug_failed:
            return
        if sig.count < r.min_signals:
            return self._filtered(now, s, f"only {sig.count} signal(s): {', '.join(sig.reasons)}")
        if s.price_change_1h_pct > r.no_chase_pump_pct:
            pullback = 100 * (1 - s.price / s.high_1h) if s.high_1h else 0
            if pullback < r.pullback_pct:
                return self._filtered(now, s, f"no chase: +{s.price_change_1h_pct:.0f}% 1h, "
                                              f"pullback only {pullback:.0f}%")
        rep = self.rug.check(s)
        if not rep.passed:
            self._rug_failed.add(s.mint)
            return self._filtered(now, s, "rug check FAIL: " + "; ".join(rep.failures))
        usd, note = self.sizer.size(self.equity.cash, self.total_equity(), sig.count,
                                    len(self.executor.positions))
        if usd <= 0:
            return self._filtered(now, s, f"sizer: {note}")
        self.last_snaps[s.mint] = s
        self.executor.open(sig, usd, now)

    # ------------------------------------------------------------------ end
    def _check_kill(self, now: datetime) -> None:
        start_usd, max_loss = self.kill
        prices = {m: s.price for m, s in self.last_snaps.items()}
        try:
            wallet = self.executor.broker.wallet_usd(prices)
        except Exception as e:  # network hiccup: check again next time
            self.log.log(now, "ERROR", "-", f"Wallet-Abfrage für Kill-Switch fehlgeschlagen: {e}")
            return
        loss = start_usd - wallet
        self.executor.broker.status = (self.executor.broker.status.split(" | ")[0]
                                       + f" | Wallet ${wallet:.2f} ({-loss:+.2f})")
        if loss >= max_loss:
            self.log.log(now, "KILL", "-", f"Wallet ${wallet:.2f}: Verlust ${loss:.2f} >= "
                         f"Limit ${max_loss:.2f} - verkaufe alles und stoppe")
            self.close_all(now, "kill switch")
            self.stopped = True
            self.paused = True
            self.pause_reason = f"KILL-SWITCH: Verlust ${loss:.2f}"

    def close_all(self, now: datetime, reason: str = "session end 07:00", passes: int = 3) -> None:
        for _ in range(passes):
            for mint, pos in list(self.executor.positions.items()):
                snap = self.feed.get(mint, now) or self.last_snaps.get(mint)
                if snap is None:
                    continue
                closed = self.executor._close(pos, snap, now, reason)
                if closed:
                    self.sizer.record_result(closed.pnl_usd)
                    self.equity.on_close(closed, self.total_equity())
            if not self.executor.positions:
                return
        for pos in self.executor.positions.values():
            self.log.log(now, "ERROR", pos.symbol, "konnte nicht verkauft werden - bitte manuell prüfen")

    def recap(self) -> dict:
        trades = self.equity.trades
        by_coin: dict[str, float] = defaultdict(float)
        for t in trades:
            by_coin[t.symbol] += t.pnl_usd
        end = self.total_equity()
        best = max(trades, key=lambda t: t.pnl_usd, default=None)
        worst = min(trades, key=lambda t: t.pnl_usd, default=None)
        return {
            "start_balance": self.cfg.start_balance,
            "end_balance": round(end, 2),
            "return_pct": round(100 * (end / self.cfg.start_balance - 1), 2),
            "total_trades": len(trades),
            "wins": self.equity.wins,
            "losses": self.equity.losses,
            "win_rate_pct": round(100 * self.equity.wins / len(trades), 1) if trades else 0.0,
            "best_trade": (best.symbol, round(best.pnl_usd, 2), round(best.pnl_pct, 1)) if best else None,
            "worst_trade": (worst.symbol, round(worst.pnl_usd, 2), round(worst.pnl_pct, 1)) if worst else None,
            "top3_coins": sorted(((k, round(v, 2)) for k, v in by_coin.items() if v > 0),
                                 key=lambda x: -x[1])[:3],
            "max_drawdown_pct": round(max((100 * (pk - eq) / pk for pk, eq in
                                           _running_peak(self.equity.curve)), default=0), 2),
            "paused": self.pause_reason or None,
        }


def _running_peak(curve):
    peak = 0.0
    for _, eq in curve:
        peak = max(peak, eq)
        yield peak, eq
