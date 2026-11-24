"""Set-up for --dry-run and --real: wallet, guards, leftovers, confirmation."""
from __future__ import annotations

from .config import BotConfig


def setup(args, cfg: BotConfig, input_fn=input, print_fn=print):
    """Return (broker, kill, label). Raises SystemExit if anything is not safe to run."""
    from . import wallet
    from .brokers.jupiter import JupiterBroker, RealConfig, Solana
    from .models import BrokerError

    mode = "real" if args.real else "dry"
    label = "ECHTGELD" if mode == "real" else "DRY-RUN"
    kp = wallet.load(args.wallet)
    sol = Solana.from_env()
    rc = RealConfig(budget_usd=args.budget, max_trade_usd=args.max_trade, kill_pct=args.kill_pct)
    broker = JupiterBroker(kp, sol, rc, mode, log=lambda m: print_fn(f"  [broker] {m}"))
    owner = broker.owner

    try:
        price = sol.sol_price()
        bal = sol.sol_balance(owner)
        leftovers = broker.leftovers()
    except BrokerError as e:
        raise SystemExit(f"Wallet/Preis nicht abrufbar: {e}")

    print_fn("=" * 60)
    print_fn(f"  {label}")
    print_fn("=" * 60)
    print_fn(f"  Wallet:   {owner}")
    print_fn(f"  Guthaben: {bal:.4f} SOL (~${bal * price:.2f}, SOL = ${price:.2f})")

    # leftovers from an earlier run / crash
    if leftovers:
        print_fn(f"  Restbestände in der Wallet: {len(leftovers)} Token")
        for t in leftovers:
            print_fn(f"    {t['mint']}  {t['ui']:g}")
        if mode == "real":
            if input_fn("  Diese Token jetzt verkaufen? (j/n): ").strip().lower() in ("j", "ja", "y"):
                for t in leftovers:
                    try:
                        usd = broker.sell_leftover(t)
                        print_fn(f"    verkauft {t['mint'][:8]}… für ${usd:.2f}")
                    except BrokerError as e:
                        print_fn(f"    {t['mint'][:8]}… nicht verkaufbar: {e}")
                bal = sol.sol_balance(owner)
        else:
            print_fn("  (Dry-Run: Restbestände werden nicht angefasst)")

    usable = max(0.0, (bal - rc.sol_reserve) * price)
    budget = min(rc.budget_usd, usable)
    if mode == "real" and budget < rc.budget_usd:
        print_fn(f"  Budget auf ${budget:.2f} reduziert (Guthaben minus {rc.sol_reserve} SOL Reserve)")
    if budget < 2.0:
        raise SystemExit(f"Zu wenig Guthaben: nutzbar ${usable:.2f}. Schicke SOL an {owner}.")
    rc.budget_usd = budget
    cfg.start_balance = budget
    cfg.executor.entry_parts = 1          # one entry: half the fees on small tickets

    max_loss = rc.kill_pct / 100 * budget
    print_fn(f"  Budget:   ${budget:.2f}   max. pro Trade: ${rc.max_trade_usd:.2f}")
    print_fn(f"  Kill-Switch: alles verkaufen + stoppen bei ${max_loss:.2f} Verlust")
    print_fn(f"  Reserve:  {rc.sol_reserve} SOL für Gebühren   Priority-Fee max "
             f"{rc.max_priority_lamports / 1e9:.4f} SOL/Swap")
    print_fn("=" * 60)

    if mode == "real":
        print_fn("  Der Bot handelt jetzt mit ECHTEM Geld. Verluste sind möglich, bis zum ganzen Budget.")
        if input_fn("  Zum Starten 'JA' eintippen: ").strip() != "JA":
            raise SystemExit("Abgebrochen - kein Trade ausgeführt.")
        kill = (bal * price, max_loss)
        broker.kill_sol_price = price      # SOL price moves are not trading losses
    else:
        print_fn("  Dry-Run: Transaktionen werden gebaut und simuliert, aber NICHT gesendet.")
        kill = None
    return broker, kill, label
