"""Agent 5 - LOGGER: writes every ENTER / EXIT / FILTERED line."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class TradeLogger:
    name = "LOGGER"

    def __init__(self, path: Path | None = None, echo: bool = True):
        self.lines: list[dict] = []
        self.path = path
        self.echo = echo
        self.status = "0 lines"
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")

    def log(self, ts: datetime, kind: str, symbol: str, reason: str,
            size: float | None = None, price: float | None = None,
            pnl: float | None = None) -> None:
        entry = {"time": ts.strftime("%H:%M:%S"), "kind": kind, "symbol": symbol,
                 "reason": reason, "size": size, "price": price, "pnl": pnl}
        self.lines.append(entry)
        self.status = f"{len(self.lines)} lines"
        if self.path:
            with self.path.open("a") as f:
                f.write(json.dumps(entry) + "\n")
        if self.echo:
            print(self.format(entry))

    @staticmethod
    def format(e: dict) -> str:
        parts = [f"[{e['time']}] {e['kind']:<8} {e['symbol']:<10}"]
        if e["size"] is not None:
            parts.append(f"size=${e['size']:.2f}")
        if e["price"] is not None:
            parts.append(f"px={e['price']:.8g}")
        if e["pnl"] is not None:
            parts.append(f"pnl={e['pnl']:+.2f}")
        parts.append(f"| {e['reason']}")
        return " ".join(parts)
