"""
DASHBOARD SERVER v1.1
========================
Lightweight HTTP server that serves the trading dashboard
and exposes JSON API endpoints for bot state.

Runs in a background daemon thread alongside the main bot loop.
All data is read from JSON state files — no shared memory needed.

v1.1: Added /api/health endpoint, email alerts, observation mode resume.
"""

import json
import os
import smtplib
import threading
from datetime import datetime, timezone
from email.mime.text import MIMEText
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import config

# State file paths — all routed through config.STATE_DIR for persistence
STATE_FILES = {
    "trades": config.TRADE_LOG_FILE,
    "risk": config.RISK_STATE_FILE,
    "pnl": config.PNL_HISTORY_FILE,
    "bot_status": config.BOT_STATUS_FILE,
    "pending": config.PENDING_TRADES_FILE,
    "reports": config.DAILY_REPORTS_FILE,
    "backtest": config.BACKTEST_RESULTS_FILE,
    "attribution": config.EDGE_ATTRIBUTION_FILE,
}

DASHBOARD_PORT = int(os.environ.get("PORT", 8050))

# Track which alerts have already been sent (avoid spamming)
_alerts_sent = {}


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


# ═══════════════════════════════════════════════════════
# EMAIL ALERTS
# ═══════════════════════════════════════════════════════

def _send_email_alert(subject, body):
    """Send an email alert. Runs in a thread to avoid blocking."""
    if not config.ALERT_EMAIL or not config.SMTP_USER or not config.SMTP_PASS:
        return

    def _send():
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"[Kalshi Bot] {subject}"
            msg["From"] = config.SMTP_USER
            msg["To"] = config.ALERT_EMAIL

            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=15) as server:
                server.starttls()
                server.login(config.SMTP_USER, config.SMTP_PASS)
                server.send_message(msg)
        except Exception as e:
            print(f"  [ALERT] Email failed: {e}")

    threading.Thread(target=_send, daemon=True).start()


def _alert_once(key, subject, body):
    """Send an alert only once per key. Resets when the condition clears."""
    if key in _alerts_sent:
        return
    _alerts_sent[key] = True
    _send_email_alert(subject, body)


def _clear_alert(key):
    """Clear a previously-sent alert so it can fire again."""
    _alerts_sent.pop(key, None)


def _check_alerts(bot_status, risk_state, pending):
    """Run all alert checks. Called on each /api/health request."""
    now = datetime.now(timezone.utc)

    # 1. Stalled bot (no scan in HEALTH_STALE_MINUTES)
    last_scan_str = bot_status.get("timestamp")
    if last_scan_str:
        try:
            last_scan = datetime.fromisoformat(last_scan_str)
            if last_scan.tzinfo is None:
                last_scan = last_scan.replace(tzinfo=timezone.utc)
            age_min = (now - last_scan).total_seconds() / 60
            if age_min > config.HEALTH_STALE_MINUTES:
                _alert_once("stalled",
                    "Bot appears stalled",
                    f"Last scan was {age_min:.0f} minutes ago (threshold: {config.HEALTH_STALE_MINUTES}min).\n"
                    f"The bot may have crashed. Check Railway logs.")
            else:
                _clear_alert("stalled")
        except Exception:
            pass

    # 2. Observation mode triggered
    if bot_status.get("observation_mode") or risk_state.get("observation_mode"):
        reason = bot_status.get("observation_reason") or risk_state.get("observation_reason", "Unknown")
        _alert_once("observation",
            "Kill switch triggered — OBSERVATION MODE",
            f"The bot has entered observation mode.\nReason: {reason}\n\n"
            f"Trading is paused. Resume via the dashboard or clear observation_mode in risk_state.json.")
    else:
        _clear_alert("observation")

    # 3. Pending trade unapproved for too long
    for trade in (pending or []):
        if trade.get("status") != "pending":
            continue
        ts = trade.get("timestamp")
        if not ts:
            continue
        try:
            created = datetime.fromisoformat(ts)
            age_min = (datetime.now() - created).total_seconds() / 60
            if age_min > config.PENDING_ALERT_MINUTES:
                tid = trade.get("id", "?")
                _alert_once(f"pending_{tid}",
                    f"Pending trade waiting {age_min:.0f} min",
                    f"Trade {trade.get('ticker', '?')} {trade.get('side', '?').upper()} "
                    f"x{trade.get('contracts', '?')} has been waiting for approval "
                    f"for {age_min:.0f} minutes.\n\nApprove or reject it on the dashboard.")
        except Exception:
            pass

    # 4. Daily loss limit hit
    if risk_state.get("daily_loss_cents", 0) >= config.DAILY_LOSS_LIMIT_CENTS:
        _alert_once("daily_loss",
            "Daily loss limit hit",
            f"Daily losses have reached ${risk_state['daily_loss_cents']/100:.2f} "
            f"(limit: ${config.DAILY_LOSS_LIMIT_CENTS/100:.2f}).\n"
            f"No more trades will execute today.")
    else:
        _clear_alert("daily_loss")


