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
    "analysis": config.TRADE_ANALYSIS_FILE,
    "scan_log": config.SCAN_LOG_FILE,
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
                if key in ("trades", "pending", "reports", "attribution", "analysis", "scan_log"):
                    state[key] = _read_json(filepath, default=[])
                else:
                    state[key] = _read_json(filepath, default={})
            self._send_json(state)

        elif path == "/api/pending":
            self._send_json(_read_json(STATE_FILES["pending"], default=[]))

        elif path == "/api/reports":
            self._send_json(_read_json(STATE_FILES["reports"], default=[]))

        elif path == "/api/fills":
            # Fetch recent fills from Kalshi API (buys + sells)
            if _kalshi_client is None:
                self._send_json({"error": "KalshiClient not initialized"}, 503)
            else:
                result = _kalshi_client.get_fills(limit=200)
                self._send_json(result or {"error": "API call failed"})

        elif path == "/api/balance":
            if _kalshi_client is None:
                self._send_json({"error": "KalshiClient not initialized"}, 503)
            else:
                result = _kalshi_client.get_balance()
                self._send_json(result or {"error": "API call failed"})

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

        elif path == "/api/sync-positions":
            # Consolidate fragmented position entries and remove phantoms
            risk = _read_json(STATE_FILES["risk"], default={})
            positions = risk.get("positions", [])
            trades = _read_json(STATE_FILES["trades"], default=[])

            # Build set of (ticker, side) pairs that have live_filled trades
            filled_pairs = {(t["ticker"], t["side"]) for t in trades if t.get("status") == "live_filled"}

            # Group positions by (ticker, side) and merge
            merged = {}
            for p in positions:
                ticker = p.get("ticker", "")
                side = p.get("side", "")
                # Skip phantom positions with no matching filled trade for this side
                if (ticker, side) not in filled_pairs:
                    continue
                key = (ticker, side)
                if key in merged:
                    merged[key]["contracts"] += p.get("contracts", 1)
                    merged[key]["cost_cents"] += p.get("cost_cents", 0)
                    merged[key]["expected_profit_cents"] = merged[key].get("expected_profit_cents", 0) + p.get("expected_profit_cents", 0)
                else:
                    merged[key] = dict(p)  # copy

            # Also merge trades into positions: count filled trades per (ticker, side)
            trade_totals = {}
            for t in trades:
                if t.get("status") != "live_filled":
                    continue
                key = (t["ticker"], t["side"])
                if key not in trade_totals:
                    trade_totals[key] = {"contracts": 0, "cost_cents": 0}
                trade_totals[key]["contracts"] += t.get("contracts", 1)
                trade_totals[key]["cost_cents"] += t.get("cost_cents", 0)

            # Reconcile: use trade totals as source of truth for contracts/cost
            for key, totals in trade_totals.items():
                if key in merged:
                    merged[key]["contracts"] = totals["contracts"]
                    merged[key]["cost_cents"] = totals["cost_cents"]

            consolidated = list(merged.values())
            risk["positions"] = consolidated

            # Recalculate city exposure
            risk["city_exposure"] = {}
            for p in consolidated:
                city = p.get("city_code", "")
                if city:
                    risk["city_exposure"][city] = risk["city_exposure"].get(city, 0) + p.get("cost_cents", 0)

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
            risk = _read_json(STATE_FILES["risk"], default={})
            positions = risk.get("positions", [])
            pos = None
            pos_idx = None
            # Prefer exact (ticker, side) match if side was provided
            if close_side:
                for i, p in enumerate(positions):
                    if p.get("ticker") == ticker and p.get("side") == close_side:
                        pos = p
                        pos_idx = i
                        break
            # Fallback: match by ticker only
            if pos is None:
                for i, p in enumerate(positions):
                    if p.get("ticker") == ticker:
                        pos = p
                        pos_idx = i
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
            positions.pop(pos_idx)
            risk["positions"] = positions
            # Update city exposure
            if city_code and city_code in risk.get("city_exposure", {}):
                risk["city_exposure"][city_code] = max(
                    0, risk["city_exposure"][city_code] - cost_cents
                )
            risk["total_exposure_cents"] = max(
                0, risk.get("total_exposure_cents", 0) - cost_cents
            )
            _write_json(STATE_FILES["risk"], risk)

            # NOTE: P&L history is NOT updated here. _sync_pnl_from_kalshi()
            # is the single writer — it rebuilds from Kalshi API each cycle.

            # Add exit record to trade history
            trades = _read_json(STATE_FILES["trades"], default=[])
            trades.append({
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
    """Start the dashboard HTTP server in a daemon background thread.

    Uses a daemon thread so the process exits cleanly when the main
    bot thread stops.  The outer restart loop in kalshi_bot.py ensures
    the bot recovers from crashes — keeping a zombie dashboard alive
    without the trading loop is worse than restarting the whole process.
    """
    server = HTTPServer(("0.0.0.0", port), DashboardHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"  [DASHBOARD] Running on port {port}")
    return server
