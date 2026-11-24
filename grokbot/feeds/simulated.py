"""Synthetic Solana meme-coin market for offline testing / backtesting.

Every few minutes a new token launches with a hidden archetype:
  runner   - real momentum, can do 2x-20x
  pumpdump - pumps hard, then bleeds out
  rug      - looks fine, then dev dumps or LP is pulled
  dud      - chops sideways and bleeds
Safety properties (mint authority, top10 %, honeypot ...) are drawn so that
bad tokens fail checks more often - but not always, just like real life.
"""
from __future__ import annotations

import math
import random
import string
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models import PairSnapshot

ARCHETYPES = [("runner", 0.07), ("pumpdump", 0.38), ("rug", 0.30), ("dud", 0.25)]


def _pick(rng: random.Random, table):
    r, acc = rng.random(), 0.0
    for name, p in table:
        acc += p
        if r <= acc:
            return name
    return table[-1][0]


@dataclass
class _Token:
    mint: str
    symbol: str
    dex: str
    kind: str
    created_at: datetime
    price: float
    liquidity: float
    holders: float
    props: dict
    phase_change_min: float      # when the archetype "turns"
    drift_up: float              # per-minute log drift before the turn
    drift_down: float            # per-minute log drift after the turn
    vol_hist: deque = field(default_factory=lambda: deque(maxlen=120))   # (ts, usd)
    px_hist: deque = field(default_factory=lambda: deque(maxlen=240))    # (ts, price)
    holder_hist: deque = field(default_factory=lambda: deque(maxlen=60))
    buys: deque = field(default_factory=lambda: deque(maxlen=20))
    sells: deque = field(default_factory=lambda: deque(maxlen=20))
    dead: bool = False
    dev_sold: bool = False
    lp_pulled: bool = False
    smart_buys: int = 0
    kol: bool = False


