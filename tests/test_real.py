"""Dry-run / real execution against a fake Jupiter + fake chain.

Transactions are real solders transactions, really signed with a test keypair;
only the network is simulated.
"""
import base64
import json
import os
import types
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest

pytest.importorskip("solders")
from solders.hash import Hash  # noqa: E402
from solders.instruction import AccountMeta, Instruction  # noqa: E402
from solders.keypair import Keypair  # noqa: E402
from solders.message import MessageV0  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402
from solders.signature import Signature  # noqa: E402
from solders.transaction import VersionedTransaction  # noqa: E402

from grokbot import realmode, wallet  # noqa: E402
from grokbot.agents import EquityTracker, Executor, TradeLogger  # noqa: E402
from grokbot.brokers import jupiter  # noqa: E402
from grokbot.brokers.jupiter import (SOL_MINT, USDC_MINT, JupiterBroker,  # noqa: E402
                                     RealConfig, Solana)
from grokbot.config import BotConfig  # noqa: E402
from grokbot.feeds import helius  # noqa: E402
from grokbot.feeds.helius import RpcError  # noqa: E402
from grokbot.models import BrokerError, Signal  # noqa: E402
from tests.test_agents import NOW, snap  # noqa: E402

SOL_USD = 200.0
TOKEN_USD = 0.001
MINT = "Meme111111111111111111111111111111111111111"
FEE = 5000


class FakeNet:
    def __init__(self, kp, sol=0.5):
        self.kp = kp
        self.owner = str(kp.pubkey())
        self.lamports = int(sol * 1e9)
        self.tokens = {}                 # mint -> raw
        self.pending = {}                # message bytes -> quote
        self.txs = {}                    # signature -> getTransaction result
        self.sell_fail_below_bps = 0     # preflight fails for sells with less slippage
        self.sim_err = None
        self.calls = []
        self.closed = []
        self.n = 0

    # ---------------------------------------------------------- jupiter
    def http(self, url, payload=None, timeout=15.0):
        if payload is None:                                   # quote
            q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
            self.calls.append(("quote", q["inputMint"][:4], int(q["slippageBps"])))
            amt = int(q["amount"])
            if q["inputMint"] == SOL_MINT and q["outputMint"] == USDC_MINT:
                out = amt / 1e9 * SOL_USD * 1e6
            elif q["inputMint"] == SOL_MINT:
                out = amt / 1e9 * SOL_USD / TOKEN_USD * 1e6
            else:
                out = amt / 1e6 * TOKEN_USD / SOL_USD * 1e9
            return {**q, "inAmount": str(amt), "outAmount": str(int(out))}
        quote = payload["quoteResponse"]                      # swap
        assert payload["userPublicKey"] == self.owner
        self.n += 1
        ix = Instruction(Pubkey.from_string("11111111111111111111111111111111"),
                         bytes([2, 0, 0, 0]) + self.n.to_bytes(8, "little"),
                         [AccountMeta(self.kp.pubkey(), True, True),
                          AccountMeta(Pubkey.default(), False, True)])
        msg = MessageV0.try_compile(self.kp.pubkey(), [ix], [], Hash.default())
        self.pending[bytes(msg)] = quote
        unsigned = VersionedTransaction.populate(msg, [Signature.default()])
        return {"swapTransaction": base64.b64encode(bytes(unsigned)).decode(),
                "lastValidBlockHeight": 1000}

    # -------------------------------------------------------------- rpc
    def rpc(self, url, payload, timeout=10.0):
        m, p = payload["method"], payload["params"]
        self.calls.append((m,))
        if m == "getBalance":
            return {"value": self.lamports}
        if m == "getTokenAccountsByOwner":
            if p[1]["programId"] != jupiter.TOKEN_PROGRAMS[0]:
                return {"value": []}
            return {"value": [{"pubkey": "Acct" + mint[:40], "account": {"data": {"parsed": {"info": {
                "mint": mint, "tokenAmount": {"amount": str(raw), "decimals": 6,
                                              "uiAmount": raw / 1e6}}}}}}
                for mint, raw in self.tokens.items()]}
        if m == "getAccountInfo":
            return {"value": {"data": {"parsed": {"info": {"decimals": 6}}}}}
        if m == "simulateTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
            assert tx.signatures[0] != Signature.default(), "not signed"
            return {"value": {"err": self.sim_err, "logs": ["log"]}}
        if m == "sendTransaction":
            tx = VersionedTransaction.from_bytes(base64.b64decode(p[0]))
            sig = str(tx.signatures[0])
            if bytes(tx.message) not in self.pending:          # close-account tx
                keys = [str(k) for k in tx.message.account_keys]
                self.closed += [k for k in keys if k.startswith("Acct")]
                self.txs[sig] = {"meta": {"err": None, "fee": FEE}}
                return sig
            if sig in self.txs:                                # rebroadcast
                return sig
            quote = self.pending[bytes(tx.message)]
            selling = quote["inputMint"] != SOL_MINT
            if selling and int(quote["slippageBps"]) < self.sell_fail_below_bps and not p[1]["skipPreflight"]:
                raise RpcError("Slippage tolerance exceeded")
            self._execute(sig, quote)
            return sig
        if m == "getSignatureStatuses":
            return {"value": [{"confirmationStatus": "confirmed", "err": None}]}
        if m == "getBlockHeight":
            return 1
        if m == "getTransaction":
            return self.txs.get(p[0])
        if m == "getLatestBlockhash":
            return {"value": {"blockhash": str(Hash.default()), "lastValidBlockHeight": 1000}}
        raise AssertionError(m)

    def _execute(self, sig, quote):
        mint = quote["outputMint"] if quote["inputMint"] == SOL_MINT else quote["inputMint"]
        pre_sol, pre_tok = self.lamports, self.tokens.get(mint, 0)
        if quote["inputMint"] == SOL_MINT:
            self.lamports -= int(quote["inAmount"]) + FEE
            self.tokens[mint] = pre_tok + int(quote["outAmount"])
        else:
            self.lamports += int(quote["outAmount"]) - FEE
            self.tokens[mint] = pre_tok - int(quote["inAmount"])
        bal = lambda r: [{"owner": self.owner, "mint": mint, "uiTokenAmount": {"amount": str(r)}}]
        self.txs[sig] = {"blockTime": 0, "transaction": {"message": {"accountKeys": [{"pubkey": self.owner}]}},
                         "meta": {"err": None, "fee": FEE,
                                  "preBalances": [pre_sol], "postBalances": [self.lamports],
                                  "preTokenBalances": bal(pre_tok), "postTokenBalances": bal(self.tokens[mint])}}


