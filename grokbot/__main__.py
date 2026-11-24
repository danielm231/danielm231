"""CLI: python -m grokbot --mode sim | live"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import server
from .config import BotConfig
from .dashboard import terminal, write_html
from .feeds import LiveFeed, SimulatedFeed
from .feeds.helius import HeliusWalletTracker, load_wallets
from .orchestrator import Orchestrator, session_window


def main(argv=None) -> dict:
    ap = argparse.ArgumentParser(description="Grok Bot - overnight meme-coin PAPER trader")
    ap.add_argument("--mode", choices=["sim", "live"], default="sim",
                    help="sim: synthetic market, runs the whole night in seconds. "
                         "live: real DexScreener/RugCheck data in real time, paper fills.")
    ap.add_argument("--balance", type=float, default=70.0)
    ap.add_argument("--start", default="23:00")
    ap.add_argument("--end", default="07:00")
    ap.add_argument("--seed", type=int, default=None, help="sim: random seed")
    ap.add_argument("--now", action="store_true", help="live: start immediately instead of waiting for --start")
    ap.add_argument("--out", default="runs", help="output folder for log, dashboard, recap")
    ap.add_argument("--quiet", action="store_true", help="don't echo log lines")
    ap.add_argument("--smart-wallets", default="wallets/smart_money.txt",
                    help="live: file with smart-money wallet addresses (Helius tracker)")
    ap.add_argument("--kol-wallets", default="wallets/kol.txt",
                    help="live: file with KOL wallet addresses (Helius tracker)")
    ap.add_argument("--serve", nargs="?", const=8000, type=int, metavar="PORT",
                    help="start the built-in dashboard web server (default port 8000) "
                         "to watch from your phone; keeps serving after 07:00 until Ctrl+C")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address for --serve (0.0.0.0 = whole network, 127.0.0.1 = this machine only)")
    money = ap.add_mutually_exclusive_group()
    money.add_argument("--dry-run", action="store_true",
                       help="real Jupiter transactions with your bot wallet are built and SIMULATED, "
                            "nothing is sent (implies --mode live)")
    money.add_argument("--real", action="store_true",
                       help="REAL MONEY trading with your bot wallet (implies --mode live)")
    ap.add_argument("--budget", type=float, default=15.0, help="real/dry-run: total USD the bot may use")
    ap.add_argument("--max-trade", type=float, default=5.0, help="real/dry-run: hard cap per trade in USD")
    ap.add_argument("--kill-pct", type=float, default=40.0,
                    help="real: sell all and stop when the wallet lost this %% of the budget")
    ap.add_argument("--wallet", default=None, help="bot wallet file (default ~/.grokbot/wallet.json)")
    ap.add_argument("--helius-poll", type=int, default=60,
                    help="live: seconds between Helius wallet polls (each poll = 1 call per wallet)")
    args = ap.parse_args(argv)
    if args.dry_run or args.real:
        args.mode = "live"

    cfg = BotConfig(start_balance=args.balance, session_start=args.start, session_end=args.end)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / f"{args.mode}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    dash = out / "dashboard.html"
    srv = None
    if args.serve:
        srv = server.start(out, port=args.serve, host=args.host)
        print("Dashboard im Browser (Handy im selben WLAN):")
        for u in server.urls(srv):
            print(f"  {u}")

    if args.mode == "sim":
        start, end = session_window(cfg, datetime.now())
        feed = SimulatedFeed(start, seed=args.seed, tick_seconds=cfg.tick_seconds)
    else:
        now = datetime.now()
        start, end = session_window(cfg, now)
        if now < end - timedelta(days=1):   # e.g. 02:00 -> still inside last night's session
            start, end = start - timedelta(days=1), end - timedelta(days=1)
        if args.now:
            start = now
        tracker = HeliusWalletTracker(load_wallets(args.smart_wallets),
                                      load_wallets(args.kol_wallets),
                                      window_min=cfg.scanner.smart_money_window_min,
                                      poll_seconds=args.helius_poll)
        cfg.scanner.tracked_wallets = sorted(tracker.smart)
        cfg.scanner.kol_wallets = sorted(tracker.kol)
        print(f"Helius tracker: {tracker.status}")
        feed = LiveFeed(wallet_activity=tracker if tracker.enabled else None)
        feed.tracker = tracker

    broker, kill, label = None, None, "PAPER TRADING"
    if args.dry_run or args.real:
        from .realmode import setup
        broker, kill, label = setup(args, cfg)
        mode_label = f"live {label}"
    else:
        mode_label = args.mode

    bot = Orchestrator(cfg, feed, log_path=out / "night.log.jsonl", echo=not args.quiet,
                       broker=broker, kill=kill)
    print(f"Grok Bot [{args.mode}] {label}  {start:%Y-%m-%d %H:%M} -> {end:%H:%M}  "
          f"start ${cfg.start_balance:.2f}\nOutput: {out}/  (open dashboard.html)")

    t = start
    i = 0
    if args.mode == "live":
        while datetime.now() < start:
            time.sleep(min(30, (start - datetime.now()).total_seconds()))
    errors = 0
    while t < end and not bot.stopped:
        try:
            bot.tick(t)
            errors = 0
        except Exception as e:  # never die with open positions
            if args.mode == "sim":
                raise
            errors += 1
            bot.log.log(datetime.now(), "ERROR", "-", f"Tick-Fehler {errors}/5: {type(e).__name__}: {e}")
            if errors >= 5:
                bot.log.log(datetime.now(), "KILL", "-", "5 Fehler in Folge - verkaufe alles und stoppe")
                bot.close_all(datetime.now(), "Notstopp nach Fehlern")
                bot.stopped = True
        i += 1
        if args.mode == "sim":
            if i % 120 == 0:  # every simulated hour
                print(terminal(bot, mode_label))
                write_html(bot, dash, mode_label)
            t += timedelta(seconds=cfg.tick_seconds)
        else:
            print(terminal(bot, mode_label))
            write_html(bot, dash, mode_label)
            time.sleep(cfg.tick_seconds)
            t = datetime.now()

    bot.close_all(end if args.mode == "sim" else datetime.now())
    recap = bot.recap()
    if broker is not None:
        recap["execution"] = label
        recap["swaps"] = broker.swaps
        recap["network_fees_sol"] = round(broker.fees_sol, 6)
    (out / "recap.json").write_text(json.dumps(recap, indent=2))
    write_html(bot, dash, mode_label, recap)
    print(terminal(bot, mode_label))
    print("\n===== RECAP 07:00 =====")
    print(f"Start ${recap['start_balance']:.2f} -> End ${recap['end_balance']:.2f} "
          f"({recap['return_pct']:+.1f}%)")
    print(f"Trades {recap['total_trades']}  W/L {recap['wins']}/{recap['losses']}  "
          f"win rate {recap['win_rate_pct']}%  max DD {recap['max_drawdown_pct']}%")
    print(f"Best  {recap['best_trade']}\nWorst {recap['worst_trade']}")
    print(f"Top 3 coins {recap['top3_coins']}")
    if recap["paused"]:
        print(f"Paused: {recap['paused']}")
    if srv:
        print("\nDashboard läuft weiter unter " + server.urls(srv)[0] + "  (beenden mit Ctrl+C)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            srv.shutdown()
    return recap


if __name__ == "__main__":
    main()
