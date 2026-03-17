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
from zoneinfo import ZoneInfo

import config

# State file paths — all routed through config.STATE_DIR for persistence
STATE_FILES = {
    "trades": config.TRADE_LOG_FILE,
    "risk": config.RISK_STATE_FILE,
    "pnl": config.PNL_HISTORY_FILE,
    "bot_status": config.BOT_STATUS_FILE,
    "scan_log": config.SCAN_LOG_FILE,
    "maker": config.MAKER_ORDERS_FILE,
    "learning": config.LEARNING_STATE_FILE,
}

DASHBOARD_PORT = int(os.environ.get("PORT", 8050))

# Track which alerts have already been sent (avoid spamming)
_alerts_sent = {}

# Shared KalshiClient instance — set by kalshi_bot.py after initialization
_kalshi_client = None


def set_kalshi_client(client):
    """Store a reference to the KalshiClient for force-exit endpoint."""
    global _kalshi_client
    _kalshi_client = client


# Shared TradeReviewer instance — set by kalshi_bot.py after initialization
_trade_reviewer = None


def set_trade_reviewer(reviewer):
    """Store a reference to the TradeReviewer for learning endpoint."""
    global _trade_reviewer
    _trade_reviewer = reviewer


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
        config.atomic_json_save(path, data)
    except Exception:
        pass


def _default_risk_state():
    return {
        "positions": {},
        "pending_orders": {},
        "daily_pnl_cents": 0,
        "daily_date": datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d"),
        "consecutive_losses": 0,
        "kill_switch_until": None,
        "last_trade_time": None,
        "trade_count_today": 0,
        "total_exposure_cents": 0,
        "observation_mode": False,
        "observation_reason": "",
    }


def _merge_position(existing, incoming):
    existing_contracts = int(existing.get("contracts", 0) or 0)
    incoming_contracts = int(incoming.get("contracts", 0) or 0)
    if incoming_contracts <= 0:
        return existing

    existing_cost = int(existing.get("cost_cents", 0) or 0)
    incoming_cost = int(incoming.get("cost_cents", 0) or 0)
    total_contracts = existing_contracts + incoming_contracts
    total_cost = existing_cost + incoming_cost

    merged = dict(existing)
    merged.update(incoming)
    merged["contracts"] = total_contracts
    merged["cost_cents"] = total_cost
    if total_contracts > 0:
        merged["price_cents"] = int(round(float(total_cost) / float(total_contracts)))
    return merged


def _rebuild_risk_metrics(risk):
    total_exposure = 0
    city_exposure = {}

    for pos in risk.get("positions", {}).values():
        cost_cents = int(pos.get("cost_cents", 0) or 0)
        total_exposure += cost_cents
        city_code = pos.get("city_code", "")
        if city_code:
            city_exposure[city_code] = city_exposure.get(city_code, 0) + cost_cents

    for order in risk.get("pending_orders", {}).values():
        remaining_cost = int(order.get("remaining_cost_cents", 0) or 0)
        total_exposure += remaining_cost
        city_code = order.get("city_code", "")
        if city_code:
            city_exposure[city_code] = city_exposure.get(city_code, 0) + remaining_cost

    risk["total_exposure_cents"] = total_exposure
    risk["city_exposure"] = city_exposure
    return risk