class SimulatedFeed:
    def __init__(self, start: datetime, seed: int | None = None,
                 launches_per_hour: float = 10.0, tick_seconds: int = 30):
        self.rng = random.Random(seed)
        self.now = start
        self.tick = tick_seconds
        self.launch_rate = launches_per_hour
        self.tokens: dict[str, _Token] = {}
        # pre-seed some pairs launched in the last hours
        for _ in range(int(launches_per_hour * 2)):
            age = self.rng.uniform(5, 180)
            self._launch(start - timedelta(minutes=age), warm_minutes=age)

    # ------------------------------------------------------------------ launch
    def _launch(self, at: datetime, warm_minutes: float = 0.0) -> None:
        rng = self.rng
        kind = _pick(rng, ARCHETYPES)
        sym = "".join(rng.choices(string.ascii_uppercase, k=rng.randint(3, 6)))
        mint = "".join(rng.choices(string.ascii_letters + string.digits, k=44))
        # many scammers know the checklist: ~half of the bad tokens look clean
        bad = kind in ("rug", "pumpdump") and rng.random() < 0.5
        props = {
            "mint_open": rng.random() < (0.25 if bad else 0.05),
            "freeze_open": rng.random() < (0.15 if bad else 0.03),
            "lp_burned": rng.random() < (0.4 if bad else 0.75),
            "lp_locked": rng.random() < 0.85,
            "lp_lock_days": rng.choice([7, 14, 30, 90, 365]),
            "dev_pct": rng.uniform(0.5, 12 if bad else 6),
            "top10_pct": rng.uniform(15, 60 if bad else 40),
            "bundled": rng.random() < (0.35 if bad else 0.08),
            "honeypot": rng.random() < (0.10 if kind == "rug" else 0.01),
        }
        turn = {"runner": rng.uniform(40, 200), "pumpdump": rng.uniform(8, 40),
                "rug": rng.uniform(10, 90), "dud": rng.uniform(5, 30)}[kind]
        up = {"runner": rng.uniform(0.01, 0.035), "pumpdump": rng.uniform(0.02, 0.06),
              "rug": rng.uniform(0.005, 0.03), "dud": rng.uniform(-0.005, 0.01)}[kind]
        down = {"runner": rng.uniform(-0.02, 0.0), "pumpdump": rng.uniform(-0.06, -0.02),
                "rug": -0.5, "dud": rng.uniform(-0.02, -0.005)}[kind]
        t = _Token(mint=mint, symbol=sym, dex=rng.choice(["pump.fun", "Raydium", "Meteora"]),
                   kind=kind, created_at=at, price=rng.uniform(1e-6, 5e-4),
                   liquidity=rng.uniform(25_000, 120_000), holders=rng.uniform(150, 900),
                   props=props, phase_change_min=turn, drift_up=up, drift_down=down)
        self.tokens[mint] = t
        # warm up history so 1h stats exist
        steps = int(warm_minutes * 60 / self.tick)
        for i in range(steps):
            self._step_token(t, at + timedelta(seconds=self.tick * (i + 1)))

    # -------------------------------------------------------------------- step
    def _step_token(self, t: _Token, ts: datetime) -> None:
        if t.dead:
            return
        rng = self.rng
        age_min = (ts - t.created_at).total_seconds() / 60
        dt_min = self.tick / 60
        turned = age_min >= t.phase_change_min
        drift = t.drift_down if turned else t.drift_up
        sigma = 0.09
        if t.kind == "rug" and turned and not (t.dev_sold or t.lp_pulled):
            if rng.random() < 0.5:
                t.lp_pulled = True
                t.liquidity *= 0.02
            else:
                t.dev_sold = True
            t.price *= rng.uniform(0.05, 0.3)
        shock = rng.gauss(drift * dt_min, sigma * math.sqrt(dt_min))
        t.price *= math.exp(shock)
        momentum = shock / (sigma * math.sqrt(dt_min))   # ~N(0,1) + drift
        t.liquidity = max(500.0, t.liquidity * math.exp(0.5 * shock))
        # volume and order flow
        base = t.liquidity * rng.uniform(0.01, 0.03) * dt_min
        hype = math.exp(max(0, momentum) * 0.6) * (3 if not turned and t.kind != "dud" else 1)
        vol = base * hype
        buy_share = 1 / (1 + math.exp(-1.4 * momentum - (0.8 if not turned else -0.8)))
        n = max(1, int(vol / rng.uniform(80, 250)))
        t.buys.append((ts, int(n * buy_share)))
        t.sells.append((ts, n - int(n * buy_share)))
        t.vol_hist.append((ts, vol))
        t.px_hist.append((ts, t.price))
        t.holders = max(10.0, t.holders * (1 + (0.012 if not turned else -0.004) * dt_min
                                           * (1 + max(0, momentum))))
        t.holder_hist.append((ts, t.holders))
        # smart money & KOLs like real runners (and sometimes get fooled)
        sm_p = {"runner": 0.05, "pumpdump": 0.04, "rug": 0.02, "dud": 0.005}[t.kind]
        t.smart_buys = rng.randint(3, 6) if (not turned and rng.random() < sm_p) else max(0, t.smart_buys - 1)
        t.kol = (not turned) and rng.random() < sm_p / 2
        if t.price * 1e6 < 0.001 or age_min > 12 * 60:
            t.dead = True

    def advance(self, now: datetime) -> None:
        while self.now < now:
            self.now += timedelta(seconds=self.tick)
            if self.rng.random() < self.launch_rate * self.tick / 3600:
                self._launch(self.now)
            for t in list(self.tokens.values()):
                self._step_token(t, self.now)

    # --------------------------------------------------------------- snapshots
    def _snap(self, t: _Token) -> PairSnapshot:
        now = self.now
        def window(dq, minutes):
            return [v for ts, v in dq if (now - ts).total_seconds() <= minutes * 60]
        px_1h = window(t.px_hist, 60) or [t.price]
        h15 = window(t.holder_hist, 15) or [t.holders]
        p = t.props
        return PairSnapshot(
            mint=t.mint, symbol=t.symbol, dex=t.dex, created_at=t.created_at,
            price=t.price, liquidity_usd=t.liquidity,
            volume_5m=sum(window(t.vol_hist, 5)), volume_1h=sum(window(t.vol_hist, 60)),
            buys_5m=sum(window(t.buys, 5)), sells_5m=sum(window(t.sells, 5)),
            holders=int(t.holders), holders_15m_ago=int(h15[0]),
            price_change_1h_pct=100 * (t.price / px_1h[0] - 1), high_1h=max(px_1h),
            mint_authority_open=p["mint_open"], freeze_authority_open=p["freeze_open"],
            lp_locked=p["lp_locked"], lp_burned=p["lp_burned"],
            lp_lock_days=p["lp_lock_days"], dev_pct=p["dev_pct"], top10_pct=p["top10_pct"],
            bundled_launch=p["bundled"], honeypot=p["honeypot"],
            smart_wallet_buys_10m=t.smart_buys, kol_bought=t.kol,
            dev_sold=t.dev_sold, lp_pulled=t.lp_pulled,
        )

    def snapshots(self, now: datetime) -> list[PairSnapshot]:
        self.advance(now)
        return [self._snap(t) for t in self.tokens.values() if not t.dead]

    def get(self, mint: str, now: datetime) -> PairSnapshot | None:
        self.advance(now)
        t = self.tokens.get(mint)
        return self._snap(t) if t else None
