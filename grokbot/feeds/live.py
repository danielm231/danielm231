"""Live market data from public APIs (no keys needed). Trades stay PAPER trades.

  DexScreener  - new Solana pairs, price, liquidity, volume, buys/sells
  RugCheck     - mint/freeze authority, holders, top10, LP lock, creator
  Jupiter      - sell-quote as honeypot simulation

Smart-money / KOL tracking needs a wallet-activity provider (e.g. Helius
webhooks). Plug one in via `wallet_activity`; without it those two signals
never fire and the bot relies on volume / buy-ratio / holder growth.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime
from typing import Callable

from ..models import PairSnapshot

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1"
JUP = "https://lite-api.jup.ag/swap/v1/quote"
SOL = "So11111111111111111111111111111111111111112"
DEXES = {"pumpfun", "pumpswap", "raydium", "meteora"}

WalletActivity = Callable[[str, datetime], tuple[int, bool]]   # -> (smart buys 10m, kol bought)


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "grokbot-paper/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


class LiveFeed:
    def __init__(self, wallet_activity: WalletActivity | None = None,
                 rug_cache_s: int = 300, max_tokens: int = 60):
        self.wallet_activity = wallet_activity
        self.tracker = None   # optional: object with .name/.status for the dashboard
        self.rug_cache_s = rug_cache_s
        self.max_tokens = max_tokens
        self.watch: deque[str] = deque(maxlen=max_tokens)    # mints we follow
        self._rug: dict[str, tuple[float, dict]] = {}
        self._holders: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
        self._prices: dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        self._honeypot: dict[str, bool] = {}
        self._dev_prev: dict[str, float] = {}
        self.errors = 0

    # ------------------------------------------------------------- discovery
    def _discover(self) -> None:
        for path in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
            try:
                for item in _get(DEX + path):
                    if item.get("chainId") == "solana":
                        mint = item.get("tokenAddress")
                        if mint and mint not in self.watch:
                            self.watch.append(mint)
            except (urllib.error.URLError, TimeoutError, ValueError):
                self.errors += 1

    def _pairs(self, mints: list[str]) -> dict[str, dict]:
        best: dict[str, dict] = {}
        for i in range(0, len(mints), 30):
            chunk = ",".join(mints[i:i + 30])
            try:
                pairs = _get(f"{DEX}/tokens/v1/solana/{chunk}")
            except (urllib.error.URLError, TimeoutError, ValueError):
                self.errors += 1
                continue
            for p in pairs or []:
                if p.get("dexId", "").lower() not in DEXES:
                    continue
                m = p["baseToken"]["address"]
                liq = (p.get("liquidity") or {}).get("usd") or 0
                if m not in best or liq > ((best[m].get("liquidity") or {}).get("usd") or 0):
                    best[m] = p
        return best

    # ----------------------------------------------------------------- safety
    def _rugcheck(self, mint: str) -> dict:
        hit = self._rug.get(mint)
        if hit and time.time() - hit[0] < self.rug_cache_s:
            return hit[1]
        try:
            rep = _get(f"{RUG}/tokens/{mint}/report")
        except (urllib.error.URLError, TimeoutError, ValueError):
            self.errors += 1
            rep = hit[1] if hit else {}
        self._rug[mint] = (time.time(), rep)
        return rep

    def _is_honeypot(self, mint: str, decimals: int = 6) -> bool:
        if mint in self._honeypot:
            return self._honeypot[mint]
        try:
            q = _get(f"{JUP}?inputMint={mint}&outputMint={SOL}"
                     f"&amount={10 ** decimals}&slippageBps=300")
            hp = not q.get("outAmount")
        except (urllib.error.URLError, TimeoutError, ValueError):
            hp = True   # cannot simulate a sell -> treat as honeypot
        self._honeypot[mint] = hp
        return hp

    @staticmethod
    def _safety(rep: dict) -> dict:
        top = rep.get("topHolders") or []
        creator = rep.get("creator")
        dev_pct = sum(h.get("pct", 0) for h in top if h.get("owner") == creator)
        top10 = sum(h.get("pct", 0) for h in top[:10])
        markets = rep.get("markets") or []
        lp_pct = max([(m.get("lp") or {}).get("lpLockedPct", 0) for m in markets] or [0])
        lock_days = 0
        for lk in (rep.get("lockers") or {}).values():
            unlock = lk.get("unlockDate") or 0
            if unlock:
                lock_days = max(lock_days, int((unlock - time.time()) / 86400))
        risks = " ".join(r.get("name", "").lower() for r in rep.get("risks") or [])
        return {
            "mint_open": bool(rep.get("mintAuthority")),
            "freeze_open": bool(rep.get("freezeAuthority")),
            "holders": int(rep.get("totalHolders") or 0),
            "dev_pct": dev_pct,
            "top10": top10,
            "lp_burned": lp_pct >= 99 and lock_days == 0,
            "lp_locked": lp_pct >= 90,
            "lock_days": lock_days if lock_days else (9999 if lp_pct >= 99 else 0),
            "bundled": "bundle" in risks,
            "rugged": bool(rep.get("rugged")),
        }

    # -------------------------------------------------------------- snapshots
    def _to_snapshot(self, mint: str, p: dict, now: datetime, deep: bool) -> PairSnapshot:
        price = float(p.get("priceUsd") or 0)
        self._prices[mint].append((now, price))
        hist = [(t, v) for t, v in self._prices[mint] if (now - t).total_seconds() <= 3600]
        ch1h = float((p.get("priceChange") or {}).get("h1") or 0)
        start_1h = price / (1 + ch1h / 100) if ch1h > -100 else price
        high_1h = max([v for _, v in hist] + [price, start_1h])

        sf = self._safety(self._rugcheck(mint)) if deep else {}
        holders = sf.get("holders", 0)
        if holders:
            self._holders[mint].append((now, holders))
        h15 = [v for t, v in self._holders[mint] if (now - t).total_seconds() <= 900]
        smart, kol = (self.wallet_activity(mint, now) if self.wallet_activity else (0, False))
        tx = (p.get("txns") or {}).get("m5") or {}
        created_ms = p.get("pairCreatedAt") or 0
        return PairSnapshot(
            mint=mint, symbol=p["baseToken"].get("symbol", mint[:5]), dex=p.get("dexId", "?"),
            created_at=datetime.fromtimestamp(created_ms / 1000) if created_ms else now,  # local time
            price=price, liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
            volume_5m=float((p.get("volume") or {}).get("m5") or 0),
            volume_1h=float((p.get("volume") or {}).get("h1") or 0),
            buys_5m=int(tx.get("buys") or 0), sells_5m=int(tx.get("sells") or 0),
            holders=holders, holders_15m_ago=h15[0] if h15 else holders,
            price_change_1h_pct=ch1h, high_1h=high_1h,
            mint_authority_open=sf.get("mint_open", False),
            freeze_authority_open=sf.get("freeze_open", False),
            lp_locked=sf.get("lp_locked", True), lp_burned=sf.get("lp_burned", False),
            lp_lock_days=sf.get("lock_days", 0), dev_pct=sf.get("dev_pct", 0.0),
            top10_pct=sf.get("top10", 0.0), bundled_launch=sf.get("bundled", False),
            honeypot=self._is_honeypot(mint) if deep else False,
            smart_wallet_buys_10m=smart, kol_bought=kol,
            dev_sold=False, lp_pulled=sf.get("rugged", False),
        )

    def snapshots(self, now: datetime) -> list[PairSnapshot]:
        if hasattr(self.wallet_activity, "refresh"):
            self.wallet_activity.refresh()     # keep wallet history fresh every tick
        self._discover()
        pairs = self._pairs(list(self.watch))
        out = []
        for mint, p in pairs.items():
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            vol = float((p.get("volume") or {}).get("m5") or 0)
            # only pay for RugCheck/Jupiter calls on pairs that can pass the cheap filters
            deep = liq >= 40_000 and vol >= 25_000
            out.append(self._to_snapshot(mint, p, now, deep))
        return out

    def get(self, mint: str, now: datetime) -> PairSnapshot | None:
        pairs = self._pairs([mint])
        if mint not in pairs:
            return None
        snap = self._to_snapshot(mint, pairs[mint], now, deep=True)
        # dev-sell detection: creator balance dropped since the last report
        prev = self._dev_prev.get(mint)
        if prev and snap.dev_pct < prev * 0.7:
            snap.dev_sold = True
        self._dev_prev[mint] = snap.dev_pct
        return snap
