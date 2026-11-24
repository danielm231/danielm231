"""Helius RPC wallet tracker against recorded-shape JSON-RPC responses (no network)."""
import time
from datetime import datetime, timedelta

from grokbot.feeds import helius
from grokbot.feeds.helius import HeliusWalletTracker, buys_from_tx, load_wallets

SOL = "So11111111111111111111111111111111111111112"


def tx(wallet, mint, minutes_ago, *, sell=False, err=None):
    bt = int(time.time() - minutes_ago * 60)
    before, after = (500, 0) if sell else (0, 500)
    return {"blockTime": bt, "meta": {"err": err,
            "preTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(before)}}],
            "postTokenBalances": [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(after)}},
                                  {"owner": "POOL", "mint": SOL, "uiTokenAmount": {"amount": "9"}}]}}


def test_buys_from_tx():
    assert buys_from_tx(tx("W", "MEME", 1), "W") == ["MEME"]
    assert buys_from_tx(tx("W", "MEME", 1, sell=True), "W") == []
    assert buys_from_tx(tx("W", "MEME", 1, err={"x": 1}), "W") == []
    assert buys_from_tx(tx("W", SOL, 1), "W") == []           # quote token is not a buy
    assert buys_from_tx(tx("OTHER", "MEME", 1), "W") == []


def test_load_wallets(tmp_path):
    f = tmp_path / "w.txt"
    a, b = "A" * 44, "B" * 43
    f.write_text(f"# comment\n{a}  # whale\n\n{b}\n{a}\nshort\n")
    assert load_wallets(f) == [a, b]
    assert load_wallets(tmp_path / "missing.txt") == []


class FakeChain:
    """wallet -> list of (signature, tx) newest first."""

    def __init__(self, history):
        self.history = history
        self.calls = []

    def __call__(self, url, payload, timeout=10.0):
        assert "api-key=KEY" in url
        method, params = payload["method"], payload["params"]
        self.calls.append(method)
        if method == "getSignaturesForAddress":
            wallet, opts = params
            out = []
            for sig, t in self.history[wallet]:
                if sig == opts.get("until"):
                    break
                out.append({"signature": sig, "blockTime": t["blockTime"], "err": t["meta"]["err"]})
            return out[:opts["limit"]]
        if method == "getTransaction":
            for txs in self.history.values():
                for sig, t in txs:
                    if sig == params[0]:
                        return t
        raise AssertionError(method)


def test_tracker_counts_and_saves_credits(monkeypatch):
    chain = FakeChain({
        "S1": [("a", tx("S1", "MEME", 2))],
        "S2": [("b", tx("S2", "MEME", 8))],
        "S3": [("d", tx("S3", "OTHER", 1)), ("c", tx("S3", "MEME", 9))],
        "S4": [("e", tx("S4", "MEME", 25))],     # outside 10-min smart window
        "K1": [("f", tx("K1", "MEME", 20)), ("old", tx("K1", "X", 120))],  # 2h old: skipped
    })
    monkeypatch.setattr(helius, "_post", chain)
    t = HeliusWalletTracker(["S1", "S2", "S3", "S4"], ["K1"], api_key="KEY")
    now = datetime.now()
    assert t("MEME", now) == (3, True)
    assert t("OTHER", now) == (1, False)
    assert t("X", now) == (0, False)
    # 5 signature calls + 6 tx fetches (the 2h-old one costs nothing)
    assert chain.calls.count("getSignaturesForAddress") == 5
    assert chain.calls.count("getTransaction") == 6
    assert t.credits == 11

    # next poll: nothing new -> only 5 cheap signature calls, no double counting
    t.refresh(force=True)
    assert chain.calls.count("getTransaction") == 6 and t.credits == 16
    assert t("MEME", now) == (3, True)

    # a new buy appears on S4
    chain.history["S4"].insert(0, ("g", tx("S4", "MEME", 0)))
    t.refresh(force=True)
    assert t("MEME", datetime.now()) == (4, True)


def test_tracker_disabled_without_key(monkeypatch):
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    monkeypatch.delenv("HELIUS_RPC_URL", raising=False)
    t = HeliusWalletTracker(["S1"], [])
    assert not t.enabled and t("MEME", datetime.now()) == (0, False) and "off" in t.status