@pytest.fixture
def net(monkeypatch):
    kp = Keypair()
    n = FakeNet(kp)
    monkeypatch.setattr(jupiter, "_http", n.http)
    monkeypatch.setattr(helius, "_post", n.rpc)
    monkeypatch.setattr(jupiter.time, "sleep", lambda s: None)
    return n


def broker(net, mode, **cfg):
    return JupiterBroker(net.kp, Solana("http://rpc"), RealConfig(**cfg), mode, log=lambda m: None)


def s_(**kw):
    kw.setdefault("price", TOKEN_USD)
    return snap(mint=MINT, **kw)


# ------------------------------------------------------------------ broker
def test_dry_run_buy_simulates_and_sends_nothing(net):
    b = broker(net, "dry")
    tokens, fill, spent = b.buy(20, s_())                # capped to max_trade 5
    assert spent == 5 and tokens == pytest.approx(5 / TOKEN_USD, rel=1e-6)
    assert ("simulateTransaction",) in net.calls and ("sendTransaction",) not in net.calls
    assert net.lamports == int(0.5e9) and not net.tokens
    usd, _ = b.sell(tokens, s_())                       # quote only
    assert usd == pytest.approx(5, rel=1e-3) and ("sendTransaction",) not in net.calls


def test_dry_run_simulation_failure(net):
    net.sim_err = {"InstructionError": [0, "Custom"]}
    with pytest.raises(BrokerError, match="Simulation"):
        broker(net, "dry").buy(5, s_())


def test_real_buy_books_actual_chain_amounts(net):
    b = broker(net, "real")
    tokens, fill, spent = b.buy(5, s_())
    assert net.tokens[MINT] == int(tokens * 1e6)
    assert spent == pytest.approx(5 + FEE / 1e9 * SOL_USD, rel=1e-6)   # network fee included
    assert b.fees_sol == pytest.approx(FEE / 1e9)