def _normalize_risk_state(risk):
    state = _default_risk_state()
    if not isinstance(risk, dict):
        return state

    for key, value in risk.items():
        if key not in ("positions", "pending_orders"):
            state[key] = value

    if "trade_count_today" not in risk and "daily_trade_count" in risk:
        state["trade_count_today"] = int(risk.get("daily_trade_count", 0) or 0)
    if "daily_pnl_cents" not in risk and "daily_loss_cents" in risk:
        state["daily_pnl_cents"] = -abs(int(risk.get("daily_loss_cents", 0) or 0))
    if not state.get("daily_date") and risk.get("last_reset_date"):
        state["daily_date"] = risk.get("last_reset_date", "")
    if not state.get("kill_switch_until") and risk.get("loss_pause_until"):
        state["kill_switch_until"] = risk.get("loss_pause_until")

    raw_positions = risk.get("positions", {})
    if isinstance(raw_positions, dict):
        position_rows = [(ticker, pos) for ticker, pos in raw_positions.items()]
    elif isinstance(raw_positions, list):
        position_rows = []
        for pos in raw_positions:
            if isinstance(pos, dict):
                ticker = pos.get("ticker", "")
                if ticker:
                    position_rows.append((ticker, pos))
    else:
        position_rows = []

    positions = {}
    for ticker, pos in position_rows:
        if not isinstance(pos, dict):
            continue
        ticker = pos.get("ticker", ticker) or ticker
        if not ticker:
            continue

        normalized = dict(pos)
        normalized["ticker"] = ticker
        normalized["contracts"] = int(normalized.get("contracts", 0) or 0)
        normalized["cost_cents"] = int(normalized.get("cost_cents", 0) or 0)
        normalized["price_cents"] = int(normalized.get("price_cents", 0) or 0)
        if normalized["contracts"] <= 0:
            continue

        existing = positions.get(ticker)
        if existing and existing.get("side") == normalized.get("side"):
            positions[ticker] = _merge_position(existing, normalized)
        else:
            positions[ticker] = normalized

    raw_pending = risk.get("pending_orders", {})
    if isinstance(raw_pending, dict):
        pending_rows = raw_pending.items()
    elif isinstance(raw_pending, list):
        pending_rows = []
        for order in raw_pending:
            if isinstance(order, dict):
                order_id = order.get("order_id", "")
                if order_id:
                    pending_rows.append((order_id, order))
    else:
        pending_rows = []

    pending_orders = {}
    for order_id, order in pending_rows:
        if not isinstance(order, dict):
            continue
        order_id = order.get("order_id", order_id) or order_id
        if not order_id:
            continue

        normalized = dict(order)
        normalized["order_id"] = order_id
        normalized["requested_contracts"] = int(
            normalized.get("requested_contracts", normalized.get("contracts", 0)) or 0
        )
        normalized["remaining_contracts"] = int(
            normalized.get("remaining_contracts", normalized["requested_contracts"]) or 0
        )
        normalized["filled_contracts"] = int(normalized.get("filled_contracts", 0) or 0)
        normalized["remaining_cost_cents"] = int(
            normalized.get(
                "remaining_cost_cents",
                int(normalized.get("price_cents", 0) or 0) * normalized["remaining_contracts"],
            ) or 0
        )
        if normalized["remaining_contracts"] <= 0:
            continue
        pending_orders[order_id] = normalized

    state["positions"] = positions
    state["pending_orders"] = pending_orders
    return _rebuild_risk_metrics(state)


def _is_executed_buy_fill(trade):
    if not isinstance(trade, dict):
        return False
    if trade.get("entry_type"):
        return trade.get("entry_type") == "buy_fill"
    return trade.get("status") in ("live_filled", "executed")


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
            if age_min > getattr(config, "PENDING_ALERT_MINUTES", 30):
                tid = trade.get("id", "?")
                _alert_once(f"pending_{tid}",
                    f"Pending trade waiting {age_min:.0f} min",
                    f"Trade {trade.get('ticker', '?')} {trade.get('side', '?').upper()} "
                    f"x{trade.get('contracts', '?')} has been waiting for approval "
                    f"for {age_min:.0f} minutes.\n\nApprove or reject it on the dashboard.")
        except Exception:
            pass

    # 4. Daily loss limit hit
    daily_pnl_cents = int(risk_state.get("daily_pnl_cents", 0) or 0)
    if daily_pnl_cents <= -config.DAILY_LOSS_LIMIT_CENTS:
        _alert_once("daily_loss",
            "Daily loss limit hit",
            f"Daily P&L has reached ${daily_pnl_cents/100:.2f} "
            f"(limit: ${config.DAILY_LOSS_LIMIT_CENTS/100:.2f}).\n"
            f"No more trades will execute today.")
    else:
        _clear_alert("daily_loss")


# ═══════════════════════════════════════════════════════
# HEALTH ENDPOINT LOGIC
# ═══════════════════════════════════════════════════════

