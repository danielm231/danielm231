from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from grokbot.agents import EquityTracker, Executor, RugChecker, Scanner, Sizer, TradeLogger
from grokbot.config import BotConfig
from grokbot.models import PairSnapshot, Signal
from grokbot.orchestrator import Orchestrator

NOW = datetime(2026, 11, 24, 23, 30)
CFG = BotConfig()


def snap(**kw) -> PairSnapshot:
    base = dict(
        mint="M1", symbol="TEST", dex="Raydium", created_at=NOW - timedelta(hours=1),
        price=1.0, liquidity_usd=100_000, volume_5m=30_000, volume_1h=60_000,
        buys_5m=80, sells_5m=20, holders=500, holders_15m_ago=400,
        price_change_1h_pct=50, high_1h=1.1, mint_authority_open=False,
        freeze_authority_open=False, lp_locked=True, lp_burned=True, lp_lock_days=365,
        dev_pct=2, top10_pct=20, bundled_launch=False, honeypot=False,
    )
    base.update(kw)
    return PairSnapshot(**base)


# ----------------------------------------------------------------- scanner
def test_universe_filters():
    sc = Scanner(CFG.universe, CFG.scanner)
    assert sc.universe_filter(snap(), NOW) is None
    assert "old" in sc.universe_filter(snap(created_at=NOW - timedelta(hours=7)), NOW)
    assert "liquidity" in sc.universe_filter(snap(liquidity_usd=39_000), NOW)
    assert "volume" in sc.universe_filter(snap(volume_5m=20_000), NOW)
    assert "holders" in sc.universe_filter(snap(holders=299), NOW)
    assert "freeze" in sc.universe_filter(snap(freeze_authority_open=True), NOW)
    assert "mint" in sc.universe_filter(snap(mint_authority_open=True), NOW)
    assert "LP" in sc.universe_filter(snap(lp_locked=False, lp_burned=False), NOW)


def test_scanner_signals():
    sc = Scanner(CFG.universe, CFG.scanner)
    sig = sc.signals(snap(smart_wallet_buys_10m=3, kol_bought=True))
    joined = " ".join(sig)
    # 30k vs 5k avg -> 6x spike, 80% buys, +25% holders, smart money, KOL
    for word in ("vol spike", "buy ratio", "holders", "smart money", "KOL"):
        assert word in joined
    assert sc.signals(snap(volume_5m=20_000, volume_1h=240_000, buys_5m=50, sells_5m=50,
                           holders_15m_ago=500)) == []


# --------------------------------------------------------------- rug check
def test_rugcheck():
    rc = RugChecker(CFG.rug)
    assert rc.check(snap()).passed
    for bad in (dict(dev_pct=5), dict(top10_pct=35), dict(bundled_launch=True),
                dict(honeypot=True), dict(lp_burned=False, lp_lock_days=7)):
        assert not rc.check(snap(**bad)).passed, bad
    assert rc.check(snap(lp_burned=False, lp_lock_days=30)).passed


# ------------------------------------------------------------------- sizer
def test_sizer():
    sz = Sizer(CFG.sizer)
    assert sz.size(100, 100, 2, 0)[0] == pytest.approx(20)
    assert sz.size(100, 100, 3, 0)[0] == pytest.approx(30)
    assert sz.size(100, 100, 5, 0)[0] == pytest.approx(40)
    assert sz.size(100, 100, 3, 2)[0] == 0           # max 2 open
    sz.record_result(-1); sz.record_result(-1)
    assert sz.size(100, 100, 2, 0)[0] == pytest.approx(10)          # halved after 2 losses
    sz.record_result(+1)
    assert sz.size(100, 100, 2, 0)[0] == pytest.approx(20)


# ---------------------------------------------------------------- executor
def make_exec():
    eq = EquityTracker(70)
    ex_cfg = replace(CFG.executor, fee_pct=0, extra_slippage_pct=0)
    return Executor(ex_cfg, eq, TradeLogger(echo=False)), eq


