"""CLI: python -m grokbot --mode sim | live"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    ap.add_argument("--helius-poll", type=int, default=60,
                    help="live: seconds between Helius wallet polls (each poll = 1 call per wallet)")
    args = ap.parse_args(argv)

    cfg = BotConfig(start_balance=args.balance, session_start=args.start, session_end=args.end)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / f"{args.mode}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    dash = out / "dashboard.html"

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

    bot = Orchestrator(cfg, feed, log_path=out / "night.log.jsonl", echo=not args.quiet)
    print(f"Grok Bot [{args.mode}] PAPER TRADING  {start:%Y-%m-%d %H:%M} -> {end:%H:%M}  "
          f"start ${cfg.start_balance:.2f}\nOutput: {out}/  (open dashboard.html)")

    t = start
    i = 0
    if args.mode == "live":
        while datetime.now() < start:
            time.sleep(min(30, (start - datetime.now()).total_seconds()))
    while t < end:
        bot.tick(t)
        i += 1
        if args.mode == "sim":
            if i % 120 == 0:  # every simulated hour
                print(terminal(bot, args.mode))
                write_html(bot, dash, args.mode)
            t += timedelta(seconds=cfg.tick_seconds)
        else:
            print(terminal(bot, args.mode))
            write_html(bot, dash, args.mode)
            time.sleep(cfg.tick_seconds)
            t = datetime.now()

    bot.close_all(end)
    recap = bot.recap()
    (out / "recap.json").write_text(json.dumps(recap, indent=2))
    write_html(bot, dash, args.mode, recap)
    print(terminal(bot, args.mode))
    print("\n===== RECAP 07:00 =====")
    print(f"Start ${recap['start_balance']:.2f} -> End ${recap['end_balance']:.2f} "
          f"({recap['return_pct']:+.1f}%)")
    print(f"Trades {recap['total_trades']}  W/L {recap['wins']}/{recap['losses']}  "
          f"win rate {recap['win_rate_pct']}%  max DD {recap['max_drawdown_pct']}%")
    print(f"Best  {recap['best_trade']}\nWorst {recap['worst_trade']}")
    print(f"Top 3 coins {recap['top3_coins']}")
    if recap["paused"]:
        print(f"Paused: {recap['paused']}")
    return recap


if __name__ == "__main__":
    main()
