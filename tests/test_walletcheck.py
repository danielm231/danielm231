"""Wallet checker against recorded-shape JSON-RPC responses (no network)."""
import time

import pytest

from grokbot import walletcheck
from grokbot.feeds import helius
from grokbot.walletcheck import Swap, score, swap_from_tx

W = "W" * 44
USDC = "EPjFWdd5AufqSSqeM2qFJEcpzEspzrD2fXZsTZG3mvgo"
NOW = time.time()


def rpc_tx(wallet, mint, tokens, sol, ts, *, err=None, extra_mint=None):
    """Swap tx: wallet's token balance changes by `tokens`, native SOL by `sol`."""
    pre_tok, post_tok = (0, tokens) if tokens > 0 else (-tokens, 0)
    pre = [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(pre_tok)}}]
    post = [{"owner": wallet, "mint": mint, "uiTokenAmount": {"amount": str(post_tok)}}]
    if extra_mint:
        post.append({"owner": wallet, "mint": extra_mint, "uiTokenAmount": {"amount": "5"}})
    return {"blockTime": int(ts),
            "transaction": {"message": {"accountKeys": [{"pubkey": wallet}, {"pubkey": "POOL"}]}},
            "meta": {"err": err, "preBalances": [10 * 10**9, 0],
                     "postBalances": [int(10 * 10**9 + sol * 10**9), 0],
                     "preTokenBalances": pre, "postTokenBalances": post}}


def test_swap_from_tx():
    buy = swap_from_tx(rpc_tx(W, "MEME", 1000, -0.5, NOW), W)
    assert buy.mint == "MEME" and buy.tokens == 1000 and buy.sol == pytest.approx(-0.5)
    sell = swap_from_tx(rpc_tx(W, "MEME", -1000, 1.2, NOW), W)
    assert sell.tokens == -1000 and sell.sol == pytest.approx(1.2)
    assert swap_from_tx(rpc_tx(W, "MEME", 1000, -0.5, NOW, err={"x": 1}), W) is None
    assert swap_from_tx(rpc_tx(W, "MEME", 1000, 0.0, NOW), W) is None          # airdrop
    assert swap_from_tx(rpc_tx(W, "MEME", 1000, -0.5, NOW, extra_mint=USDC), W) is None
    assert swap_from_tx(rpc_tx(W, "MEME", 1000, -0.5, NOW, extra_mint="X"), W) is None
    assert swap_from_tx(rpc_tx("OTHER", "MEME", 1000, -0.5, NOW), W) is None


def trades(n_win, n_loss, hold_s=600, size=1.0, win_x=2.0, loss_x=0.6):
    out, t = [], NOW - 20 * 86400
    for i in range(n_win + n_loss):
        x = win_x if i < n_win else loss_x
        out += [Swap(t, f"T{i}", 100, -size), Swap(t + hold_s, f"T{i}", -100, size * x)]
        t += 3600
    return out


def test_score_good_wallet():
    r = score(W, trades(8, 6), age_days=200)
    assert r.verdict == "GUT", r.reasons
    assert r.closed == 14 and r.wins == 8
    assert r.pnl_sol == pytest.approx(8 * 1.0 - 6 * 0.4)
    assert r.median_hold_min == pytest.approx(10)


def test_score_red_flags():
    assert score(W, trades(3, 12)).verdict == "RAUS"                       # loses money
    sniper = score(W, trades(10, 4, hold_s=20))
    assert sniper.verdict == "RAUS" and any("Sniper" in x for x in sniper.reasons)
    decoy = score(W, trades(14, 1), age_days=5, funder="DEV")
    assert decoy.verdict == "PRUEFEN"
    assert any("verdächtig" in x for x in decoy.reasons) and any("DEV" in x for x in decoy.reasons)
    few = score(W, trades(3, 1))
    assert few.verdict == "PRUEFEN" and any("nur 4" in x for x in few.reasons)
    one_hit = score(W, trades(1, 10, win_x=50))
    assert any("aus einem Trade" in x for x in one_hit.reasons)
    # sells of tokens bought before the window are ignored
    assert score(W, [Swap(NOW, "OLD", -100, 5.0)]).closed == 0


