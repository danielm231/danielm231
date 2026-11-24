"""Tiny built-in web server so the dashboard can be watched from a phone.

Serves only the current run folder:
  /             -> dashboard.html (refreshes itself every 10 s)
  /log          -> night.log.jsonl
  /recap        -> recap.json (after 07:00)
Nothing else on the machine is reachable through it.
"""
from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROUTES = {
    "/": ("dashboard.html", "text/html; charset=utf-8"),
    "/dashboard.html": ("dashboard.html", "text/html; charset=utf-8"),
    "/log": ("night.log.jsonl", "text/plain; charset=utf-8"),
    "/recap": ("recap.json", "application/json"),
}

WAITING = (b"<!doctype html><meta charset='utf-8'><meta http-equiv='refresh' content='5'>"
           b"<meta name='viewport' content='width=device-width,initial-scale=1'>"
           b"<body style='font-family:monospace;padding:16px'>Grok Bot startet &hellip;</body>")


def make_handler(run_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            route = ROUTES.get(self.path.split("?", 1)[0])
            if route is None:
                return self._send(404, b"not found", "text/plain")
            name, ctype = route
            f = run_dir / name
            if not f.exists():
                if name == "dashboard.html":
                    return self._send(200, WAITING, ctype)
                return self._send(404, f"{name} gibt es noch nicht".encode(), "text/plain; charset=utf-8")
            self._send(200, f.read_bytes(), ctype)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):   # keep the terminal clean
            pass

    return Handler


def local_ip() -> str:
    """LAN address of this machine (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def start(run_dir: Path, port: int = 8000, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer((host, port), make_handler(run_dir))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def urls(srv: ThreadingHTTPServer) -> list[str]:
    host, port = srv.server_address[:2]
    if host in ("0.0.0.0", ""):
        return [f"http://{local_ip()}:{port}", f"http://localhost:{port}"]
    return [f"http://{host}:{port}"]