def test_real_buy_refuses_without_enough_sol(net):
    net.lamports = int(0.03e9)                           # 0.02 reserve -> only ~$2 free
    with pytest.raises(BrokerError, match="zu wenig SOL"):
        broker(net, "real").buy(5, s_())
    assert not [c for c in net.calls if c[0] == "quote" and c[2] != 50]   # only the SOL price lookup


def test_real_sell_escalates_slippage_and_closes_account(net):
    b = broker(net, "real")
    tokens, _, _ = b.buy(5, s_())
    net.sell_fail_below_bps = 1000                       # 2.5 % and 5 % fail, 10 % works
    usd, _ = b.sell(tokens * 0.999, s_())                # >= 99.5 % -> sells everything
    assert net.tokens[MINT] == 0
    sell_slips = [c[2] for c in net.calls if c[0] == "quote" and c[1] == MINT[:4]]
    assert sell_slips == [250, 500, 1000]
    assert usd == pytest.approx(5 - FEE / 1e9 * SOL_USD, rel=1e-3)
    assert net.closed == ["Acct" + MINT[:40]]


def test_real_sell_gives_up_after_all_steps(net):
    b = broker(net, "real")
    tokens, _, _ = b.buy(5, s_())
    net.sell_fail_below_bps = 99999
    with pytest.raises(BrokerError, match="Verkauf fehlgeschlagen"):
        b.sell(tokens, s_())
    assert net.tokens[MINT] > 0


# ---------------------------------------------------------------- executor
def test_executor_with_real_broker_round_trip(net):
    cfg = BotConfig().executor
    cfg.entry_parts = 1
    eq = EquityTracker(15)
    ex = Executor(cfg, eq, TradeLogger(echo=False), broker(net, "real"))
    pos = ex.open(Signal(s_(), ["a", "b"]), 5, NOW)
    assert pos and eq.cash == pytest.approx(15 - 5 - FEE / 1e9 * SOL_USD)
    closed = ex.manage(pos, s_(price=TOKEN_USD * 0.8), NOW + timedelta(minutes=1))
    assert closed and "hard stop" in closed.reason and net.tokens[MINT] == 0


class FailingBroker:
    impact_pct = staticmethod(lambda usd, liq: 0.1)

    def __init__(self):
        self.fail = False

    def buy(self, usd, s):
        return usd / s.price, s.price, usd

    def sell(self, tokens, s):
        if self.fail:
            raise BrokerError("no liquidity")
        return tokens * s.price, s.price


def test_failed_sell_keeps_position_then_writes_off():
    b = FailingBroker()
    cfg = BotConfig().executor
    cfg.entry_parts = 1
    ex = Executor(cfg, EquityTracker(15), TradeLogger(echo=False), b)
    pos = ex.open(Signal(snap(), ["a", "b"]), 5, NOW)
    b.fail = True
    for i in range(Executor.MAX_SELL_FAILURES - 1):
        assert ex.manage(pos, snap(lp_pulled=True), NOW + timedelta(seconds=30 * (i + 1))) is None
        assert pos.mint in ex.positions                 # still tracked, retried
    closed = ex.manage(pos, snap(lp_pulled=True), NOW + timedelta(minutes=10))
    assert closed and "abgeschrieben" in closed.reason and closed.returned_usd == 0
    assert pos.mint not in ex.positions


def test_tp_not_counted_when_sell_fails():
    b = FailingBroker()
    cfg = BotConfig().executor
    cfg.entry_parts = 1
    ex = Executor(cfg, EquityTracker(15), TradeLogger(echo=False), b)
    pos = ex.open(Signal(snap(), ["a", "b"]), 5, NOW)
    b.fail = True
    ex.manage(pos, snap(price=2.5), NOW + timedelta(minutes=1))
    assert pos.tp_hits == 0
    b.fail = False
    ex.manage(pos, snap(price=2.5), NOW + timedelta(minutes=2))
    assert pos.tp_hits == 1


