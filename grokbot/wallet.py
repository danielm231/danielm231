"""Bot wallet: create, import and show the dedicated trading wallet.

    python -m grokbot.wallet new       # create a fresh wallet (recommended)
    python -m grokbot.wallet import    # import a secret key (hidden input)
    python -m grokbot.wallet show      # address + balance

The secret key is stored in ~/.grokbot/wallet.json (Solana CLI format, file mode
600). It never leaves this machine, is never printed and never goes into the repo.
Use a wallet ONLY for the bot and only fund it with money you can lose.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
from pathlib import Path

DEFAULT_PATH = Path.home() / ".grokbot" / "wallet.json"


def _solders():
    try:
        from solders.keypair import Keypair
        return Keypair
    except ImportError:
        raise SystemExit("Für echtes Trading fehlt die Bibliothek 'solders'.\n"
                         "Installieren mit:  python3 -m pip install -r requirements-real.txt")


def wallet_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("GROKBOT_WALLET") or DEFAULT_PATH).expanduser()


def save(kp, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(list(bytes(kp)), f)


def load(path: str | Path | None = None):
    """Load the keypair; refuses files other users could read."""
    Keypair = _solders()
    p = wallet_path(path)
    if not p.exists():
        raise SystemExit(f"Keine Bot-Wallet unter {p}. Anlegen mit:  python3 -m grokbot.wallet new")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise SystemExit(f"{p} ist für andere lesbar (Rechte {oct(mode)}). "
                         f"Absichern mit:  chmod 600 '{p}'")
    data = json.loads(p.read_text())
    return Keypair.from_bytes(bytes(data))


def parse_secret(text: str):
    """Accept a base58 secret (Phantom/Solflare export) or a JSON byte array."""
    Keypair = _solders()
    text = text.strip()
    if text.startswith("["):
        return Keypair.from_bytes(bytes(json.loads(text)))
    return Keypair.from_base58_string(text)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="Grok Bot wallet")
    ap.add_argument("cmd", choices=["new", "import", "show"])
    ap.add_argument("--path", default=None, help=f"wallet file (default {DEFAULT_PATH})")
    args = ap.parse_args(argv)
    p = wallet_path(args.path)

    if args.cmd in ("new", "import"):
        if p.exists():
            raise SystemExit(f"{p} existiert schon - wird nicht überschrieben.\n"
                             f"Adresse ansehen:  python3 -m grokbot.wallet show")
        if args.cmd == "new":
            kp = _solders()()
        else:
            secret = getpass.getpass("Secret Key einfügen (Eingabe bleibt unsichtbar): ")
            try:
                kp = parse_secret(secret)
            except Exception:
                raise SystemExit("Das sieht nicht nach einem gültigen Solana Secret Key aus.")
        save(kp, p)
        print(f"Bot-Wallet gespeichert: {p} (nur für dich lesbar)")
        print(f"Adresse: {kp.pubkey()}")
        print("\nSchicke an diese Adresse nur, was du verlieren kannst, plus ~0.05 SOL für Gebühren.")
        print("Backup: Kopiere die Datei an einen sicheren Ort. Ohne sie ist das Guthaben weg.")
        return

    kp = load(p)
    print(f"Adresse: {kp.pubkey()}")
    try:
        from .brokers.jupiter import Solana
        sol = Solana.from_env()
        bal = sol.sol_balance(str(kp.pubkey()))
        price = sol.sol_price()
        print(f"Guthaben: {bal:.4f} SOL (~${bal * price:.2f})")
        for t in sol.token_balances(str(kp.pubkey())):
            print(f"  Token {t['mint']}: {t['ui']:g}")
    except SystemExit as e:
        print(f"(Guthaben nicht abrufbar: {e})", file=sys.stderr)
    except Exception as e:  # network etc.
        print(f"(Guthaben nicht abrufbar: {type(e).__name__})", file=sys.stderr)


if __name__ == "__main__":
    main()