# ═══════════════════════════════════════════════════════
# HEALTH ENDPOINT LOGIC
# ═══════════════════════════════════════════════════════

def _build_health_response():
    """Build the /api/health response."""
    now = datetime.now(timezone.utc)
    bot_status = _read_json(STATE_FILES["bot_status"], default={})
    risk_state = _read_json(STATE_FILES["risk"], default={})
    pending = _read_json(STATE_FILES["pending"], default=[])

    # Run alert checks
    _check_alerts(bot_status, risk_state, pending)

    last_scan_str = bot_status.get("timestamp")
    last_scan_age_sec = None
    status = "unknown"

    if last_scan_str:
        try:
            last_scan = datetime.fromisoformat(last_scan_str)
            if last_scan.tzinfo is None:
                last_scan = last_scan.replace(tzinfo=timezone.utc)
            last_scan_age_sec = (now - last_scan).total_seconds()
            age_min = last_scan_age_sec / 60

            if age_min <= config.HEALTH_WARN_MINUTES:
                status = "healthy"
            elif age_min <= config.HEALTH_STALE_MINUTES:
                status = "degraded"
            else:
                status = "down"
        except Exception:
            status = "unknown"

    if bot_status.get("observation_mode") or risk_state.get("observation_mode"):
        status = "observation"

    return {
        "status": status,
        "last_scan": last_scan_str,
        "last_scan_age_seconds": round(last_scan_age_sec) if last_scan_age_sec is not None else None,
        "cycle": bot_status.get("cycle"),
        "environment": bot_status.get("environment"),
        "dry_run": bot_status.get("dry_run"),
        "observation_mode": bot_status.get("observation_mode", False),
        "observation_reason": bot_status.get("observation_reason", ""),
        "timestamp": now.isoformat(),
    }


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

        elif path == "/api/health":
            self._send_json(_build_health_response())

        elif path == "/api/state":
            state = {}
            for key, filepath in STATE_FILES.items():
                if key in ("trades", "pending", "reports", "attribution"):
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
            _clear_alert("observation")
            self._send_json({"ok": True, "action": "resumed"})

        elif path == "/api/clear-failed":
            # Remove trades that were logged but never actually placed on Kalshi
            # (status: live_submitted, demo_submitted — not live_filled)
            trades = _read_json(STATE_FILES["trades"], default=[])
            original_count = len(trades)
            failed_statuses = {"live_submitted", "demo_submitted", "live_error"}
            failed_trades = [t for t in trades if t.get("status") in failed_statuses]
            kept_trades = [t for t in trades if t.get("status") not in failed_statuses]
            removed_count = original_count - len(kept_trades)
            _write_json(STATE_FILES["trades"], kept_trades)

            # Also clean up risk state: remove positions for tickers with NO successful trades
            failed_tickers = {t.get("ticker") for t in failed_trades}
            kept_tickers = {t.get("ticker") for t in kept_trades}
            orphaned_tickers = failed_tickers - kept_tickers  # only tickers with zero filled trades
            risk = _read_json(STATE_FILES["risk"], default={})
            positions = risk.get("positions", [])
            risk["positions"] = [p for p in positions if p.get("ticker") not in orphaned_tickers]
            # Recalculate city exposure from remaining positions
            risk["city_exposure"] = {}
            for p in risk["positions"]:
                city = p.get("city_code", "")
                if city:
                    risk["city_exposure"][city] = risk["city_exposure"].get(city, 0) + p.get("cost_cents", 0)
            risk["daily_trade_count"] = max(0, risk.get("daily_trade_count", 0) - removed_count)
            _write_json(STATE_FILES["risk"], risk)

            self._send_json({
                "ok": True,
                "action": "clear_failed",
                "removed": removed_count,
                "remaining": len(kept_trades),
                "removed_tickers": [t.get("ticker") for t in failed_trades],
            })

        elif path == "/api/reset":
            # Wipe all runtime state files to start fresh
            from datetime import datetime as _dt
            clean_state = {
                "trades": [],
                "risk": {
                    "daily_loss_cents": 0,
                    "daily_trade_count": 0,
                    "last_reset_date": _dt.now().strftime("%Y-%m-%d"),
                    "last_trade_time": None,
                    "positions": [],
                    "consecutive_losses": 0,
                    "loss_pause_until": None,
                    "city_exposure": {},
                    "total_exposure_cents": 0,
                },
                "pnl": {
                    "trades": [],
                    "total_invested_cents": 0,
                    "total_returned_cents": 0,
                    "total_profit_cents": 0,
                    "wins": 0,
                    "losses": 0,
                },
                "pending": [],
                "bot_status": {},
            }
            for key, default in clean_state.items():
                if key in STATE_FILES:
                    _write_json(STATE_FILES[key], default)
            self._send_json({"ok": True, "action": "reset", "cleared": list(clean_state.keys())})

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
