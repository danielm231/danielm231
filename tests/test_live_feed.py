"""Live feed parsing against recorded-shape API responses (no network)."""
import time
from datetime import datetime

from grokbot.feeds import live

PAIR = {"chainId": "solana", "dexId": "raydium", "baseToken": {"address": "MINT", "symbol": "WIF2"},
        "priceUsd": "0.0012", "liquidity": {"usd": 55000}, "volume": {"m5": 30000, "h1": 90000},
        "txns": {"m5": {"buys": 90, "sells": 20}}, "priceChange": {"h1": 120},
        "pairCreatedAt": int(time.time() * 1000) - 3600_000}
REPORT = {"mintAuthority": None, "freezeAuthority": None, "creator": "DEV", "totalHolders": 640,
          "topHolders": [{"owner": "DEV", "pct": 3.0}] + [{"owner": f"W{i}", "pct": 2.0} for i in range(12)],
          "markets": [{"lp": {"lpLockedPct": 100}}], "risks": [], "rugged": False}


def fake_get(url, timeout=10.0):
    if "/token-profiles/" in url or "/token-boosts/" in url:
        return [{"chainId": "solana", "tokenAddress": "MINT"}, {"chainId": "base", "tokenAddress": "X"}]
    if "/tokens/v1/solana/" in url:
        return [PAIR]
    if "rugcheck" in url:
        return REPORT
    if "jup.ag" in url:
        return {"outAmount": "123"}
    raise AssertionError(url)


def test_live_snapshot(monkeypatch):
    monkeypatch.setattr(live, "_get", fake_get)
    feed = live.LiveFeed()
    [s] = feed.snapshots(datetime.now())
    assert s.symbol == "WIF2" and s.liquidity_usd == 55000 and s.buys_5m == 90
    assert s.holders == 640 and s.dev_pct == 3.0 and s.top10_pct == 3 + 9 * 2
    assert s.lp_burned and not s.mint_authority_open and not s.honeypot
    assert 0.9 < s.age_hours(datetime.now()) < 1.1