def _build_health_response(authenticated=False):
    """Build the /api/health response.

    When *authenticated* is False only ``{status, timestamp}`` is returned.
    Full operational details require an authenticated request.
    """
    now = datetime.now(timezone.utc)
    bot_status = _read_json(STATE_FILES["bot_status"], default={})
    risk_state = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
    pending = []  # Pending trades removed in v4

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

    if not authenticated:
        return {"status": status, "timestamp": now.isoformat()}

    response = {
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

    # Include last error info if present (written by main loop catch-all handler)
    if bot_status.get("last_error"):
        response["last_error"] = bot_status["last_error"]
        response["last_error_time"] = bot_status.get("last_error_time")
        response["last_error_cycle"] = bot_status.get("last_error_cycle")

    return response


class DashboardHandler(BaseHTTPRequestHandler):
    """Handle HTTP requests for the dashboard."""

    def log_message(self, format, *args):
        """Suppress default request logging to keep bot output clean."""
        pass

    def _is_authenticated(self):
        """Check whether the request carries a valid Bearer token. Does NOT send a 401 on failure."""
        token = config.DASHBOARD_TOKEN
        if not token:
            return True  # No auth configured (local dev)
        auth_header = self.headers.get("Authorization", "")
        if auth_header == "Bearer %s" % token:
            return True
        return False

    def _check_auth(self):
        """Validate bearer token if DASHBOARD_TOKEN is configured. Returns True if authorized."""
        if self._is_authenticated():
            return True
        self._send_json({"error": "Unauthorized"}, 401)
        return False

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CORS_ORIGIN", "*"))
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
            # Unauthenticated: minimal {status, timestamp}.
            # Authenticated: full operational details.
            is_authed = self._is_authenticated()
            self._send_json(_build_health_response(authenticated=is_authed))

        elif path == "/api/state":
            if not self._check_auth():
                return
            state = {}
            for key, filepath in STATE_FILES.items():
                if key in ("trades", "reports", "attribution", "analysis", "scan_log"):
                    state[key] = _read_json(filepath, default=[])
                else:
                    state[key] = _read_json(filepath, default={})
            state["risk"] = _normalize_risk_state(state.get("risk", {}))
            self._send_json(state)

        elif path == "/api/pending":
            self.send_response(410)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Gone", "message": "This endpoint has been removed"}')
            return

        elif path == "/api/reports":
            self.send_response(410)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "Gone", "message": "This endpoint has been removed"}')
            return

        elif path == "/api/fills":
            if not self._check_auth():
                return
            # Fetch recent fills from Kalshi API (buys + sells)
            if _kalshi_client is None:
                self._send_json({"error": "KalshiClient not initialized"}, 503)
            else:
                result = _kalshi_client.get_fills(limit=200)
                self._send_json(result or {"error": "API call failed"})

        elif path == "/api/learning":
            if not self._check_auth():
                return
            if _trade_reviewer is None:
                self._send_json({"error": "TradeReviewer not initialized"}, 503)
            else:
                self._send_json(_trade_reviewer.get_learning_summary())

        elif path == "/api/balance":
            if not self._check_auth():
                return
            if _kalshi_client is None:
                self._send_json({"error": "KalshiClient not initialized"}, 503)
            else:
                result = _kalshi_client.get_balance()
                self._send_json(result or {"error": "API call failed"})

        elif path == "/api/performance":
            if not self._check_auth():
                return
            # Load trade intelligence and compute metrics
            try:
                pnl_data = _read_json(STATE_FILES.get("pnl", "pnl_history.json"), default={})
                daily_history = pnl_data.get("daily_history", [])

                if not daily_history:
                    self._send_json({"error": "No daily history available"})
                    return

                import math
                daily_pnls = [d.get("pnl_cents", 0) for d in daily_history]
                n = len(daily_pnls)

                # Sharpe ratio (annualized)
                sharpe = None
                if n >= 5:
                    mean_pnl = sum(daily_pnls) / n
                    variance = sum((x - mean_pnl) ** 2 for x in daily_pnls) / (n - 1)
                    std_pnl = math.sqrt(variance) if variance > 0 else 0
                    if std_pnl > 0:
                        sharpe = round((mean_pnl / std_pnl) * math.sqrt(252), 3)

                # Max drawdown
                cumulative = 0
                peak = 0
                max_dd_cents = 0
                max_dd_pct = 0.0
                for pnl in daily_pnls:
                    cumulative += pnl
                    if cumulative > peak:
                        peak = cumulative
                    dd = peak - cumulative
                    if dd > max_dd_cents:
                        max_dd_cents = dd
                        max_dd_pct = round(dd / peak, 4) if peak > 0 else 0

                # Win rate
                winning = sum(1 for p in daily_pnls if p > 0)
                losing = sum(1 for p in daily_pnls if p < 0)
                win_rate = round(winning / n, 3) if n > 0 else 0

                # Profit factor
                gross_profit = sum(p for p in daily_pnls if p > 0)
                gross_loss = abs(sum(p for p in daily_pnls if p < 0))
                profit_factor = round(gross_profit / gross_loss, 3) if gross_loss > 0 else None

                self._send_json({
                    "sharpe_ratio": sharpe,
                    "max_drawdown_cents": max_dd_cents,
                    "max_drawdown_pct": max_dd_pct,
                    "win_rate": win_rate,
                    "profit_factor": profit_factor,
                    "winning_days": winning,
                    "losing_days": losing,
                    "total_days": n,
                    "cumulative_pnl_cents": sum(daily_pnls),
                    "rolling_5d_pnl_cents": pnl_data.get("rolling_5d_pnl_cents"),
                })
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/equity-curve":
            if not self._check_auth():
                return
            try:
                pnl_data = _read_json(STATE_FILES.get("pnl", "pnl_history.json"), default={})
                daily_history = pnl_data.get("daily_history", [])

                cumulative = 0
                curve = []
                for entry in daily_history:
                    cumulative += entry.get("pnl_cents", 0)
                    curve.append({
                        "date": entry.get("date"),
                        "daily_pnl_cents": entry.get("pnl_cents", 0),
                        "cumulative_pnl_cents": cumulative,
                        "account_balance_cents": entry.get("account_balance_cents"),
                    })

                self._send_json(curve)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)

        elif path == "/api/positions":
            if not self._check_auth():
                return
            risk_state = _read_json(STATE_FILES["risk"], default={})
            positions = risk_state.get("positions", {})
            pending = risk_state.get("pending_orders", {})
            self._send_json({"positions": positions, "pending_orders": pending})

        elif path == "/api/risk":
            if not self._check_auth():
                return
            risk_state = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
            self._send_json({
                "daily_pnl_cents": risk_state.get("daily_pnl_cents", 0),
                "consecutive_losses": risk_state.get("consecutive_losses", 0),
                "kill_switch_until": risk_state.get("kill_switch_until"),
                "total_exposure_cents": risk_state.get("total_exposure_cents", 0),
                "positions_count": len(risk_state.get("positions", {})),
                "pending_count": len(risk_state.get("pending_orders", {})),
            })

        elif path == "/api/pnl":
            if not self._check_auth():
                return
            pnl = _read_json(STATE_FILES["pnl"], default={})
            # Exclude daily_history (large) — use /api/equity-curve for that
            pnl.pop("daily_history", None)
            self._send_json(pnl)

        elif path.startswith("/api/trades"):
            if not self._check_auth():
                return
            from urllib.parse import parse_qs
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)
            page = int(qs.get("page", ["1"])[0])
            per_page = min(int(qs.get("per_page", ["20"])[0]), 100)
            city_filter = qs.get("city", [None])[0]

            trades = _read_json(STATE_FILES["trades"], default=[])
            if not isinstance(trades, list):
                trades = trades.get("trades", [])

            # Filter by city if specified
            if city_filter:
                trades = [t for t in trades if t.get("city_code") == city_filter]

            # Sort latest first
            trades.sort(key=lambda t: t.get("timestamp", ""), reverse=True)

            # Paginate
            start = (page - 1) * per_page
            end = start + per_page
            self._send_json({
                "trades": trades[start:end],
                "total": len(trades),
                "page": page,
                "per_page": per_page,
            })

        else:
            self.send_error(404)

    def do_POST(self):
        if not self._check_auth():
            return
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
            self._send_json({"error": "Pending trades not supported in v4"}, 404)

        elif path == "/api/reject":
            trade_id = payload.get("id")
            if not trade_id:
                self._send_json({"error": "Missing trade id"}, 400)
                return
            self._send_json({"error": "Pending trades not supported in v4"}, 404)

        elif path == "/api/resume":
            # Resume trading: clear observation mode in risk_state.json
            risk = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
            risk["observation_mode"] = False
            risk["observation_reason"] = ""
            risk["consecutive_losses"] = 0
            risk["kill_switch_until"] = None
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
            risk = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
            risk["positions"] = {
                ticker: pos
                for ticker, pos in risk.get("positions", {}).items()
                if ticker not in orphaned_tickers
            }
            risk["pending_orders"] = {
                order_id: order
                for order_id, order in risk.get("pending_orders", {}).items()
                if order.get("ticker") not in orphaned_tickers
            }
            _rebuild_risk_metrics(risk)
            _write_json(STATE_FILES["risk"], risk)

            self._send_json({
                "ok": True,
                "action": "clear_failed",
                "removed": removed_count,
                "remaining": len(kept_trades),
                "removed_tickers": [t.get("ticker") for t in failed_trades],
            })

        elif path == "/api/sync-positions":
            # Consolidate fragmented position entries and remove phantoms
            risk = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
            positions = risk.get("positions", {})
            trades = _read_json(STATE_FILES["trades"], default=[])

            filled_pairs = {
                (t.get("ticker", ""), t.get("side", ""))
                for t in trades
                if _is_executed_buy_fill(t)
            }

            if filled_pairs:
                risk["positions"] = {
                    ticker: pos
                    for ticker, pos in positions.items()
                    if (ticker, pos.get("side", "")) in filled_pairs
                }

            consolidated = list(risk["positions"].values())
            _rebuild_risk_metrics(risk)

            _write_json(STATE_FILES["risk"], risk)
            self._send_json({
                "ok": True,
                "action": "sync_positions",
                "before": len(positions),
                "after": len(consolidated),
                "positions": [{
                    "ticker": p["ticker"],
                    "side": p["side"],
                    "contracts": p["contracts"],
                    "cost_cents": p["cost_cents"],
                } for p in consolidated],
            })

        elif path == "/api/close-position":
            # Record a manual position exit (sold outside the bot)
            # Accepts total_payout_cents (what Kalshi shows as "Total payout")
            ticker = payload.get("ticker")
            total_payout_cents = payload.get("total_payout_cents")
            close_side = payload.get("side", "")  # Optional: disambiguate YES/NO on same ticker
            if not ticker or total_payout_cents is None:
                self._send_json({"error": "Missing ticker or total_payout_cents"}, 400)
                return

            total_payout_cents = int(total_payout_cents)

            # Find the position in risk state
            risk = _normalize_risk_state(_read_json(STATE_FILES["risk"], default={}))
            positions = risk.get("positions", {})
            pos = None
            pos_key = None
            # Prefer exact (ticker, side) match if side was provided
            if close_side:
                for tk, p in positions.items():
                    if p.get("ticker") == ticker and p.get("side") == close_side:
                        pos = p
                        pos_key = tk
                        break
            # Fallback: match by ticker only
            if pos is None:
                for tk, p in positions.items():
                    if p.get("ticker") == ticker:
                        pos = p
                        pos_key = tk
                        break

            if pos is None:
                self._send_json({"error": f"Position {ticker} not found"}, 404)
                return

            contracts = pos.get("contracts", 1)
            cost_cents = pos.get("cost_cents", 0)
            city_code = pos.get("city_code", "")
            side = pos.get("side", "")

            # Calculate realized P&L
            realized_pnl = total_payout_cents - cost_cents

            # Remove position from risk state
            positions.pop(pos_key, None)
            risk["positions"] = positions
            _rebuild_risk_metrics(risk)
            _write_json(STATE_FILES["risk"], risk)

            # NOTE: P&L history is NOT updated here. _sync_pnl_from_kalshi()
            # is the single writer — it rebuilds from Kalshi API each cycle.

            # Add exit record to trade history
            trades = _read_json(STATE_FILES["trades"], default=[])
            trades.append({
                "entry_type": "manual_close",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ticker": ticker,
                "side": side,
                "price_cents": total_payout_cents,
                "contracts": contracts,
                "cost_cents": cost_cents,
                "edge": 0,
                "confidence": 0,
                "city_code": city_code,
                "strategy": "MANUAL_EXIT",
                "status": "closed",
                "settled": True,
                "result": "win" if realized_pnl >= 0 else "loss",
                "profit_cents": realized_pnl,
                "title": pos.get("title", ""),
                "market_description": pos.get("market_description", ""),
            })
            _write_json(STATE_FILES["trades"], trades)

            self._send_json({
                "ok": True,
                "action": "close_position",
                "ticker": ticker,
                "contracts": contracts,
                "cost_cents": cost_cents,
                "total_payout_cents": total_payout_cents,
                "realized_pnl_cents": realized_pnl,
            })

        elif path == "/api/record-manual-trade":
            # Record a trade that was placed manually on Kalshi (not tracked by bot)
            description = payload.get("description", "Manual trade")
            cost_cents = payload.get("cost_cents")
            payout_cents = payload.get("payout_cents")
            if cost_cents is None or payout_cents is None:
                self._send_json({"error": "Missing cost_cents or payout_cents"}, 400)
                return

            cost_cents = int(cost_cents)
            payout_cents = int(payout_cents)
            realized_pnl = payout_cents - cost_cents

            # NOTE: P&L history is NOT updated here. _sync_pnl_from_kalshi()
            # is the single writer — it rebuilds from Kalshi API each cycle.

            # Add to trade history
            trades = _read_json(STATE_FILES["trades"], default=[])
            trades.append({
                "entry_type": "manual_trade",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "ticker": description,
                "side": "",
                "price_cents": payout_cents,
                "contracts": 0,
                "cost_cents": cost_cents,
                "edge": 0,
                "confidence": 0,
                "city_code": "",
                "strategy": "MANUAL_TRADE",
                "status": "closed",
                "settled": True,
                "result": "win" if realized_pnl >= 0 else "loss",
                "profit_cents": realized_pnl,
                "title": description,
                "market_description": description,
            })
            _write_json(STATE_FILES["trades"], trades)

            self._send_json({
                "ok": True,
                "action": "record_manual_trade",
                "description": description,
                "cost_cents": cost_cents,
                "payout_cents": payout_cents,
                "realized_pnl_cents": realized_pnl,
            })

        elif path == "/api/force-exit":
            # Force-exit positions by placing market sell orders on Kalshi.
            # Accepts: {"ticker": "KXHIGH..."} for one position, or {"ticker": "all"} for all.
            # Optional "side" to disambiguate if both YES/NO exist on same ticker.
            if _kalshi_client is None:
                self._send_json({"error": "KalshiClient not initialized yet"}, 503)
                return

            target_ticker = payload.get("ticker")
            target_side = payload.get("side", "")
            if not target_ticker:
                self._send_json({"error": "Missing ticker (use specific ticker or 'all')"}, 400)
                return

            # Get current positions from Kalshi API (source of truth)
            positions_resp = _kalshi_client.get_positions()
            if not positions_resp:
                self._send_json({"error": "Failed to fetch positions from Kalshi"}, 502)
                return

            market_positions = positions_resp.get("market_positions", [])
            # Filter to positions with non-zero count
            open_positions = []
            for mp in market_positions:
                yes_count = mp.get("position", 0)
                no_count = mp.get("total_traded", 0) - mp.get("position", 0)
                # Kalshi returns "position" = net contracts (positive = YES, negative can mean NO)
                # More reliable: check market_exposure or use the position field
                # position > 0 means YES side, position < 0 means NO side
                pos = mp.get("position", 0)
                if pos > 0:
                    open_positions.append({"ticker": mp["ticker"], "side": "yes", "count": pos})
                elif pos < 0:
                    open_positions.append({"ticker": mp["ticker"], "side": "no", "count": abs(pos)})

            if not open_positions:
                self._send_json({"ok": True, "message": "No open positions found", "results": []})
                return

            # Filter to target
            if target_ticker.lower() != "all":
                if target_side:
                    open_positions = [p for p in open_positions
                                      if p["ticker"] == target_ticker and p["side"] == target_side]
                else:
                    open_positions = [p for p in open_positions if p["ticker"] == target_ticker]

            results = []
            for pos in open_positions:
                ticker = pos["ticker"]
                side = pos["side"]
                count = pos["count"]
                try:
                    # Direct API call with error capture (sell_order swallows errors)
                    import requests as req_lib
                    # Kalshi requires a price even for market orders
                    price_key = "yes_price" if side == "yes" else "no_price"
                    order_data = {
                        "ticker": ticker,
                        "action": "sell",
                        "side": side,
                        "count": count,
                        "type": "market",
                        price_key: 1,  # Sell at any price (1c floor = accept worst fill)
                    }
                    headers = _kalshi_client._sign_request("POST", "/portfolio/orders")
                    url = f"{_kalshi_client.base_url}/portfolio/orders"
                    resp = req_lib.post(url, headers=headers, json=order_data, timeout=30)
                    if resp.status_code in (200, 201):
                        data = resp.json()
                        order = data.get("order", data)
                        results.append({
                            "ticker": ticker,
                            "side": side,
                            "count": count,
                            "status": "sold",
                            "order_id": order.get("order_id", ""),
                            "avg_price": order.get("avg_price", 0),
                        })
                    else:
                        results.append({
                            "ticker": ticker,
                            "side": side,
                            "count": count,
                            "status": "error",
                            "http_status": resp.status_code,
                            "detail": resp.text[:300],
                        })
                except Exception as e:
                    results.append({
                        "ticker": ticker,
                        "side": side,
                        "count": count,
                        "status": "error",
                        "detail": str(e),
                    })

            self._send_json({
                "ok": True,
                "action": "force_exit",
                "positions_exited": len([r for r in results if r["status"] == "sold"]),
                "results": results,
            })

        elif path == "/api/fix-trades":
            # Direct trade log update for reconciliation.
            # Accepts: {"trades": [...]} to replace the full trade log.
            new_trades = payload.get("trades")
            if not isinstance(new_trades, list):
                self._send_json({"error": "Missing or invalid 'trades' array"}, 400)
                return
            if len(new_trades) > 5000:
                self._send_json({"error": "Trade array too large (max 5000)"}, 400)
                return
            # Backup old trade log before overwriting
            old_trades = _read_json(STATE_FILES["trades"], default=[])
            old_count = len(old_trades)
            try:
                import shutil
                backup_path = STATE_FILES["trades"] + ".bak"
                if os.path.exists(STATE_FILES["trades"]):
                    shutil.copy2(STATE_FILES["trades"], backup_path)
            except Exception:
                pass
            _write_json(STATE_FILES["trades"], new_trades)
            self._send_json({
                "ok": True,
                "action": "fix_trades",
                "before": old_count,
                "after": len(new_trades),
            })

        elif path == "/api/record-accuracy":
            self._send_json({"error": "Endpoint removed in v4.0"}, 410)

        elif path == "/api/reset":
            # Require explicit confirmation
            if not payload.get("confirm"):
                self._send_json({"error": "Must include 'confirm': true to reset"}, 400)
                return
            # Backup all state files before wiping
            try:
                import shutil
                for key, fpath in STATE_FILES.items():
                    if os.path.exists(fpath):
                        shutil.copy2(fpath, fpath + ".bak")
            except Exception:
                pass
            # Wipe all runtime state files to start fresh
            clean_state = {
                "trades": [],
                "risk": _default_risk_state(),
                "pnl": {
                    "trades": [],
                    "total_invested_cents": 0,
                    "total_returned_cents": 0,
                    "total_profit_cents": 0,
                    "wins": 0,
                    "losses": 0,
                },
                "maker": {},
                "learning": {},
                "bot_status": {},
                "scan_log": [],
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
        self.send_header("Access-Control-Allow-Origin", os.environ.get("CORS_ORIGIN", "*"))
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()


def start_dashboard_server(port=DASHBOARD_PORT):
    """Start the dashboard HTTP server in a daemon background thread.

    Uses a daemon thread so the process exits cleanly when the main
    bot thread stops.  The outer restart loop in kalshi_bot.py ensures
    the bot recovers from crashes — keeping a zombie dashboard alive
    without the trading loop is worse than restarting the whole process.
    """
    if not config.DASHBOARD_TOKEN and os.environ.get("RAILWAY_ENVIRONMENT"):
        print("  [DASHBOARD] WARNING: No DASHBOARD_TOKEN set in production — all endpoints are unprotected!")
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"  [DASHBOARD] Running on port {port}")
    return server
