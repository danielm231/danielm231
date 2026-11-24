"""Smart-money / KOL wallet tracker via the Helius Enhanced Transactions API.

Every `poll_seconds` it fetches the latest SWAP transactions of every tracked
wallet (in parallel) and records which wallet bought which token when.
A "buy" is a swap in which the wallet receives a token that is not SOL / a
stablecoin.

Plug into the live feed:  LiveFeed(wallet_activity=tracker)
The tracker is called as tracker(mint, now) -> (smart wallets bought in 10 min,
KOL bought within kol_window).

Needs a free Helius API key: https://dashboard.helius.dev  ->  env HELIUS_API_KEY
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

API = "https://api.helius.xyz/v0/addresses/{addr}/transactions"
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qFJEcpzEspzrD2fXZsTZG3mvgo",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


def _get(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": "grokbot-paper/1.0",
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_wallets(path: str | Path | None) -> list[str]:
    """One address per line; '#' starts a comment; anything after the address is ignored."""
    if not path or not Path(path).exists():
        return []
    out = []
    for line in Path(path).read_text().splitlines():
        addr = line.split("#", 1)[0].strip().split()
        if addr and 32 <= len(addr[0]) <= 44:
            out.append(addr[0])
    return list(dict.fromkeys(out))


def buys_from_tx(tx: dict, wallet: str) -> list[str]:
    """Mints the wallet received in this swap transaction (i.e. bought)."""
    if tx.get("transactionError"):
        return []
    received = {t.get("mint") for t in tx.get("tokenTransfers") or []
                if t.get("toUserAccount") == wallet and float(t.get("tokenAmount") or 0) > 0}
    sent = {t.get("mint") for t in tx.get("tokenTransfers") or []
            if t.get("fromUserAccount") == wallet}
    # receiving a token you also sent in the same tx is a transfer/route, not a buy
    return [m for m in received - sent - QUOTE_MINTS if m]


class HeliusWalletTracker:
    name = "WALLETS"

    def __init__(self, smart_wallets: list[str], kol_wallets: list[str],
                 api_key: str | None = None, window_min: int = 10, kol_window_min: int = 30,
                 poll_seconds: int = 60, workers: int = 5, tx_limit: int = 20):
        self.api_key = api_key or os.environ.get("HELIUS_API_KEY", "")
        self.smart = set(smart_wallets)
        self.kol = set(kol_wallets)
        self.window = timedelta(minutes=window_min)
        self.kol_window = timedelta(minutes=kol_window_min)
        self.poll_seconds = poll_seconds
        self.workers = workers
        self.tx_limit = tx_limit
        # mint -> wallet -> latest buy time
        self.buys: dict[str, dict[str, datetime]] = {}
        self._seen: set[str] = set()
        self._last_poll = 0.0
        self._lock = threading.Lock()
        self.errors = 0
        self.polls = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and bool(self.smart or self.kol)

    # ----------------------------------------------------------------- polling
    def _fetch(self, wallet: str) -> list[dict]:
        q = urllib.parse.urlencode({"api-key": self.api_key, "type": "SWAP",
                                    "limit": self.tx_limit})
        try:
            return _get(API.format(addr=wallet) + "?" + q) or []
        except (urllib.error.URLError, TimeoutError, ValueError):
            with self._lock:
                self.errors += 1
            return []

    def ingest(self, wallet: str, txs: list[dict]) -> None:
        with self._lock:
            for tx in txs:
                sig = tx.get("signature")
                if not sig or sig in self._seen:
                    continue
                self._seen.add(sig)
                ts = datetime.fromtimestamp(tx.get("timestamp") or 0)
                for mint in buys_from_tx(tx, wallet):
                    prev = self.buys.setdefault(mint, {}).get(wallet)
                    if prev is None or ts > prev:
                        self.buys[mint][wallet] = ts

    def refresh(self, force: bool = False) -> None:
        if not self.enabled:
            return
        if not force and time.time() - self._last_poll < self.poll_seconds:
            return
        self._last_poll = time.time()
        wallets = sorted(self.smart | self.kol)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for wallet, txs in zip(wallets, pool.map(self._fetch, wallets)):
                self.ingest(wallet, txs)
        self.polls += 1
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now() - max(self.window, self.kol_window) - timedelta(minutes=5)
        with self._lock:
            for mint in list(self.buys):
                self.buys[mint] = {w: t for w, t in self.buys[mint].items() if t >= cutoff}
                if not self.buys[mint]:
                    del self.buys[mint]
            if len(self._seen) > 50_000:
                self._seen.clear()

    # --------------------------------------------------------------- interface
    def __call__(self, mint: str, now: datetime) -> tuple[int, bool]:
        self.refresh()
        with self._lock:
            by_wallet = dict(self.buys.get(mint, {}))
        smart = sum(1 for w, t in by_wallet.items()
                    if w in self.smart and now - self.window <= t <= now + timedelta(minutes=1))
        kol = any(w in self.kol and now - self.kol_window <= t <= now + timedelta(minutes=1)
                  for w, t in by_wallet.items())
        return smart, kol

    @property
    def status(self) -> str:
        if not self.enabled:
            return "off (no HELIUS_API_KEY or no wallets)"
        return (f"{len(self.smart)} smart / {len(self.kol)} KOL wallets, "
                f"{self.polls} polls, {self.errors} errors, {len(self.buys)} tokens bought")
