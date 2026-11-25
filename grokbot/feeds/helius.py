"""Smart-money / KOL wallet tracker over the Helius RPC (standard Solana JSON-RPC).

Credit-friendly design (standard RPC calls cost 1 credit each on Helius):
  1. every `poll_seconds`, per wallet: getSignaturesForAddress(until=<last seen>)
     -> only NEW signatures come back (1 credit per wallet per poll)
  2. only for new, successful, recent signatures: getTransaction(jsonParsed)
     -> 1 credit per new transaction
A "buy" = the wallet's balance of a token (not SOL/USDC/USDT) went UP in that
transaction, read from pre/postTokenBalances.

Plug into the live feed:  LiveFeed(wallet_activity=tracker)
Called as tracker(mint, now) -> (smart wallets bought within 10 min, KOL bought within 30 min)

Key: env HELIUS_API_KEY (or a full RPC URL in HELIUS_RPC_URL).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",   # wSOL
    "EPjFWdd5AufqSSqeM2qFJEcpzEspzrD2fXZsTZG3mvgo",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
}


# Highest Solana transaction version we ask the RPC for. Newer versions exist on
# mainnet; asking for less makes getTransaction fail for those transactions.
MAX_TX_VERSION = 1


class RpcError(Exception):
    pass


def clean_key(value: str | None) -> str:
    """Accept the bare key, or a whole Helius URL pasted by mistake (...?api-key=KEY)."""
    v = (value or "").strip().strip("'\"")
    if "api-key=" in v:
        v = v.rsplit("api-key=", 1)[1].split("&", 1)[0]
    return v


def rpc_url_from_env(api_key: str | None = None) -> str:
    """HELIUS_RPC_URL if set, else the mainnet RPC with HELIUS_API_KEY; '' if neither."""
    url = (os.environ.get("HELIUS_RPC_URL") or "").strip().strip("'\"")
    if url:
        return url
    key = clean_key(api_key or os.environ.get("HELIUS_API_KEY"))
    return RPC.format(key=key) if key else ""


def _post(url: str, payload: dict, timeout: float = 10.0):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "grokbot-paper/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            src = "HELIUS_RPC_URL" if os.environ.get("HELIUS_RPC_URL") else "HELIUS_API_KEY"
            raise RpcError(f"HTTP {e.code}: Helius lehnt den Zugang ab - prüfe {src} "
                           "(der Key hat 36 Zeichen, z. B. mit: echo ${#HELIUS_API_KEY})") from None
        raise
    if "error" in body:
        raise RpcError(str(body["error"].get("message", body["error"])))
    return body.get("result")


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
    """Mints whose balance owned by `wallet` increased in this (jsonParsed) transaction."""
    meta = (tx or {}).get("meta") or {}
    if meta.get("err"):
        return []

    def balances(key):
        out: dict[str, float] = {}
        for b in meta.get(key) or []:
            if b.get("owner") == wallet:
                amt = float((b.get("uiTokenAmount") or {}).get("amount") or 0)
                out[b["mint"]] = out.get(b["mint"], 0.0) + amt
        return out

    pre, post = balances("preTokenBalances"), balances("postTokenBalances")
    return [m for m, amt in post.items() if amt > pre.get(m, 0.0) and m not in QUOTE_MINTS]


class HeliusWalletTracker:
    name = "WALLETS"

    def __init__(self, smart_wallets: list[str], kol_wallets: list[str],
                 api_key: str | None = None, rpc_url: str | None = None,
                 window_min: int = 10, kol_window_min: int = 30,
                 poll_seconds: int = 60, workers: int = 5, sig_limit: int = 25):
        self.rpc_url = rpc_url or rpc_url_from_env(api_key)
        self.smart = set(smart_wallets)
        self.kol = set(kol_wallets)
        self.window = timedelta(minutes=window_min)
        self.kol_window = timedelta(minutes=kol_window_min)
        self.poll_seconds = poll_seconds
        self.workers = workers
        self.sig_limit = sig_limit
        self.buys: dict[str, dict[str, datetime]] = {}   # mint -> wallet -> latest buy
        self._last_sig: dict[str, str] = {}                # wallet -> newest seen signature
        self._last_poll = 0.0
        self._lock = threading.Lock()
        self.errors = 0
        self.polls = 0
        self.credits = 0

    @property
    def enabled(self) -> bool:
        return bool(self.rpc_url) and bool(self.smart or self.kol)

    def _call(self, method: str, params: list):
        with self._lock:
            self.credits += 1
        return _post(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})

    # ----------------------------------------------------------------- polling
    def _poll_wallet(self, wallet: str) -> None:
        try:
            opts = {"limit": self.sig_limit}
            if wallet in self._last_sig:
                opts["until"] = self._last_sig[wallet]
            sigs = self._call("getSignaturesForAddress", [wallet, opts]) or []
            if not sigs:
                return
            self._last_sig[wallet] = sigs[0]["signature"]   # newest first
            oldest = time.time() - self.kol_window.total_seconds()
            for s in sigs:
                if s.get("err") or (s.get("blockTime") or 0) < oldest:
                    continue          # failed or too old to matter: don't spend a credit
                try:
                    tx = self._call("getTransaction", [s["signature"], {
                        "encoding": "jsonParsed", "maxSupportedTransactionVersion": MAX_TX_VERSION}])
                except RpcError:
                    with self._lock:
                        self.errors += 1
                    continue          # one unreadable tx must not hide the others
                ts = datetime.fromtimestamp((tx or {}).get("blockTime") or s["blockTime"])
                for mint in buys_from_tx(tx, wallet):
                    with self._lock:
                        prev = self.buys.setdefault(mint, {}).get(wallet)
                        if prev is None or ts > prev:
                            self.buys[mint][wallet] = ts
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, RpcError):
            with self._lock:
                self.errors += 1

    def refresh(self, force: bool = False) -> None:
        if not self.enabled:
            return
        if not force and time.time() - self._last_poll < self.poll_seconds:
            return
        self._last_poll = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            list(pool.map(self._poll_wallet, sorted(self.smart | self.kol)))
        self.polls += 1
        self._prune()

    def _prune(self) -> None:
        cutoff = datetime.now() - max(self.window, self.kol_window) - timedelta(minutes=5)
        with self._lock:
            for mint in list(self.buys):
                self.buys[mint] = {w: t for w, t in self.buys[mint].items() if t >= cutoff}
                if not self.buys[mint]:
                    del self.buys[mint]

    # --------------------------------------------------------------- interface
    def __call__(self, mint: str, now: datetime) -> tuple[int, bool]:
        self.refresh()
        with self._lock:
            by_wallet = dict(self.buys.get(mint, {}))
        lo_s, lo_k, hi = now - self.window, now - self.kol_window, now + timedelta(minutes=1)
        smart = sum(1 for w, t in by_wallet.items() if w in self.smart and lo_s <= t <= hi)
        kol = any(w in self.kol and lo_k <= t <= hi for w, t in by_wallet.items())
        return smart, kol

    @property
    def status(self) -> str:
        if not self.enabled:
            return "off (no HELIUS_API_KEY or no wallets)"
        return (f"{len(self.smart)} smart / {len(self.kol)} KOL wallets, {self.polls} polls, "
                f"~{self.credits} credits, {self.errors} errors, {len(self.buys)} tokens bought")