# ------------------------------------------------------------- kill switch
def test_kill_switch_sells_and_stops():
    from grokbot.orchestrator import Orchestrator
    from tests.test_agents import OneCoinFeed
    b = FailingBroker()
    b.wallet = 15.0
    b.wallet_usd = lambda prices: b.wallet
    b.status = "ECHT"
    o = Orchestrator(BotConfig(start_balance=15), OneCoinFeed(snap()), echo=False,
                     broker=b, kill=(15.0, 6.0))
    o.tick(NOW)
    assert o.executor.positions and not o.stopped
    b.wallet = 8.0                                     # lost $7 > $6 limit
    for i in range(10):
        o.tick(NOW + timedelta(seconds=30 * (i + 1)))
    assert o.stopped and not o.executor.positions
    assert any(line["kind"] == "KILL" for line in o.log.lines)


# ------------------------------------------------------------------ setup
def args(**kw):
    base = dict(real=True, dry_run=False, budget=15.0, max_trade=5.0, kill_pct=40.0, wallet=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def wallet_file(net, tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    wallet.save(net.kp, p)
    monkeypatch.setenv("GROKBOT_WALLET", str(p))
    monkeypatch.setenv("HELIUS_API_KEY", "KEY")
    return p


def test_setup_requires_typed_JA(net, wallet_file):
    out = []
    with pytest.raises(SystemExit, match="Abgebrochen"):
        realmode.setup(args(), BotConfig(), input_fn=lambda q: "ja", print_fn=out.append)
    broker_, kill, label = realmode.setup(args(), cfg := BotConfig(),
                                          input_fn=lambda q: "JA", print_fn=out.append)
    assert label == "ECHTGELD" and cfg.start_balance == 15 and cfg.executor.entry_parts == 1
    assert kill == (pytest.approx(0.5 * SOL_USD), pytest.approx(6.0))


def test_setup_caps_budget_and_sells_leftovers(net, wallet_file):
    net.lamports = int(0.06e9)                          # $12 - reserve $4 = $8 usable
    net.tokens[MINT] = 1_000_000_000                     # 1000 tokens left from a crash
    answers = iter(["j", "JA"])
    out = []
    _, _, _ = realmode.setup(args(), cfg := BotConfig(), input_fn=lambda q: next(answers),
                             print_fn=out.append)
    assert net.tokens[MINT] == 0
    assert cfg.start_balance == pytest.approx((net.lamports / 1e9 - 0.02) * SOL_USD, rel=1e-6)
    assert any("reduziert" in line for line in out)


def test_setup_dry_run_needs_no_confirmation(net, wallet_file):
    broker_, kill, label = realmode.setup(args(real=False, dry_run=True), BotConfig(),
                                          input_fn=lambda q: pytest.fail("asked"), print_fn=lambda m: None)
    assert label == "DRY-RUN" and kill is None and broker_.mode == "dry"


def test_setup_refuses_empty_wallet(net, wallet_file):
    net.lamports = int(0.025e9)
    with pytest.raises(SystemExit, match="Zu wenig Guthaben"):
        realmode.setup(args(), BotConfig(), input_fn=lambda q: "JA", print_fn=lambda m: None)


# ------------------------------------------------------------------ wallet
def test_wallet_file_is_private_and_loadable(tmp_path):
    kp = Keypair()
    p = tmp_path / "sub" / "wallet.json"
    wallet.save(kp, p)
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert wallet.load(p).pubkey() == kp.pubkey()
    with pytest.raises(FileExistsError):
        wallet.save(Keypair(), p)                        # never overwrite
    os.chmod(p, 0o644)
    with pytest.raises(SystemExit, match="chmod 600"):
        wallet.load(p)


def test_parse_secret_formats():
    kp = Keypair()
    assert wallet.parse_secret(str(kp)).pubkey() == kp.pubkey()
    assert wallet.parse_secret(json.dumps(list(bytes(kp)))).pubkey() == kp.pubkey()


def test_kill_switch_ignores_sol_price_moves(net, wallet_file):
    b, kill, _ = realmode.setup(args(), BotConfig(), input_fn=lambda q: "JA", print_fn=lambda m: None)
    start_usd, max_loss = kill
    global SOL_USD
    old, SOL_USD = SOL_USD, SOL_USD * 0.7          # SOL -30 % overnight
    b.sol._sol_price = None
    try:
        assert start_usd - b.wallet_usd({}) < 0.01  # no trading loss -> no kill
    finally:
        SOL_USD = old
