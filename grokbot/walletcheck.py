"""Wallet checker: scores candidate smart-money wallets from their on-chain swaps.

    python -m grokbot.walletcheck kandidaten.txt --days 30

For every wallet it loads the swaps of the last N days over the Helius RPC,
pairs buys and sells per token (priced in SOL) and computes realized PnL,
win rate, hold times and red flags. Verdict per wallet: GUT / PRUEFEN / RAUS.

Credits: ~1 per 1000 signatures + 1 per loaded transaction. --max-tx caps
transactions per wallet, --max-credits caps the whole run.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import time
import urllib.error
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .feeds import helius
from .feeds.helius import MAX_TX_VERSION, QUOTE_MINTS, RpcError, load_wallets

WSOL = "So11111111111111111111111111111111111111112"
LAMPORTS = 1e9


# --------------------------------------------------------------------- parsing
@dataclass
class Swap:
    ts: float
    mint: str
    tokens: float     # + bought / - sold (raw units)
    sol: float        # - spent / + received


def swap_from_tx(tx: dict, wallet: str) -> Swap | None:
    """Turn a jsonParsed transaction into a SOL<->token swap of `wallet`, or None."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return None
    keys = [k["pubkey"] if isinstance(k, dict) else k
            for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    if wallet not in keys:
        return None
    i = keys.index(wallet)
    native = (meta["postBalances"][i] - meta["preBalances"][i]) / LAMPORTS

    def bal(key):
        out: dict[str, float] = defaultdict(float)
        for b in meta.get(key) or []:
            if b.get("owner") == wallet:
                out[b["mint"]] += float((b.get("uiTokenAmount") or {}).get("amount") or 0)
        return out

    pre, post = bal("preTokenBalances"), bal("postTokenBalances")
    deltas = {m: post.get(m, 0) - pre.get(m, 0) for m in set(pre) | set(post)}
    wsol = deltas.pop(WSOL, 0) / LAMPORTS
    if any(abs(deltas.get(m, 0)) > 0 for m in QUOTE_MINTS - {WSOL}):
        return None                                   # USDC/USDT trade: not priced in SOL
    changed = {m: d for m, d in deltas.items() if d != 0}
    if len(changed) != 1:
        return None                                   # no swap or token->token route
    (mint, d), = changed.items()
    sol = native + wsol
    if (d > 0 and sol >= 0) or (d < 0 and sol <= 0):
        return None                                   # transfer / airdrop, not a trade
    return Swap(ts=tx.get("blockTime") or 0, mint=mint, tokens=d, sol=sol)


# --------------------------------------------------------------------- scoring
@dataclass
class TokenTrade:
    mint: str
    cost: float = 0.0
    proceeds: float = 0.0
    bought: float = 0.0
    sold: float = 0.0
    first_buy: float = 0.0
    last_sell: float = 0.0

    @property
    def closed(self) -> bool:
        return self.bought > 0 and self.sold >= 0.9 * self.bought

    @property
    def pnl(self) -> float:
        frac = min(1.0, self.sold / self.bought) if self.bought else 0
        return self.proceeds - self.cost * frac

    @property
    def hold_s(self) -> float:
        return max(0.0, self.last_sell - self.first_buy)


@dataclass
class WalletReport:
    wallet: str
    swaps: int = 0
    closed: int = 0
    open: int = 0
    wins: int = 0
    pnl_sol: float = 0.0
    best_sol: float = 0.0
    top_share: float = 0.0
    median_hold_min: float = 0.0
    flip_share: float = 0.0
    avg_size_sol: float = 0.0
    wallet_age_days: float | None = None
    funder: str | None = None
    credits: int = 0
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return 100 * self.wins / self.closed if self.closed else 0.0


def score(wallet: str, swaps: list[Swap], age_days: float | None = None,
          funder: str | None = None, min_closed: int = 10) -> WalletReport:
    trades: dict[str, TokenTrade] = {}
    for s in sorted(swaps, key=lambda x: x.ts):
        t = trades.setdefault(s.mint, TokenTrade(s.mint))
        if s.tokens > 0:
            if not t.bought:
                t.first_buy = s.ts
            t.bought += s.tokens
            t.cost += -s.sol
        elif t.bought:                     # ignore sells of tokens bought before the window
            t.sold += -s.tokens
            t.proceeds += s.sol
            t.last_sell = s.ts
    closed = [t for t in trades.values() if t.closed]
    r = WalletReport(wallet, swaps=len(swaps), closed=len(closed),
                     open=sum(1 for t in trades.values() if t.bought and not t.closed),
                     wallet_age_days=age_days, funder=funder)
    if closed:
        pnls = [t.pnl for t in closed]
        r.wins = sum(p > 0 for p in pnls)
        r.pnl_sol = sum(pnls)
        r.best_sol = max(pnls)
        gains = sum(p for p in pnls if p > 0)
        r.top_share = 100 * r.best_sol / gains if gains > 0 else 0
        r.median_hold_min = statistics.median(t.hold_s for t in closed) / 60
        r.flip_share = 100 * sum(t.hold_s < 60 for t in closed) / len(closed)
        r.avg_size_sol = statistics.mean(t.cost for t in closed)

    bad, warn = [], []
    if r.closed < min_closed:
        warn.append(f"nur {r.closed} abgeschlossene Trades")
    if r.closed and r.pnl_sol <= 0:
        bad.append(f"Verlust {r.pnl_sol:+.2f} SOL")
    if r.closed >= min_closed and r.win_rate > 85:
        warn.append(f"Trefferquote {r.win_rate:.0f}% verdächtig hoch (Lockvogel?)")
    if r.closed >= min_closed and r.win_rate < 30:
        bad.append(f"Trefferquote nur {r.win_rate:.0f}%")
    if r.top_share > 60 and r.closed >= 3:
        warn.append(f"{r.top_share:.0f}% des Gewinns aus einem Trade")
    if r.flip_share > 30:
        bad.append(f"{r.flip_share:.0f}% der Trades < 60 s gehalten (Sniper/Bot)")
    elif r.closed and r.median_hold_min < 2:
        warn.append(f"Median-Haltedauer {r.median_hold_min:.1f} min sehr kurz")
    if r.closed and r.avg_size_sol < 0.2:
        warn.append(f"Ø Einsatz nur {r.avg_size_sol:.2f} SOL")
    if age_days is not None and age_days < 14:
        warn.append(f"Wallet erst {age_days:.0f} Tage alt" + (f", finanziert von {funder}" if funder else ""))
    r.reasons = bad + warn
    r.verdict = "RAUS" if bad else ("PRUEFEN" if warn else "GUT")
    return r


# ------------------------------------------------------------------------ RPC
class Checker:
    def __init__(self, rpc_url: str, days: int = 30, max_tx: int = 300, workers: int = 4,
                 max_credits: int | None = None, min_closed: int = 10):
        self.rpc_url = rpc_url
        self.min_closed = min_closed
        self.cutoff = time.time() - days * 86400
        self.max_tx = max_tx
        self.workers = workers
        self.max_credits = max_credits
        self.credits = 0
        self.skipped: list[str] = []

    def _call(self, method: str, params: list):
        if self.max_credits is not None and self.credits >= self.max_credits:
            raise BudgetExceeded()
        self.credits += 1
        for attempt in range(4):
            try:
                return helius._post(self.rpc_url, {"jsonrpc": "2.0", "id": 1,
                                                   "method": method, "params": params})
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 3:
                    raise
                time.sleep(1.5 * (attempt + 1))      # rate limited: back off

    def signatures(self, wallet: str) -> tuple[list[dict], bool]:
        """Successful signatures in the window (newest first) and whether we saw the wallet's first tx."""
        out, before, reached_start = [], None, False
        while len(out) < self.max_tx:
            opts = {"limit": 1000}
            if before:
                opts["before"] = before
            page = self._call("getSignaturesForAddress", [wallet, opts]) or []
            for s in page:
                if (s.get("blockTime") or 0) < self.cutoff:
                    return out, False
                if not s.get("err"):
                    out.append(s)
            if len(page) < 1000:
                reached_start = True
                break
            before = page[-1]["signature"]
        return out[: self.max_tx], reached_start and len(out) <= self.max_tx

    def _tx(self, sig: str) -> dict | None:
        try:
            return self._call("getTransaction", [sig, {"encoding": "jsonParsed",
                                                       "maxSupportedTransactionVersion": MAX_TX_VERSION}])
        except RpcError as e:              # skip this one transaction, keep the rest
            self.skipped.append(str(e))
            return None

    @staticmethod
    def funder(wallet: str, tx: dict | None) -> str | None:
        """Who sent SOL to the wallet in its first transaction (already loaded, no credit)."""
        for ix in (((tx or {}).get("transaction") or {}).get("message") or {}).get("instructions") or []:
            parsed = ix.get("parsed")
            info = parsed.get("info") or {} if isinstance(parsed, dict) else {}
            if info.get("destination") == wallet and info.get("source"):
                return info["source"]
        return None

    def check(self, wallet: str) -> WalletReport:
        start = self.credits
        self.skipped = []
        try:
            sigs, reached_start = self.signatures(wallet)
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                txs = list(pool.map(self._tx, [s["signature"] for s in sigs]))
            swaps = [sw for tx in txs if (sw := swap_from_tx(tx, wallet))]
            age = funder = None
            if reached_start and sigs:
                age = (time.time() - (sigs[-1].get("blockTime") or time.time())) / 86400
                if age < 14:
                    funder = self.funder(wallet, txs[-1])
            rep = score(wallet, swaps, age, funder, self.min_closed)
            if self.skipped:
                n = len(self.skipped)
                if sigs and n == len(sigs):
                    rep.verdict = "FEHLER"
                    rep.reasons = [f"keine Transaktion lesbar: {self.skipped[0][:200]}"]
                else:
                    rep.reasons.append(f"{n} von {len(sigs)} Transaktionen nicht lesbar")
        except BudgetExceeded:
            rep = WalletReport(wallet, verdict="ABGEBROCHEN", reasons=["Credit-Limit erreicht"])
        except (urllib.error.URLError, TimeoutError, ValueError, RpcError) as e:
            rep = WalletReport(wallet, verdict="FEHLER", reasons=[str(e)[:200]])
        rep.credits = self.credits - start
        return rep


class BudgetExceeded(Exception):
    pass


# ------------------------------------------------------------------------ CLI
def _print(reports: list[WalletReport]) -> None:
    hdr = f"{'Wallet':<14} {'Urteil':<11} {'Trades':>6} {'Win%':>5} {'PnL SOL':>8} {'Hold':>6} {'Ø SOL':>6}  Gründe"
    print(hdr + "\n" + "-" * len(hdr))
    for r in reports:
        print(f"{r.wallet[:6]}…{r.wallet[-6:]} {r.verdict:<11} {r.closed:>6} {r.win_rate:>5.0f} "
              f"{r.pnl_sol:>+8.2f} {r.median_hold_min:>5.0f}m {r.avg_size_sol:>6.2f}  "
              + "; ".join(r.reasons))


def main(argv=None) -> list[WalletReport]:
    ap = argparse.ArgumentParser(description="Score candidate smart-money wallets (Helius RPC)")
    ap.add_argument("wallets", help="file with one wallet address per line")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-tx", type=int, default=300, help="max transactions per wallet (= credits)")
    ap.add_argument("--max-credits", type=int, default=20_000, help="stop the run at this many credits")
    ap.add_argument("--min-trades", type=int, default=10)
    ap.add_argument("--csv", default="wallet_report.csv")
    ap.add_argument("--good-out", default="wallets/gepruefte_wallets.txt",
                    help="GUT wallets are written here in wallets/*.txt format")
    args = ap.parse_args(argv)

    url = helius.rpc_url_from_env()
    if not url:
        raise SystemExit("HELIUS_API_KEY (oder HELIUS_RPC_URL) ist nicht gesetzt.")
    wallets = load_wallets(args.wallets)
    if not wallets:
        raise SystemExit(f"Keine Wallet-Adressen in {args.wallets} gefunden.")
    print(f"Prüfe {len(wallets)} Wallets, {args.days} Tage, max {args.max_tx} Transaktionen je Wallet "
          f"(≤ ~{len(wallets) * (args.max_tx + 2):,} Credits, Limit {args.max_credits:,})\n")

    checker = Checker(url, days=args.days, max_tx=args.max_tx, max_credits=args.max_credits,
                      min_closed=args.min_trades)
    reports = []
    for i, w in enumerate(wallets, 1):
        r = checker.check(w)
        reports.append(r)
        print(f"[{i}/{len(wallets)}] {w[:6]}…{w[-6:]}  {r.verdict}  ({r.credits} credits)")
    order = {"GUT": 0, "PRUEFEN": 1, "RAUS": 2, "FEHLER": 3, "ABGEBROCHEN": 4}
    reports.sort(key=lambda r: (order.get(r.verdict, 9), -r.pnl_sol))
    print()
    _print(reports)
    print(f"\nVerbrauchte Credits: ~{checker.credits:,}")

    with open(args.csv, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["wallet", "verdict", "closed_trades", "open", "win_rate", "pnl_sol", "best_sol",
                     "top_share_pct", "median_hold_min", "flip_share_pct", "avg_size_sol",
                     "wallet_age_days", "funder", "reasons"])
        for r in reports:
            wr.writerow([r.wallet, r.verdict, r.closed, r.open, round(r.win_rate, 1),
                         round(r.pnl_sol, 3), round(r.best_sol, 3), round(r.top_share, 1),
                         round(r.median_hold_min, 1), round(r.flip_share, 1),
                         round(r.avg_size_sol, 3),
                         "" if r.wallet_age_days is None else round(r.wallet_age_days, 1),
                         r.funder or "", "; ".join(r.reasons)])
    good = [r for r in reports if r.verdict == "GUT"]
    Path(args.good_out).parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    Path(args.good_out).write_text(
        f"# Geprüft am {stamp}: {len(good)} von {len(reports)} Wallets mit Urteil GUT.\n"
        "# Kontrollieren und dann nach wallets/smart_money.txt kopieren.\n"
        + "".join(f"{r.wallet}  # {r.closed} Trades, {r.win_rate:.0f}% Win, "
                  f"{r.pnl_sol:+.1f} SOL, Hold {r.median_hold_min:.0f}m\n" for r in good))
    print(f"Bericht: {args.csv}\nGUT-Wallets: {args.good_out}")
    return reports


if __name__ == "__main__":
    main()