class FakeRpc:
    def __init__(self, history):
        self.history = history          # wallet -> list of (sig, tx) newest first
        self.by_sig = {s: t for txs in history.values() for s, t in txs}
        self.calls = 0

    def __call__(self, url, payload, timeout=10.0):
        self.calls += 1
        m, p = payload["method"], payload["params"]
        if m == "getSignaturesForAddress":
            return [{"signature": s, "blockTime": t["blockTime"], "err": t["meta"]["err"]}
                    for s, t in self.history[p[0]]]
        if m == "getTransaction":
            return self.by_sig[p[0]]
        raise AssertionError(m)


def build_history(wallet, n_win, n_loss, hold_s=600, funded_by=None):
    txs, t = [], NOW - 10 * 86400
    for i in range(n_win + n_loss):
        x = 2.0 if i < n_win else 0.6
        txs.append((f"{wallet[:3]}b{i}", rpc_tx(wallet, f"T{i}", 100, -1.0, t)))
        txs.append((f"{wallet[:3]}s{i}", rpc_tx(wallet, f"T{i}", -100, x, t + hold_s)))
        t += 3600
    if funded_by:
        first = {"blockTime": int(NOW - 10 * 86400 - 60), "meta": {"err": None},
                 "transaction": {"message": {"accountKeys": [], "instructions": [
                     {"parsed": {"type": "transfer",
                                 "info": {"source": funded_by, "destination": wallet}}}]}}}
        txs.insert(0, (f"{wallet[:3]}fund", first))   # oldest transaction
    return list(reversed(txs))


def test_checker_end_to_end(monkeypatch, tmp_path):
    good, bot = "G" * 44, "B" * 44
    rpc = FakeRpc({good: build_history(good, 9, 5, funded_by="CEX"),
                   bot: build_history(bot, 9, 5, hold_s=10)})
    monkeypatch.setattr(helius, "_post", rpc)
    monkeypatch.setenv("HELIUS_API_KEY", "KEY")
    cands = tmp_path / "c.txt"
    cands.write_text(f"{good}\n{bot}\n")
    out_csv, out_good = tmp_path / "r.csv", tmp_path / "good.txt"
    reports = walletcheck.main([str(cands), "--csv", str(out_csv), "--good-out", str(out_good)])

    by = {r.wallet: r for r in reports}
    # fresh 10-day wallet -> PRUEFEN with funder named; bot -> RAUS
    assert by[good].closed == 14 and by[good].funder == "CEX"
    assert by[good].verdict == "PRUEFEN" and any("10 Tage" in x for x in by[good].reasons)
    assert by[bot].verdict == "RAUS"
    assert out_csv.read_text().count("\n") == 3
    assert "0 von 2" in out_good.read_text()
    # per wallet: 1 signature page + 1 credit per tx; funder comes from the loaded oldest tx
    assert rpc.calls == (1 + 29) + (1 + 28)


def test_checker_credit_budget(monkeypatch):
    w = "C" * 44
    monkeypatch.setattr(helius, "_post", FakeRpc({w: build_history(w, 9, 5)}))
    r = walletcheck.Checker("http://x", max_credits=5).check(w)
    assert r.verdict == "ABGEBROCHEN" and r.credits == 5


def test_truncated_history_is_not_treated_as_fresh(monkeypatch):
    w = "D" * 44
    monkeypatch.setattr(helius, "_post", FakeRpc({w: build_history(w, 9, 5, funded_by="X")}))
    r = walletcheck.Checker("http://x", max_tx=10).check(w)
    assert r.wallet_age_days is None and r.funder is None and r.credits == 11
