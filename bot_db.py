"""
SQLite-backed event store for the rebuild path.

The current bot still writes JSON state for compatibility, but this module gives
us a durable append-only ledger for scan cycles, scan decisions, and paper-trade
events. That lets us migrate reporting and learning incrementally instead of
relying on mutable JSON blobs forever.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import config


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _as_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


class BotDatabase:
    def __init__(self, db_path=None):
        self.db_path = db_path or config.BOT_DB_FILE
        self._lock = threading.RLock()
        self._ensure_parent_dir()
        self._conn = self._connect()
        self._init_schema()

    def _ensure_parent_dir(self):
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def close(self):
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def __del__(self):
        self.close()

    def _init_schema(self):
        with self._lock:
            self._conn.executescript(
                """
                    CREATE TABLE IF NOT EXISTS scan_cycles (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        cycle INTEGER NOT NULL,
                        observation_mode INTEGER NOT NULL,
                        markets_scanned INTEGER NOT NULL,
                        signals_found INTEGER NOT NULL,
                        trades_placed INTEGER NOT NULL,
                        diag_null INTEGER NOT NULL,
                        diag_evaluated INTEGER NOT NULL,
                        weather_api_error TEXT NOT NULL,
                        paper_entries INTEGER NOT NULL,
                        paper_filled_pending INTEGER NOT NULL,
                        paper_resting_orders INTEGER NOT NULL,
                        paper_expired_pending INTEGER NOT NULL,
                        settlement_lock_candidates INTEGER NOT NULL,
                        raw_skip_counts_json TEXT NOT NULL,
                        raw_blocked_reasons_json TEXT NOT NULL,
                        top_signals_json TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_scan_cycles_timestamp
                    ON scan_cycles(timestamp);

                    CREATE TABLE IF NOT EXISTS scan_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scan_cycle_id INTEGER NOT NULL,
                        timestamp TEXT NOT NULL,
                        cycle INTEGER NOT NULL,
                        observation_mode INTEGER NOT NULL,
                        ticker TEXT NOT NULL,
                        event_ticker TEXT NOT NULL,
                        city_code TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        signal TEXT NOT NULL,
                        execution_status TEXT NOT NULL,
                        side TEXT NOT NULL,
                        skip_reason TEXT NOT NULL,
                        strategy TEXT NOT NULL,
                        execution_style TEXT NOT NULL,
                        edge REAL NOT NULL,
                        fee_adjusted_edge REAL NOT NULL,
                        our_prob REAL NOT NULL,
                        market_prob REAL NOT NULL,
                        price_cents INTEGER NOT NULL,
                        entry_price_cents INTEGER NOT NULL,
                        limit_price_cents INTEGER NOT NULL,
                        risk_price_cents INTEGER NOT NULL,
                        yes_price_cents INTEGER NOT NULL,
                        no_price_cents INTEGER NOT NULL,
                        predicted_high REAL,
                        todays_high_snapshot REAL,
                        confirmation_verdict TEXT NOT NULL,
                        market_title TEXT NOT NULL,
                        market_subtitle TEXT NOT NULL,
                        strike_type TEXT NOT NULL,
                        floor_strike REAL,
                        cap_strike REAL,
                        raw_json TEXT NOT NULL,
                        FOREIGN KEY (scan_cycle_id) REFERENCES scan_cycles(id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_scan_decisions_timestamp
                    ON scan_decisions(timestamp);

                    CREATE INDEX IF NOT EXISTS idx_scan_decisions_ticker
                    ON scan_decisions(ticker, timestamp);

                    CREATE TABLE IF NOT EXISTS paper_trade_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        ticker TEXT NOT NULL,
                        side TEXT NOT NULL,
                        contracts INTEGER NOT NULL,
                        strategy TEXT NOT NULL,
                        target_date TEXT NOT NULL,
                        cycle INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        limit_price_cents INTEGER NOT NULL,
                        current_price_cents INTEGER NOT NULL,
                        entry_price_cents INTEGER NOT NULL,
                        reserved_cost_cents INTEGER NOT NULL,
                        cost_cents INTEGER NOT NULL,
                        gross_profit_cents INTEGER NOT NULL,
                        net_profit_cents INTEGER NOT NULL,
                        market_result TEXT NOT NULL,
                        confirmation_verdict TEXT NOT NULL,
                        raw_json TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_paper_trade_events_timestamp
                    ON paper_trade_events(timestamp);

                    CREATE INDEX IF NOT EXISTS idx_paper_trade_events_ticker
                    ON paper_trade_events(ticker, timestamp);
                """
            )
            self._conn.commit()

    def record_scan_cycle(self, event, decisions):
        event = dict(event or {})
        decision_rows = [dict(row) for row in (decisions or [])]
        with self._lock:
            cursor = self._conn.execute(
                """
                    INSERT INTO scan_cycles (
                        timestamp, cycle, observation_mode, markets_scanned,
                        signals_found, trades_placed, diag_null, diag_evaluated,
                        weather_api_error, paper_entries, paper_filled_pending,
                        paper_resting_orders, paper_expired_pending,
                        settlement_lock_candidates, raw_skip_counts_json,
                        raw_blocked_reasons_json, top_signals_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                        event.get("timestamp", _utc_now_iso()),
                        _as_int(event.get("cycle", 0)),
                        1 if event.get("observation_mode") else 0,
                        _as_int(event.get("markets_scanned", 0)),
                        _as_int(event.get("signals_found", 0)),
                        _as_int(event.get("trades_placed", 0)),
                        _as_int(event.get("diag_null", 0)),
                        _as_int(event.get("diag_evaluated", 0)),
                        str(event.get("weather_api_error", "") or ""),
                        _as_int(event.get("paper_entries", 0)),
                        _as_int(event.get("paper_filled_pending", 0)),
                        _as_int(event.get("paper_resting_orders", 0)),
                        _as_int(event.get("paper_expired_pending", 0)),
                        _as_int(event.get("settlement_lock_candidates", 0)),
                        json.dumps(event.get("diag_skips", {}) or {}, separators=(",", ":")),
                        json.dumps(event.get("paper_blocked_reasons", {}) or {}, separators=(",", ":")),
                        json.dumps(event.get("top_signals", []) or [], separators=(",", ":")),
                ),
            )
            scan_cycle_id = cursor.lastrowid

            if decision_rows:
                self._conn.executemany(
                    """
                        INSERT INTO scan_decisions (
                            scan_cycle_id, timestamp, cycle, observation_mode,
                            ticker, event_ticker, city_code, target_date, signal,
                            execution_status, side, skip_reason, strategy,
                            execution_style, edge, fee_adjusted_edge, our_prob,
                            market_prob, price_cents, entry_price_cents,
                            limit_price_cents, risk_price_cents, yes_price_cents,
                            no_price_cents, predicted_high, todays_high_snapshot,
                            confirmation_verdict, market_title, market_subtitle,
                            strike_type, floor_strike, cap_strike, raw_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                    [
                            (
                                scan_cycle_id,
                                row.get("timestamp", event.get("timestamp", _utc_now_iso())),
                                _as_int(row.get("cycle", event.get("cycle", 0))),
                                1 if row.get("observation_mode", event.get("observation_mode")) else 0,
                                str(row.get("ticker", "") or ""),
                                str(row.get("event_ticker", "") or ""),
                                str(row.get("city_code", "") or ""),
                                str(row.get("target_date", "") or ""),
                                str(row.get("signal", "skip") or "skip"),
                                str(row.get("execution_status", "") or ""),
                                str(row.get("side", "") or ""),
                                str(row.get("skip_reason", "") or ""),
                                str(row.get("strategy", "") or ""),
                                str(row.get("execution_style", "") or ""),
                                float(row.get("edge", 0) or 0),
                                float(row.get("fee_adjusted_edge", 0) or 0),
                                float(row.get("our_prob", 0) or 0),
                                float(row.get("market_prob", 0) or 0),
                                _as_int(row.get("price_cents", 0)),
                                _as_int(row.get("entry_price_cents", 0)),
                                _as_int(row.get("limit_price_cents", 0)),
                                _as_int(row.get("risk_price_cents", 0)),
                                _as_int(row.get("yes_price_cents", 0)),
                                _as_int(row.get("no_price_cents", 0)),
                                row.get("predicted_high"),
                                row.get("todays_high_snapshot"),
                                str(row.get("confirmation_verdict", "") or ""),
                                str(row.get("market_title", "") or ""),
                                str(row.get("market_subtitle", "") or ""),
                                str(row.get("strike_type", "") or ""),
                                row.get("floor_strike"),
                                row.get("cap_strike"),
                                json.dumps(row, separators=(",", ":"), default=str),
                            )
                        for row in decision_rows
                    ],
                )
            self._conn.commit()

    def record_paper_event(self, event):
        row = dict(event or {})
        with self._lock:
            self._conn.execute(
                """
                    INSERT INTO paper_trade_events (
                        timestamp, event_type, ticker, side, contracts, strategy,
                        target_date, cycle, status, limit_price_cents,
                        current_price_cents, entry_price_cents, reserved_cost_cents,
                        cost_cents, gross_profit_cents, net_profit_cents,
                        market_result, confirmation_verdict, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                (
                        row.get("timestamp", _utc_now_iso()),
                        str(row.get("event_type", "") or ""),
                        str(row.get("ticker", "") or ""),
                        str(row.get("side", "") or ""),
                        _as_int(row.get("contracts", 0)),
                        str(row.get("strategy", "") or ""),
                        str(row.get("target_date", "") or ""),
                        _as_int(row.get("cycle", 0)),
                        str(row.get("status", "") or ""),
                        _as_int(row.get("limit_price_cents", 0)),
                        _as_int(row.get("current_price_cents", 0)),
                        _as_int(row.get("entry_price_cents", 0)),
                        _as_int(row.get("reserved_cost_cents", 0)),
                        _as_int(row.get("cost_cents", 0)),
                        _as_int(row.get("gross_profit_cents", 0)),
                        _as_int(row.get("net_profit_cents", 0)),
                        str(row.get("market_result", "") or ""),
                        str(row.get("confirmation_verdict", "") or ""),
                        json.dumps(row, separators=(",", ":"), default=str),
                ),
            )
            self._conn.commit()

    def fetch_recent_events(self, hours=72, max_rows=5000):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, _as_int(hours)))).isoformat()
        limit = max(1, _as_int(max_rows))
        with self._lock:
            scan_rows = self._conn.execute(
                """
                    SELECT *
                    FROM scan_cycles
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
            paper_rows = self._conn.execute(
                """
                    SELECT *
                    FROM paper_trade_events
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()

        events = [self._row_to_scan_event(row) for row in reversed(scan_rows)]
        events.extend(self._row_to_paper_event(row) for row in reversed(paper_rows))
        events.sort(key=lambda row: row.get("timestamp", ""))
        if len(events) > limit:
            events = events[-limit:]
        return events

    def fetch_recent_decisions(self, hours=72, max_rows=5000):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, _as_int(hours)))).isoformat()
        limit = max(1, _as_int(max_rows))
        with self._lock:
            rows = self._conn.execute(
                """
                    SELECT *
                    FROM scan_decisions
                    WHERE timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                """,
                (cutoff, limit),
            ).fetchall()
        return [self._row_to_decision(row) for row in reversed(rows)]

    def fetch_scan_decisions_by_cycle(self, cycle, limit=500):
        cycle = _as_int(cycle)
        limit = max(1, _as_int(limit))
        with self._lock:
            rows = self._conn.execute(
                """
                    SELECT *
                    FROM scan_decisions
                    WHERE cycle = ?
                    ORDER BY timestamp ASC, id ASC
                    LIMIT ?
                """,
                (cycle, limit),
            ).fetchall()
        return [self._row_to_decision(row) for row in rows]

    def fetch_strategy_daily_pnl(self, hours=336):
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, _as_int(hours)))).isoformat()
        with self._lock:
            rows = self._conn.execute(
                """
                    SELECT
                        strategy,
                        substr(timestamp, 1, 10) AS trade_date,
                        SUM(net_profit_cents) AS pnl_cents,
                        SUM(CASE WHEN net_profit_cents > 0 THEN 1 ELSE 0 END) AS wins,
                        SUM(CASE WHEN net_profit_cents < 0 THEN 1 ELSE 0 END) AS losses
                    FROM paper_trade_events
                    WHERE event_type = 'paper_trade_resolved'
                      AND timestamp >= ?
                    GROUP BY strategy, trade_date
                    ORDER BY strategy ASC, trade_date ASC
                """,
                (cutoff,),
            ).fetchall()
        return [
            {
                "strategy": row["strategy"],
                "date": row["trade_date"],
                "pnl_cents": _as_int(row["pnl_cents"]),
                "wins": _as_int(row["wins"]),
                "losses": _as_int(row["losses"]),
            }
            for row in rows
        ]

    @staticmethod
    def _row_to_scan_event(row):
        return {
            "timestamp": row["timestamp"],
            "event_type": "scan_cycle",
            "cycle": row["cycle"],
            "observation_mode": bool(row["observation_mode"]),
            "markets_scanned": row["markets_scanned"],
            "signals_found": row["signals_found"],
            "trades_placed": row["trades_placed"],
            "diag_null": row["diag_null"],
            "diag_evaluated": row["diag_evaluated"],
            "diag_skips": json.loads(row["raw_skip_counts_json"] or "{}"),
            "weather_api_error": row["weather_api_error"] or "",
            "paper_entries": row["paper_entries"],
            "paper_filled_pending": row["paper_filled_pending"],
            "paper_resting_orders": row["paper_resting_orders"],
            "paper_expired_pending": row["paper_expired_pending"],
            "paper_blocked_reasons": json.loads(row["raw_blocked_reasons_json"] or "{}"),
            "settlement_lock_candidates": row["settlement_lock_candidates"],
            "top_signals": json.loads(row["top_signals_json"] or "[]"),
        }

    @staticmethod
    def _row_to_paper_event(row):
        base = json.loads(row["raw_json"] or "{}")
        if not isinstance(base, dict):
            base = {}
        base.update({
            "timestamp": row["timestamp"],
            "event_type": row["event_type"],
            "ticker": row["ticker"],
            "side": row["side"],
            "contracts": row["contracts"],
            "strategy": row["strategy"],
            "target_date": row["target_date"],
            "cycle": row["cycle"],
            "status": row["status"],
            "limit_price_cents": row["limit_price_cents"],
            "current_price_cents": row["current_price_cents"],
            "entry_price_cents": row["entry_price_cents"],
            "reserved_cost_cents": row["reserved_cost_cents"],
            "cost_cents": row["cost_cents"],
            "gross_profit_cents": row["gross_profit_cents"],
            "net_profit_cents": row["net_profit_cents"],
            "market_result": row["market_result"],
            "confirmation_verdict": row["confirmation_verdict"],
        })
        return base

    @staticmethod
    def _row_to_decision(row):
        base = json.loads(row["raw_json"] or "{}")
        if not isinstance(base, dict):
            base = {}
        base.update({
            "timestamp": row["timestamp"],
            "cycle": row["cycle"],
            "observation_mode": bool(row["observation_mode"]),
            "ticker": row["ticker"],
            "event_ticker": row["event_ticker"],
            "city_code": row["city_code"],
            "target_date": row["target_date"],
            "signal": row["signal"],
            "execution_status": row["execution_status"],
            "side": row["side"],
            "skip_reason": row["skip_reason"] or None,
            "strategy": row["strategy"],
            "execution_style": row["execution_style"],
            "edge": row["edge"],
            "fee_adjusted_edge": row["fee_adjusted_edge"],
            "our_prob": row["our_prob"],
            "market_prob": row["market_prob"],
            "price_cents": row["price_cents"],
            "entry_price_cents": row["entry_price_cents"],
            "limit_price_cents": row["limit_price_cents"],
            "risk_price_cents": row["risk_price_cents"],
            "yes_price_cents": row["yes_price_cents"],
            "no_price_cents": row["no_price_cents"],
            "predicted_high": row["predicted_high"],
            "todays_high_snapshot": row["todays_high_snapshot"],
            "confirmation_verdict": row["confirmation_verdict"],
            "market_title": row["market_title"],
            "market_subtitle": row["market_subtitle"],
            "strike_type": row["strike_type"],
            "floor_strike": row["floor_strike"],
            "cap_strike": row["cap_strike"],
        })
        return base
