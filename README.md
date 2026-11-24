# Grok Bot – Overnight Meme-Coin Trader (Multi-Agent, Paper Trading)

Nachbau des „Grok Bot“-Prompts als echtes Programm: 6 Agents handeln über Nacht
(23:00 → 07:00) frische Solana-Meme-Coins mit $70 Startkapital, loggen jede Entscheidung
und drucken morgens ein Recap.

> **Es wird nur auf Papier gehandelt.** Es gibt keine Wallet, keine Private Keys, keine echten
> Swaps. Erst wenn der Bot über viele Nächte mit Live-Daten überzeugt, lohnt es sich,
> über echte Ausführung nachzudenken. Meme-Coins sind extrem riskant: die meisten gehen auf null.

## Start

Keine Abhängigkeiten, nur Python ≥ 3.10.

```bash
# komplette Nacht im simulierten Markt (dauert ~4 Sekunden)
python -m grokbot --mode sim --seed 1

# echte Marktdaten (DexScreener + RugCheck + Jupiter), Paper-Fills, wartet bis 23:00
python -m grokbot --mode live

# sofort starten statt auf 23:00 zu warten
python -m grokbot --mode live --now
```

Optionen: `--balance 70 --start 23:00 --end 07:00 --out runs --quiet`

Jeder Lauf schreibt nach `runs/<mode>-<zeit>/`:

| Datei | Inhalt |
|---|---|
| `dashboard.html` | Live-Dashboard (aktualisiert sich alle 10 s): Balance, Trades, W/L, offene Position, Agent-Status, Equity-Kurve, komplettes Nacht-Log |
| `night.log.jsonl` | jede ENTER / PARTIAL / EXIT / FILTERED / PAUSE Zeile mit Grund, Größe, Fill-Preis, PnL |
| `recap.json` | Start vs. End, bester / schlechtester Trade, Anzahl Trades, Win-Rate, Top-3-Coins, max. Drawdown |

## Die 6 Agents

| # | Agent | Datei | Aufgabe |
|---|---|---|---|
| 1 | SCANNER | `grokbot/agents/scanner.py` | Universe-Filter (≤ 6 h alt, Liq ≥ $40k, 5m-Vol ≥ $25k, ≥ 300 Holder, kein Freeze/Mint, LP gesperrt) und Signale: Volumen-Spike > 5× 1h-Schnitt, Buy-Ratio > 70 %, Holder-Wachstum, Smart Money (3+ Wallets in 10 min), KOL-Kauf. Handelt nie. |
| 2 | RUG CHECK | `grokbot/agents/rugcheck.py` | Dev < 5 %, Top 10 < 35 %, kein Bundle-Launch, LP burned oder ≥ 30 Tage gelockt, Sell-Simulation (Honeypot). Ohne PASS kein Trade. |
| 3 | SIZER | `grokbot/agents/sizer.py` | 20–40 % der aktuellen Equity (2 Signale → 20 %, 3 → 30 %, 4+ → 40 %), max. 2 offene Positionen, nach 2 Verlusten in Folge halbe Größe. Compounding automatisch. |
| 4 | EXECUTOR | `grokbot/agents/executor.py` | Max. 2,5 % Slippage, Einstieg in 2 Teilen, TP-Leiter 40 % bei 2×, 30 % bei 4×, Rest mit 25 % Trailing-Stop. Hard-Stop −12 %, Time-Stop 25 min, sofortiger Exit bei Dev-Sell oder LP-Pull. |
| 5 | LOGGER | `grokbot/agents/logger.py` | Jede Zeile mit Grund, Größe, Fill-Preis, PnL. |
| 6 | EQUITY | `grokbot/agents/equity.py` | Balance + Equity-Kurve bei jedem Close. |

Der **Orchestrator** (`grokbot/orchestrator.py`) setzt die Regeln durch: Einstieg nur bei
≥ 2 Signalen + Rug-Check-PASS, kein Chasen (+300 % in 1 h ohne ≥ 20 % Pullback),
Pause bei −30 % Drawdown vom Peak, Compounding.

Alle Zahlen stehen in `grokbot/config.py` und lassen sich dort ändern.

Hinweis: Im Original-Screenshot war die TP-Leiter abgeschnitten („sell 40% … 30% at 4x“).
Ich habe 40 % bei **2×** angenommen.

## Datenquellen

- **sim** – synthetischer Markt (`grokbot/feeds/simulated.py`) mit Runnern, Pump & Dumps,
  Rugs und Duds. Gut, um die Pipeline zu testen. **Die Ergebnisse sagen nichts über echte
  Gewinne aus**: das Modell ist vereinfacht und meist zu freundlich.
- **live** – öffentliche APIs ohne Key (`grokbot/feeds/live.py`):
  DexScreener (neue Paare, Preis, Liquidität, Volumen, Buys/Sells), RugCheck (Authorities,
  Holder, Top 10, LP-Lock, Creator-Anteil), Jupiter-Sell-Quote als Honeypot-Check.
  Fills bleiben simuliert, inklusive Price Impact, 0,3 % Gebühr und 1 % MEV/Latenz-Aufschlag.

### Smart Money / KOL über Helius

Der Agent `WALLETS` (`grokbot/feeds/helius.py`) fragt im Live-Modus regelmäßig die letzten
Swaps jeder beobachteten Wallet über die Helius Enhanced Transactions API ab. Ein **Kauf** ist ein
Swap, bei dem die Wallet einen Token bekommt, der nicht SOL/USDC/USDT ist.

- **smart money**: 3+ Smart-Money-Wallets kaufen denselben Token innerhalb von 10 min
- **KOL bought**: mindestens eine KOL-Wallet hat ihn in den letzten 30 min gekauft

Einrichten:

1. Kostenlosen API-Key auf https://dashboard.helius.dev holen
2. Wallets eintragen, eine Adresse pro Zeile: `wallets/smart_money.txt` (das Original nutzt 40)
   und `wallets/kol.txt`
3. Starten:

```bash
export HELIUS_API_KEY=dein-key
python -m grokbot --mode live --now
# optional: --smart-wallets pfad.txt --kol-wallets pfad.txt --helius-poll 60
```

Ohne Key oder ohne Wallets bleibt der Tracker aus (`WALLETS off` im Dashboard), dann feuern diese
zwei Signale nie. Jede Abfrage kostet pro Wallet einen API-Call: 40 Wallets alle 60 s sind
~19.000 Calls pro Nacht. Prüfe im Helius-Dashboard, ob dein Plan das abdeckt, und erhöhe sonst
`--helius-poll`. Pro Wallet werden die letzten 20 Swaps gelesen. Bei Wallets, die extrem viel
handeln, können dazwischen Käufe fehlen.

## Tests

```bash
python -m pytest -q
```
