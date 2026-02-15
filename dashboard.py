"""
DASHBOARD SERVER v1.0
========================
Lightweight HTTP server that serves the trading dashboard
and exposes JSON API endpoints for bot state.

Runs in a background daemon thread alongside the main bot loop.
All data is read from JSON state files — no shared memory needed.
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# State file paths (relative to bot working directory)
STATE_FILES = {
    "trades": "trade_history.json",
    "risk": "risk_state.json",
    "pnl": "pnl_history.json",
    "bot_status": "bot_status.json",
    "pending": "pending_trades.json",
    "reports": "daily_reports.json",
    "backtest": "backtest_results.json",
    "attribution": "edge_attribution.json",
}

DASHBOARD_PORT = int(os.environ.get("PORT", 8050))


def _read_json(path, default=None):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default if default is not None else {}


def _write_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


class DashboardHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests for the dashboard."""

    def log_message(self, format, *args):
        """Suppress default request logging to keep bot output clean."""
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                body = f.read().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Dashboard HTML not found")

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/" or path == "/dashboard":
            self._send_html("dashboard.html")

        elif path == "/api/state":
            state = {}
            for key, filepath in STATE_FILES.items():
                if key == "trades":
                    state[key] = _read_json(filepath, default=[])
                elif key == "pending":
                    state[key] = _read_json(filepath, default=[])
                elif key == "reports":
                    state[key] = _read_json(filepath, default=[])
                elif key == "attribution":
                    state[key] = _read_json(filepath, default=[])
                else:
                    state[key] = _read_json(filepath, default={})
            self._send_json(state)

        elif path == "/api/pending":
            self._send_json(_read_json(STATE_FILES["pending"], default=[]))

        elif path == "/api/reports":
            self._send_json(_read_json(STATE_FILES["reports"], default=[]))

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(body)
        except Exception:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if path == "/api/approve":
            trade_id = payload.get("id")
            if not trade_id:
                self._send_json({"error": "Missing trade id"}, 400)
                return
            pending = _read_json(STATE_FILES["pending"], default=[])
            found = False
            for trade in pending:
                if trade.get("id") == trade_id and trade.get("status") == "pending":
                    trade["status"] = "approved"
                    found = True
                    break
            if found:
                _write_json(STATE_FILES["pending"], pending)
                self._send_json({"ok": True, "action": "approved"})
            else:
                self._send_json({"error": "Trade not found or already processed"}, 404)

        elif path == "/api/reject":
            trade_id = payload.get("id")
            if not trade_id:
                self._send_json({"error": "Missing trade id"}, 400)
                return
            pending = _read_json(STATE_FILES["pending"], default=[])
            found = False
            for trade in pending:
                if trade.get("id") == trade_id and trade.get("status") == "pending":
                    trade["status"] = "rejected"
                    found = True
                    break
            if found:
                _write_json(STATE_FILES["pending"], pending)
                self._send_json({"ok": True, "action": "rejected"})
            else:
                self._send_json({"error": "Trade not found or already processed"}, 404)

        elif path == "/api/resume":
            # Resume trading: clear observation mode in risk_state.json
            risk = _read_json(STATE_FILES["risk"], default={})
            risk["observation_mode"] = False
            risk["observation_reason"] = ""
            risk["consecutive_losses"] = 0
            risk["loss_pause_until"] = None
            _write_json(STATE_FILES["risk"], risk)
            self._send_json({"ok": True, "action": "resumed"})

        else:
            self.send_error(404)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


def start_dashboard_server(port=DASHBOARD_PORT):
    """Start the dashboard HTTP server in a background thread.

    Uses a non-daemon thread so the server stays alive even if the
    main bot thread crashes.  On Railway this keeps the process
    running and responding to health checks.
    """
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=False)
    thread.start()
    print(f"  [DASHBOARD] Running on port {port}")
    return server
