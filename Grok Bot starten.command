#!/bin/bash
# Grok Bot - Start per Doppelklick (macOS)
#
# Holt die neueste Version, hält den Mac wach und startet den Bot im
# Live-Modus (Paper-Trading) mit Dashboard fürs Handy.
# Zusätzliche Optionen werden durchgereicht, z. B. im Terminal:
#   "./Grok Bot starten.command" --balance 100

# Alles steckt in main(): bash liest die Funktion komplett ein, bevor sie läuft.
# So stört es nicht, wenn "git pull" diese Datei während des Laufs aktualisiert.
main() {
cd "$(dirname "$0")" || exit 1

echo "=================================================="
echo "  Grok Bot - Overnight Paper Trading"
echo "=================================================="
echo

ende() {
    echo
    echo "Fertig. Dieses Fenster kann geschlossen werden."
    read -r -p "Enter drücken zum Schließen ... " _
    exit "${1:-0}"
}

# --- 1) Python >= 3.10 finden (python.org-Version zuerst) -------------------
PY=""
for cand in /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
            /opt/homebrew/bin/python3 /usr/local/bin/python3 \
            python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        PY="$cand"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "FEHLER: Kein Python 3.10 oder neuer gefunden."
    echo "Installiere Python von https://www.python.org/downloads/"
    echo "und klicke danach unter Programme > Python 3.x auf 'Install Certificates.command'."
    ende 1
fi
echo "Python:  $("$PY" --version)"

# --- 2) Neueste Version holen ----------------------------------------------
if command -v git >/dev/null 2>&1 && [ -d .git ]; then
    if git pull --ff-only -q 2>/dev/null; then
        echo "Version: aktuell ($(git log -1 --format=%h))"
    else
        echo "Hinweis: Update nicht möglich (offline oder lokale Änderungen). Starte vorhandene Version."
    fi
fi

# --- 3) Helius-Key aus ~/.zshrc bzw. ~/.zprofile übernehmen -------------------
if [ -z "$HELIUS_API_KEY" ]; then
    line=$(grep -h -E '^[[:space:]]*export[[:space:]]+HELIUS_API_KEY=' \
           ~/.zshrc ~/.zprofile ~/.bash_profile 2>/dev/null | tail -n 1)
    [ -n "$line" ] && eval "$line"
fi
if [ -n "$HELIUS_API_KEY" ]; then
    echo "Helius:  Key gefunden"
else
    echo "Helius:  KEIN Key - Smart-Money/KOL-Signale bleiben aus."
    echo "         Einrichten: echo 'export HELIUS_API_KEY=dein-key' >> ~/.zshrc"
fi

# --- 4) Strom prüfen ---------------------------------------------------------
if command -v pmset >/dev/null 2>&1 && ! pmset -g batt | grep -q "AC Power"; then
    echo
    echo "ACHTUNG: Der Mac läuft auf Akku. Bitte das Netzteil anschließen,"
    echo "sonst reicht der Akku evtl. nicht durch die Nacht."
fi

echo
echo "Deckel offen lassen. Der Bot wartet bis 23:00 und läuft bis 07:00."
echo "Beenden: Ctrl+C in diesem Fenster."
echo "--------------------------------------------------"
echo

# --- 5) Starten, Mac wach halten ----------------------------------------------
if command -v caffeinate >/dev/null 2>&1; then
    caffeinate -is "$PY" -m grokbot --mode live --serve "$@"
else
    "$PY" -m grokbot --mode live --serve "$@"
fi
ende $?
}

main "$@"
exit $?
