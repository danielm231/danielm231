# Grok Bot – Overnight Meme-Coin Trader (Multi-Agent, Paper Trading)

Nachbau des „Grok Bot“-Prompts als echtes Programm: 6 Agents handeln über Nacht
(23:00 → 07:00) frische Solana-Meme-Coins mit $70 Startkapital, loggen jede Entscheidung
und drucken morgens ein Recap.

> **Standard ist Paper-Trading.** Echtgeld gibt es nur mit ausdrücklichem `--real`, eigener
> Bot-Wallet und getipptem „JA“ (siehe [Echtes Trading](#echtes-trading)). Meme-Coins sind extrem
> riskant: die meisten gehen auf null.

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

Optionen: `--balance 70 --start 23:00 --end 07:00 --out runs --quiet --serve`

Jeder Lauf schreibt nach `runs/<mode>-<zeit>/`:

| Datei | Inhalt |
|---|---|
| `dashboard.html` | Live-Dashboard (aktualisiert sich alle 10 s): Balance, Trades, W/L, offene Position, Agent-Status, Equity-Kurve, komplettes Nacht-Log |
| `night.log.jsonl` | jede ENTER / PARTIAL / EXIT / FILTERED / PAUSE Zeile mit Grund, Größe, Fill-Preis, PnL |
| `recap.json` | Start vs. End, bester / schlechtester Trade, Anzahl Trades, Win-Rate, Top-3-Coins, max. Drawdown |

## Start per Doppelklick (Mac)

Im Repo liegt **`Grok Bot starten.command`**. Abends im Finder doppelklicken:

1. sucht Python ≥ 3.10 (zuerst die Version von python.org)
2. holt mit `git pull` die neueste Version
3. übernimmt den Helius-Key aus `~/.zshrc` und meldet, falls keiner da ist
4. warnt, wenn der Mac auf Akku läuft
5. startet `python3 -m grokbot --mode live --serve` mit `caffeinate`, damit der Mac wach bleibt

Den Deckel offen lassen: Bei geschlossenem Deckel schläft ein MacBook trotz `caffeinate` ein.
Beim ersten Mal fragt macOS evtl., ob Python eingehende Verbindungen annehmen darf: **Erlauben**,
sonst erreicht das Handy das Dashboard nicht. Beenden morgens mit Ctrl+C im Terminal-Fenster.

Lässt sich die Datei nicht öffnen: im Finder Rechtsklick → Öffnen, oder im Terminal
`chmod +x "Grok Bot starten.command"`.

## Dashboard am Handy ansehen

Mit `--serve` startet der Bot einen kleinen Webserver für das Dashboard:

```bash
python -m grokbot --mode live --serve
```

Im Terminal stehen dann die Adressen, z. B. `http://192.168.1.23:8000`. Die öffnest du am Handy
im Browser. Handy und Rechner müssen im selben WLAN sein. Die Seite aktualisiert sich alle
10 Sekunden. Nach 07:00 läuft der Server mit dem Recap weiter, bis du ihn mit Ctrl+C beendest.

| Adresse | Inhalt |
|---|---|
| `/` | Dashboard |
| `/log` | Nacht-Log als Rohdaten |
| `/recap` | Recap als JSON (ab 07:00) |

Anderer Port: `--serve 8080`. Der Server liefert nur diese drei Dateien des aktuellen Laufs aus,
keine anderen Dateien vom Rechner. **Auf einem Mietserver im Internet** wäre das Dashboard aber für
jeden sichtbar, der die Adresse kennt. Dort `--host 127.0.0.1` setzen und per SSH-Tunnel zugreifen
(`ssh -L 8000:localhost:8000 user@server`, dann am Gerät `http://localhost:8000`).

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

Der Agent `WALLETS` (`grokbot/feeds/helius.py`) beobachtet im Live-Modus die Wallets über den
normalen Helius-RPC. Pro Wallet und Abfrage wird zuerst nur geprüft, ob es **neue** Transaktionen
gibt (1 Credit). Nur neue, erfolgreiche Transaktionen der letzten 30 min werden geladen
(1 Credit pro Stück). Ein **Kauf** heißt: Der Bestand der Wallet an einem Token (nicht
SOL/USDC/USDT) ist in der Transaktion gestiegen.

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
zwei Signale nie.

**Credits (Free-Plan: 1 Mio./Monat):** 40 Wallets alle 60 s sind ~19.000 Credits pro Nacht plus
1 Credit pro neuer Wallet-Transaktion, grob 20.000–30.000 pro Nacht, also ~600.000–900.000 bei
30 Nächten im Monat. Der aktuelle Verbrauch steht im Dashboard (`~credits`). Wenn es knapp wird:
`--helius-poll 120` halbiert die Grundkosten. Pro Abfrage werden maximal 25 neue Transaktionen
je Wallet gelesen, bei extrem aktiven Wallets können Käufe fehlen.

## Wallet-Prüfer

Bewertet Kandidaten für `wallets/smart_money.txt` anhand ihrer echten Swaps (Helius-RPC):

```bash
export HELIUS_API_KEY=dein-key
python -m grokbot.walletcheck kandidaten.txt --days 30
```

`kandidaten.txt` hat dasselbe Format wie die Wallet-Listen. Pro Wallet werden die Swaps der
letzten N Tage geladen, Käufe und Verkäufe je Token gegen SOL verrechnet und bewertet:

| Kennzahl | führt zu |
|---|---|
| Verlust über den Zeitraum, Trefferquote < 30 % | **RAUS** |
| > 30 % der Trades unter 60 s gehalten (Sniper/Bot) | **RAUS** |
| weniger als 10 abgeschlossene Trades (`--min-trades`) | PRUEFEN |
| Trefferquote > 85 % (möglicher Lockvogel) | PRUEFEN |
| > 60 % des Gewinns aus einem einzigen Trade | PRUEFEN |
| Median-Haltedauer < 2 min, Ø Einsatz < 0,2 SOL | PRUEFEN |
| Wallet jünger als 14 Tage (mit Geldgeber-Adresse) | PRUEFEN |
| nichts davon | **GUT** |

Ergebnis: Tabelle im Terminal, `wallet_report.csv` mit allen Kennzahlen und
`wallets/gepruefte_wallets.txt` mit den GUT-Wallets zum Rüberkopieren.

**Credits:** 1 pro geladener Transaktion (+1 pro 1000 Signaturen). `--max-tx 300` (Standard)
begrenzt das auf ~300 pro Wallet, 40 Kandidaten also höchstens ~12.000. `--max-credits`
(Standard 20.000) bricht den Lauf ab, bevor es mehr wird.

**Grenzen:** Nur Trades gegen SOL werden gezählt (USDC-Trades und Token-zu-Token-Routen werden
übersprungen). Bei `--max-tx` wird nur der neueste Teil der Historie bewertet. Konto-Miete
(~0,002 SOL pro neuem Token-Konto) zählt als Kosten und drückt den PnL bei Kleinst-Trades leicht.

## Echtes Trading

Drei Stufen, jede mit einem eigenen Schalter:

| Stufe | Befehl | Was passiert |
|---|---|---|
| Paper | `python3 -m grokbot --mode live` | simulierte Fills, keine Wallet |
| Dry-Run | `python3 -m grokbot --dry-run` | echte Jupiter-Transaktionen mit deiner Bot-Wallet werden gebaut, signiert und von der Blockchain **simuliert**, aber **nicht gesendet** |
| Echtgeld | `python3 -m grokbot --real` | echte Swaps, Buchung mit den echten Beträgen aus der Blockchain |

### Einrichten

```bash
python3 -m pip install -r requirements-real.txt   # Signier-Bibliothek (solders)
python3 -m grokbot.wallet new                     # neue Bot-Wallet, zeigt die Adresse
python3 -m grokbot.wallet show                    # Adresse + Guthaben
```

- Der Schlüssel liegt in `~/.grokbot/wallet.json` (nur für dich lesbar, nie im Repo). **Backup
  davon machen**, ohne die Datei ist das Guthaben weg.
- Eine bestehende Wallet importieren: `python3 -m grokbot.wallet import` (Eingabe unsichtbar).
  Nur eine Wallet nehmen, die ausschließlich für den Bot da ist.
- An die Adresse nur schicken, was du verlieren kannst, plus ~0,05 SOL für Gebühren.
  Auch der Dry-Run braucht Guthaben, weil die Blockchain sonst „zu wenig SOL“ simuliert. Er gibt
  aber nichts davon aus.
- `HELIUS_API_KEY` muss gesetzt sein: Transaktionen laufen über deinen Helius-RPC.

### Sicherheitsgurte

| Schutz | Standard | Option |
|---|---|---|
| Budget, das der Bot insgesamt nutzt | $15 (höchstens Guthaben minus Reserve) | `--budget` |
| harte Obergrenze pro Trade | $5 | `--max-trade` |
| Kill-Switch: alles verkaufen und stoppen bei Verlust von | 40 % des Budgets | `--kill-pct` |
| SOL-Reserve für Gebühren, wird nie gehandelt | 0,02 SOL | `RealConfig` |
| Priority-Fee pro Swap höchstens | 0,0002 SOL | `RealConfig` |
| Start nur nach getipptem `JA` | immer | - |
| Einstieg in einem Teil statt zwei (halbe Gebühren) | immer bei Dry-Run/Echtgeld | - |

Verhalten bei Problemen:

- **Verkauf scheitert:** neue Versuche mit 2,5 → 5 → 10 → 20 % Slippage. Klappt keiner, bleibt die
  Position und wird beim nächsten Tick erneut verkauft. Nach 10 fehlgeschlagenen Runden (z. B. Rug
  ohne Liquidität) wird sie abgeschrieben, der Token bleibt in der Wallet.
- **5 Fehler in Folge** (z. B. Netzwerk weg): alles verkaufen und stoppen.
- **Neustart nach Absturz:** Die Wallet ist die Quelle der Wahrheit. Beim Start listet der Bot
  Restbestände auf und fragt, ob er sie verkaufen soll.
- **Nach einem Komplettverkauf** wird das Token-Konto geschlossen, die ~0,002 SOL Miete kommen zurück.

Das Recap enthält zusätzlich `swaps` und `network_fees_sol`.

### Empfohlene Reihenfolge

1. 1–2 Wochen `--mode live` (Paper) und die Recaps auswerten
2. ein paar Nächte `--dry-run`: Prüfen, ob Simulationen durchgehen (`FILTERED … Simulation
   fehlgeschlagen` heißt: dieser Coin wäre nicht kaufbar gewesen)
3. `--real --budget 15 --max-trade 5`
4. erst danach mehr Budget

Mit der Start-Datei geht das im Terminal so: `"./Grok Bot starten.command" --real`

## Tests

```bash
python -m pytest -q
```
