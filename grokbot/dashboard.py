"""Live dashboard: terminal view + self-refreshing HTML file."""
from __future__ import annotations

import html
import json
from pathlib import Path

from .agents.logger import TradeLogger
from .orchestrator import Orchestrator


def terminal(o: Orchestrator, mode: str) -> str:
    eq = o.total_equity()
    e = o.equity
    lines = [
        "=" * 72,
        f" GROK BOT [{mode}]  {o.now:%H:%M}   balance ${eq:.2f}   "
        f"start ${o.cfg.start_balance:.2f}   {100 * (eq / o.cfg.start_balance - 1):+.1f}%",
        f" trades {len(e.trades)}  W/L {e.wins}/{e.losses}  peak ${e.peak:.2f}  "
        f"DD {e.drawdown_pct(eq):.1f}%  {'PAUSED' if o.paused else 'ACTIVE'}",
        "-" * 72,
    ]
    for p in o.executor.positions.values():
        s = o.last_snaps.get(p.mint)
        px = s.price if s else p.entry_price
        lines.append(f" POS {p.symbol:<8} ${p.invested_usd:6.2f} in  {px / p.entry_price:5.2f}x  "
                     f"TP {p.tp_hits}  value ${p.tokens * px:.2f}")
    for a in o.agents:
        lines.append(f"  {a.name:<10} {a.status}")
    lines.append("=" * 72)
    return "\n".join(lines)


def _svg_curve(curve, w=640, h=160) -> str:
    if len(curve) < 2:
        return ""
    vals = [v for _, v in curve]
    step = max(1, len(vals) // 400)
    vals = vals[::step] + [vals[-1]]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pts = " ".join(f"{i * w / (len(vals) - 1):.1f},{h - 8 - (v - lo) / span * (h - 16):.1f}"
                   for i, v in enumerate(vals))
    return (f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none" class="curve">'
            f'<polyline points="{pts}" fill="none" stroke="var(--accent)" stroke-width="2"/></svg>'
            f'<div class="axis"><span>${lo:.2f}</span><span>${hi:.2f}</span></div>')


def write_html(o: Orchestrator, path: Path, mode: str, recap: dict | None = None) -> None:
    eq = o.total_equity()
    e = o.equity
    pos_rows = "".join(
        f"<tr><td>{html.escape(p.symbol)}</td><td>${p.invested_usd:.2f}</td>"
        f"<td>{(o.last_snaps[p.mint].price if p.mint in o.last_snaps else p.entry_price) / p.entry_price:.2f}x</td>"
        f"<td>{p.tp_hits}</td></tr>" for p in o.executor.positions.values()
    ) or '<tr><td colspan="4" class="muted">flat</td></tr>'
    agents = "".join(f"<tr><td>{a.name}</td><td>{html.escape(a.status)}</td></tr>" for a in o.agents)
    log = "\n".join(html.escape(TradeLogger.format(x)) for x in reversed(o.log.lines[-300:]))
    recap_html = (f"<h2>Recap 07:00</h2><pre>{html.escape(json.dumps(recap, indent=2))}</pre>"
                  if recap else "")
    refresh = "" if recap else '<meta http-equiv="refresh" content="10">'
    pnl = 100 * (eq / o.cfg.start_balance - 1)
    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}
<title>Grok Bot Dashboard</title><style>
:root{{--bg:#f7f7f8;--card:#fff;--fg:#16181d;--muted:#6b7280;--accent:#1f8a4c;--line:#e5e7eb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0f1115;--card:#171a21;--fg:#e8eaee;--muted:#9097a3;--accent:#3ccf7a;--line:#262a33}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,Menlo,monospace}}
main{{max-width:960px;margin:0 auto;padding:16px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin-top:12px}}
.k{{color:var(--muted);font-size:12px}}.v{{font-size:20px;font-weight:600}}
table{{width:100%;border-collapse:collapse}}td{{padding:4px 6px;border-bottom:1px solid var(--line)}}
.muted{{color:var(--muted)}}.curve{{width:100%;height:160px}}
.axis{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}}
pre{{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px;max-height:420px;overflow:auto}}
</style></head><body><main>
<h1>Grok Bot <span class="muted">· {mode} · {o.now:%H:%M}</span></h1>
<div class="kpis">
<div class="card"><div class="k">Balance</div><div class="v">${eq:.2f}</div><div class="k">{pnl:+.1f}% vs ${o.cfg.start_balance:.0f}</div></div>
<div class="card"><div class="k">Trades</div><div class="v">{len(e.trades)}</div></div>
<div class="card"><div class="k">Wins / Losses</div><div class="v">{e.wins} / {e.losses}</div></div>
<div class="card"><div class="k">Status</div><div class="v">{'PAUSED' if o.paused else 'ACTIVE'}</div><div class="k">DD {e.drawdown_pct(eq):.1f}%</div></div>
</div>
<div class="card"><div class="k">Equity curve</div>{_svg_curve(e.curve)}</div>
<div class="card"><div class="k">Open positions</div><table>{pos_rows}</table></div>
<div class="card"><div class="k">Agents</div><table>{agents}</table></div>
{f'<div class="card">{recap_html}</div>' if recap else ''}
<div class="card"><div class="k">Night log (newest first)</div><pre>{log}</pre></div>
</main></body></html>""")