def big(**kw):  # huge liquidity -> negligible price impact
    return snap(liquidity_usd=1e12, **kw)


def test_hard_stop():
    ex, eq = make_exec()
    pos = ex.open(Signal(big(), ["a", "b"]), 20, NOW)
    ex.manage(pos, big(price=1.0), NOW + timedelta(seconds=30))   # 2nd entry
    closed = ex.manage(pos, big(price=0.87), NOW + timedelta(minutes=1))
    assert closed and "hard stop" in closed.reason
    assert abs(eq.cash - (70 - 20 * 0.13)) < 0.01


def test_tp_ladder_and_trailing():
    ex, eq = make_exec()
    pos = ex.open(Signal(big(), ["a", "b"]), 20, NOW)
    ex.manage(pos, big(price=1.0), NOW + timedelta(seconds=30))
    assert ex.manage(pos, big(price=2.001), NOW + timedelta(minutes=2)) is None
    assert pos.tp_hits == 1 and pos.tokens == pytest.approx(12)        # sold 40%
    ex.manage(pos, big(price=4.001), NOW + timedelta(minutes=3))
    assert pos.tp_hits == 2 and pos.tokens == pytest.approx(6)         # sold 30% more
    ex.manage(pos, big(price=6.0), NOW + timedelta(minutes=4))     # new peak
    closed = ex.manage(pos, big(price=4.4), NOW + timedelta(minutes=5))  # -26.7% from peak
    assert closed and "trailing" in closed.reason
    assert closed.returned_usd == pytest.approx(8 * 2.001 + 6 * 4.001 + 6 * 4.4, rel=1e-6)


def test_time_stop_and_emergency():
    ex, _ = make_exec()
    pos = ex.open(Signal(big(), ["a", "b"]), 20, NOW)
    closed = ex.manage(pos, big(price=1.05), NOW + timedelta(minutes=25))
    assert closed and "time stop" in closed.reason

    ex, _ = make_exec()
    pos = ex.open(Signal(big(mint="M2"), ["a", "b"]), 20, NOW)
    closed = ex.manage(pos, big(mint="M2", dev_sold=True), NOW + timedelta(seconds=30))
    assert closed and "dev sold" in closed.reason


def test_slippage_limit():
    ex, _ = make_exec()
    # $20 into a pool with $1000 per side -> ~1% impact ok; $100 into $2k liq -> too much
    assert ex.open(Signal(snap(liquidity_usd=2000), ["a", "b"]), 100, NOW) is None


# ------------------------------------------------------------ orchestrator
class OneCoinFeed:
    def __init__(self, s):
        self.s = s

    def snapshots(self, now):
        return [self.s]

    def get(self, mint, now):
        return self.s


def test_rules_no_chase_and_min_signals():
    o = Orchestrator(CFG, OneCoinFeed(snap(price_change_1h_pct=400, high_1h=1.05)), echo=False)
    o.tick(NOW)
    assert not o.executor.positions
    assert any("no chase" in l["reason"] for l in o.log.lines)

    o = Orchestrator(CFG, OneCoinFeed(snap(price_change_1h_pct=400, high_1h=1.5)), echo=False)
    o.tick(NOW)                                   # 33% pullback -> allowed
    assert o.executor.positions

    one_signal = snap(buys_5m=50, sells_5m=50, holders_15m_ago=500)  # only the vol spike
    o = Orchestrator(CFG, OneCoinFeed(one_signal), echo=False)
    o.tick(NOW)
    assert not o.executor.positions


def test_drawdown_pause():
    o = Orchestrator(CFG, OneCoinFeed(snap()), echo=False)
    o.equity.peak = 100
    o.equity.cash = 69
    o.tick(NOW)
    assert o.paused and not o.executor.positions
    assert o.log.lines[-1]["kind"] == "PAUSE"


def test_full_sim_night_runs():
    from grokbot.__main__ import main
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        r = main(["--mode", "sim", "--seed", "7", "--quiet", "--out", d])
    assert r["start_balance"] == 70 and r["total_trades"] >= 0
