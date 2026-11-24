"""Real execution: Jupiter swaps signed locally, sent over the Helius RPC.

Two modes:
  dry  - builds and signs the real transaction, lets the chain SIMULATE the buy
         (simulateTransaction) but sends nothing. Sells are quote-only, because
         the wallet does not actually hold the token.
  real - sends the transaction, waits for confirmation and books the ACTUAL
         amounts read back from the confirmed transaction.

Same interface as PaperBroker: impact_pct(), buy(), sell().
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..feeds import helius
from ..feeds.helius import QUOTE_MINTS, RpcError
from ..models import BrokerError, PairSnapshot
from ..walletcheck import swap_from_tx

JUP = "https://lite-api.jup.ag/swap/v1"
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qFJEcpzEspzrD2fXZsTZG3mvgo"
TOKEN_PROGRAMS = ["TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",   # SPL Token
                  "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"]   # Token-2022
LAMPORTS = 1_000_000_000


def _http(url: str, payload: dict | None = None, timeout: float = 15.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json",
                                          "User-Agent": "grokbot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:200]
        raise BrokerError(f"Jupiter HTTP {e.code}: {body}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise BrokerError(f"Jupiter nicht erreichbar: {e}") from None


# ------------------------------------------------------------------- chain
class Solana:
    """The few RPC calls the broker needs (1 Helius credit each)."""

    def __init__(self, rpc_url: str):
        self.rpc_url = rpc_url
        self._sol_price: tuple[float, float] | None = None
        self._decimals: dict[str, int] = {}

    @classmethod
    def from_env(cls) -> "Solana":
        key = os.environ.get("HELIUS_API_KEY", "")
        url = os.environ.get("HELIUS_RPC_URL") or (helius.RPC.format(key=key) if key else "")
        if not url:
            raise SystemExit("Für Dry-Run/Echtgeld wird HELIUS_API_KEY (oder HELIUS_RPC_URL) benötigt.")
        return cls(url)

    def rpc(self, method: str, params: list):
        try:
            return helius._post(self.rpc_url, {"jsonrpc": "2.0", "id": 1,
                                               "method": method, "params": params})
        except RpcError as e:
            raise BrokerError(f"RPC {method}: {e}") from None
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise BrokerError(f"RPC {method} nicht erreichbar: {e}") from None

    def sol_balance(self, owner: str) -> float:
        return (self.rpc("getBalance", [owner]) or {}).get("value", 0) / LAMPORTS

    def token_balances(self, owner: str) -> list[dict]:
        out = []
        for prog in TOKEN_PROGRAMS:
            res = self.rpc("getTokenAccountsByOwner",
                           [owner, {"programId": prog}, {"encoding": "jsonParsed"}]) or {}
            for acc in res.get("value") or []:
                info = acc["account"]["data"]["parsed"]["info"]
                amt = info["tokenAmount"]
                self._decimals[info["mint"]] = int(amt["decimals"])
                out.append({"mint": info["mint"], "raw": int(amt["amount"]),
                            "decimals": int(amt["decimals"]),
                            "ui": float(amt.get("uiAmount") or 0),
                            "account": acc["pubkey"], "program": prog})
        return out

    def decimals(self, mint: str) -> int:
        if mint not in self._decimals:
            res = self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
            info = (((res.get("value") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            if "decimals" not in info:
                raise BrokerError(f"Decimals für {mint} nicht lesbar")
            self._decimals[mint] = int(info["decimals"])
        return self._decimals[mint]

    def sol_price(self) -> float:
        if self._sol_price and time.time() - self._sol_price[0] < 60:
            return self._sol_price[1]
        q = _http(f"{JUP}/quote?inputMint={SOL_MINT}&outputMint={USDC_MINT}"
                  f"&amount={LAMPORTS}&slippageBps=50")
        price = int(q["outAmount"]) / 1e6
        self._sol_price = (time.time(), price)
        return price


# ------------------------------------------------------------------ config
@dataclass
class RealConfig:
    budget_usd: float = 15.0          # what the bot may use in total
    max_trade_usd: float = 5.0        # hard cap per trade, whatever the sizer says
    kill_pct: float = 40.0            # stop + sell all when wallet lost this % of budget
    sol_reserve: float = 0.02         # never spent: fees, rent
    max_priority_lamports: int = 200_000   # 0.0002 SOL max priority fee per swap
    confirm_timeout_s: float = 75.0
    buy_slippage_pct: float = 2.5
    sell_slippage_steps: tuple = (2.5, 5.0, 10.0, 20.0)   # escalate to get OUT


# ------------------------------------------------------------------ broker
class JupiterBroker:
    def __init__(self, keypair, solana: Solana, cfg: RealConfig, mode: str = "dry",
                 log=print):
        assert mode in ("dry", "real")
        self.kp = keypair
        self.owner = str(keypair.pubkey())
        self.sol = solana
        self.cfg = cfg
        self.mode = mode
        self.log = log
        self.fees_sol = 0.0
        self.swaps = 0
        self.kill_sol_price: float | None = None
        self.status = f"{'DRY-RUN' if mode == 'dry' else 'ECHT'} {self.owner[:4]}…{self.owner[-4:]}"

    impact_pct = staticmethod(lambda usd, liq: 100 * usd / (max(liq / 2, 1.0) + usd))

    # ------------------------------------------------------------- helpers
    def _quote(self, inp: str, out: str, amount: int, slippage_pct: float) -> dict:
        q = _http(f"{JUP}/quote?inputMint={inp}&outputMint={out}&amount={amount}"
                  f"&slippageBps={int(slippage_pct * 100)}")
        if not q or "outAmount" not in q:
            raise BrokerError(f"keine Route: {str(q)[:120]}")
        return q

    def _swap_tx(self, quote: dict) -> tuple[str, int]:
        from solders.transaction import VersionedTransaction
        res = _http(f"{JUP}/swap", {
            "quoteResponse": quote,
            "userPublicKey": self.owner,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "maxLamports": self.cfg.max_priority_lamports, "priorityLevel": "high"}},
        })
        if "swapTransaction" not in res:
            raise BrokerError(f"Jupiter swap: {str(res)[:120]}")
        tx = VersionedTransaction.from_bytes(base64.b64decode(res["swapTransaction"]))
        signed = VersionedTransaction(tx.message, [self.kp])
        return base64.b64encode(bytes(signed)).decode(), int(res.get("lastValidBlockHeight") or 0)

    def _simulate(self, tx_b64: str) -> None:
        res = self.sol.rpc("simulateTransaction", [tx_b64, {
            "encoding": "base64", "sigVerify": True, "commitment": "processed"}]) or {}
        err = (res.get("value") or {}).get("err")
        if err:
            logs = (res.get("value") or {}).get("logs") or []
            raise BrokerError(f"Simulation fehlgeschlagen: {err} {logs[-1:] if logs else ''}")

    def _send_and_confirm(self, tx_b64: str, last_valid: int) -> str:
        sig = self.sol.rpc("sendTransaction", [tx_b64, {"encoding": "base64",
                                                        "skipPreflight": False, "maxRetries": 0}])
        deadline = time.time() + self.cfg.confirm_timeout_s
        while time.time() < deadline:
            time.sleep(2)
            st = (self.sol.rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None]
            st = st[0]
            if st and st.get("err"):
                raise BrokerError(f"Transaktion fehlgeschlagen: {st['err']}")
            if st and st.get("confirmationStatus") in ("confirmed", "finalized"):
                return sig
            if last_valid and (self.sol.rpc("getBlockHeight", []) or 0) > last_valid:
                raise BrokerError("Transaktion abgelaufen (nicht bestätigt)")
            try:   # rebroadcast; the same signature can't execute twice
                self.sol.rpc("sendTransaction", [tx_b64, {"encoding": "base64",
                                                          "skipPreflight": True, "maxRetries": 0}])
            except BrokerError:
                pass
        raise BrokerError("Bestätigung hat zu lange gedauert")

    def _read_swap(self, sig: str):
        for _ in range(10):
            tx = self.sol.rpc("getTransaction", [sig, {"encoding": "jsonParsed",
                                                       "maxSupportedTransactionVersion": 0,
                                                       "commitment": "confirmed"}])
            if tx:
                fee = ((tx.get("meta") or {}).get("fee") or 0) / LAMPORTS
                self.fees_sol += fee
                sw = swap_from_tx(tx, self.owner)
                if sw is None:
                    raise BrokerError(f"Swap {sig[:12]}… nicht auswertbar - bitte im Explorer prüfen")
                return sw
            time.sleep(1.5)
        raise BrokerError(f"Transaktion {sig[:12]}… nicht lesbar - bitte im Explorer prüfen")

    def _close_account(self, account: str, program: str) -> None:
        """Get the ~0.002 SOL rent back after a token is fully sold (best effort)."""
        from solders.hash import Hash
        from solders.instruction import AccountMeta, Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.transaction import Transaction
        try:
            bh = self.sol.rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
            ix = Instruction(Pubkey.from_string(program), bytes([9]), [   # CloseAccount
                AccountMeta(Pubkey.from_string(account), False, True),
                AccountMeta(self.kp.pubkey(), False, True),
                AccountMeta(self.kp.pubkey(), True, False)])
            blockhash = Hash.from_string(bh["blockhash"])
            tx = Transaction([self.kp], Message.new_with_blockhash([ix], self.kp.pubkey(), blockhash),
                             blockhash)
            self._send_and_confirm(base64.b64encode(bytes(tx)).decode(),
                                   int(bh.get("lastValidBlockHeight") or 0))
        except BrokerError as e:
            self.log(f"Konto {account[:6]}… nicht geschlossen ({e}) - Miete bleibt gebunden")

    # ----------------------------------------------------------- interface
    def buy(self, usd: float, s: PairSnapshot) -> tuple[float, float, float]:
        """Return (tokens received, avg fill price in USD, USD actually spent)."""
        usd = min(usd, self.cfg.max_trade_usd)
        price = self.sol.sol_price()
        lamports = int(usd / price * LAMPORTS)
        free = self.sol.sol_balance(self.owner) - self.cfg.sol_reserve
        if lamports / LAMPORTS > free:
            raise BrokerError(f"zu wenig SOL: brauche {lamports / LAMPORTS:.4f}, frei {free:.4f}")
        quote = self._quote(SOL_MINT, s.mint, lamports, self.cfg.buy_slippage_pct)
        tx_b64, last_valid = self._swap_tx(quote)
        dec = self.sol.decimals(s.mint)
        if self.mode == "dry":
            self._simulate(tx_b64)
            tokens = int(quote["outAmount"]) / 10 ** dec
            self.swaps += 1
            return tokens, usd / tokens, usd
        sig = self._send_and_confirm(tx_b64, last_valid)
        sw = self._read_swap(sig)
        self.swaps += 1
        tokens = sw.tokens / 10 ** dec
        spent = -sw.sol * price
        self.log(f"Kauf bestätigt {sig}")
        return tokens, spent / tokens, spent

    def sell(self, tokens: float, s: PairSnapshot) -> tuple[float, float]:
        """Return (USD received, avg fill price). Escalates slippage to get out."""
        dec = self.sol.decimals(s.mint)
        raw = int(tokens * 10 ** dec)
        held = None
        if self.mode == "real":
            held = next((t for t in self.sol.token_balances(self.owner) if t["mint"] == s.mint), None)
            if not held or held["raw"] == 0:
                raise BrokerError("Token nicht (mehr) in der Wallet")
            if raw >= 0.995 * held["raw"]:
                raw = held["raw"]                       # sell everything, no dust
            raw = min(raw, held["raw"])
        return self._sell_raw(s.mint, raw, dec, held)

    def sell_leftover(self, t: dict) -> float:
        """Sell a whole token balance found in the wallet (from leftovers()). Returns USD."""
        usd, _ = self._sell_raw(t["mint"], t["raw"], t["decimals"], t)
        return usd

    def _sell_raw(self, mint: str, raw: int, dec: int, held: dict | None) -> tuple[float, float]:
        if raw <= 0:
            raise BrokerError("Verkaufsmenge 0")
        price = self.sol.sol_price()
        last_err = None
        for slip in self.cfg.sell_slippage_steps:
            try:
                quote = self._quote(mint, SOL_MINT, raw, slip)
                if self.mode == "dry":
                    usd = int(quote["outAmount"]) / LAMPORTS * price
                    self.swaps += 1
                    return usd, usd / (raw / 10 ** dec)
                tx_b64, last_valid = self._swap_tx(quote)
                sig = self._send_and_confirm(tx_b64, last_valid)
                sw = self._read_swap(sig)
                self.swaps += 1
                usd = sw.sol * price
                self.log(f"Verkauf bestätigt {sig} (Slippage-Limit {slip}%)")
                if held and raw == held["raw"]:
                    self._close_account(held["account"], held["program"])
                return usd, usd / (-sw.tokens / 10 ** dec)
            except BrokerError as e:
                last_err = e
                self.log(f"Verkauf mit {slip}% Slippage fehlgeschlagen: {e}")
        raise BrokerError(f"Verkauf fehlgeschlagen: {last_err}")

    # ------------------------------------------------------------- guards
    def wallet_usd(self, token_prices: dict[str, float]) -> float:
        """SOL + held tokens. SOL is valued at the price from the start of the night
        (kill_sol_price) so a falling SOL price does not count as a trading loss."""
        price = self.kill_sol_price or self.sol.sol_price()
        total = self.sol.sol_balance(self.owner) * price
        if self.mode == "real":
            for t in self.sol.token_balances(self.owner):
                if t["mint"] not in QUOTE_MINTS:
                    total += t["ui"] * token_prices.get(t["mint"], 0.0)
        return total

    def leftovers(self) -> list[dict]:
        return [t for t in self.sol.token_balances(self.owner)
                if t["raw"] > 0 and t["mint"] not in QUOTE_MINTS]
