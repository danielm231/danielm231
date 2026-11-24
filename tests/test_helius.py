"""Helius wallet tracker against recorded-shape API responses (no network)."""
from datetime import datetime, timedelta

from grokbot.feeds import helius
from grokbot.feeds.helius import HeliusWalletTracker, buys_from_tx, load_wallets

SOL = "So11111111111111111111111111111111111111112"
NOW = datetime(2026, 11, 25, 1, 0)


def swap(sig, wallet, mint, minutes_ago, *, sell=False):
    ts = int((NOW - timedelta(minutes=minutes_ago)).timestamp())
    got, gave = (SOL, mint) if sell else (mint, SOL)
    return {"signature": sig, "timestamp": ts, "type": "SWAP", "tokenTransfers": [
        {"fromUserAccount": wallet, "toUserAccount": "POOL", "mint": gave, "tokenAmount": 1},
        {"fromUserAccount": "POOL", "toUserAccount": wallet, "mint": got, "tokenAmount": 1000},
    ]}


def test_buys_from_tx():
    assert buys_from_tx(swap("s", "W", "MEME", 1), "W") == ["MEME"]
    assert buys_from_tx(swap("s", "W", "MEME", 1, sell=True), "W") == []   # got SOL back
    assert buys_from_tx({**swap("s", "W", "MEME", 1), "transactionError": {"x": 1}}, "W") == []


def test_load_wallets(tmp_path):
    f = tmp_path / "w.txt"
    a, b = "A" * 44, "B" * 43
    f.write_text(f"# comment\n{a}  # whale\n\n{b}\n{a}\nshort\n")
    assert load_wallets(f) == [a, b]
    assert load_wallets(tmp_path / "missing.txt") == []


def test_tracker_counts_windows(monkeypatch):
    feeds = {
        "S1": [swap("a", "S1", "MEME", 2)],
        "S2": [swap("b", "S2", "MEME", 8)],
        "S3": [swap("c", "S3", "MEME", 9), swap("d", "S3", "OTHER", 1)],
        "S4": [swap("e", "S4", "MEME", 25)],                 # outside 10-min window
        "K1": [swap("f", "K1", "MEME", 20)],                 # KOL within 30 min
    }
    calls = []

    def fake_get(url, timeout=10.0):
        calls.append(url)
        wallet = url.split("/addresses/")[1].split("/")[0]
        assert "api-key=KEY" in url and "type=SWAP" in url
        return feeds[wallet]

    monkeypatch.setattr(helius, "_get", fake_get)
    t = HeliusWalletTracker(["S1", "S2", "S3", "S4"], ["K1"], api_key="KEY")
    assert t.enabled
    assert t("MEME", NOW) == (3, True)
    assert t("OTHER", NOW) == (1, False)
    assert t("NOPE", NOW) == (0, False)
    assert len(calls) == 5            # second and third call reuse the poll (60s throttle)
    t.refresh(force=True)
    assert t("MEME", NOW) == (3, True)  # same signatures are not double counted


def test_tracker_disabled_without_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    t = HeliusWalletTracker(["S1"], [])
    assert not t.enabled and t("MEME", NOW) == (0, False) and "off" in t.status
