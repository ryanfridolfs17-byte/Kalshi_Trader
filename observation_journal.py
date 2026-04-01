"""
Append-only observation telemetry for scan decisions and paper-trade events.

This keeps a durable audit trail while the bot is in observation mode so we can
review what the bot saw, what it would have done, and how those hypothetical
trades resolved afterward.
"""

import json
import os
from collections import deque
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot_db import BotDatabase
import config

MARKET_TZ = ZoneInfo("America/New_York")


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _as_int(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _as_float(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _summary_day_start_utc(day_str):
    try:
        local_midnight = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=MARKET_TZ)
        return local_midnight.astimezone(timezone.utc)
    except Exception:
        return None


class ObservationJournal:
    def __init__(
        self,
        events_file=None,
        decisions_file=None,
        daily_summary_file=None,
        db_path=None,
        recent_events_file=None,
        recent_decisions_file=None,
        recent_cache_file=None,
    ):
        self.events_file = events_file or config.OBSERVATION_EVENTS_FILE
        self.decisions_file = decisions_file or config.SCAN_DECISIONS_FILE
        self.recent_events_file = recent_events_file or config.OBSERVATION_RECENT_EVENTS_FILE
        self.recent_decisions_file = recent_decisions_file or config.OBSERVATION_RECENT_DECISIONS_FILE
        self.recent_cache_file = recent_cache_file or config.OBSERVATION_RECENT_CACHE_FILE
        self.daily_summary_file = daily_summary_file or config.OBSERVATION_DAILY_SUMMARY_FILE
        self.db_path = db_path or config.BOT_DB_FILE
        self.enable_recent_mirrors = bool(getattr(config, "OBSERVATION_ENABLE_RECENT_MIRRORS", True))
        self.max_events_file_bytes = max(
            512 * 1024,
            _as_int(getattr(config, "OBSERVATION_MAX_EVENTS_FILE_BYTES", 16 * 1024 * 1024) or 0),
        )
        self.max_decisions_file_bytes = max(
            2 * 1024 * 1024,
            _as_int(getattr(config, "OBSERVATION_MAX_DECISIONS_FILE_BYTES", 96 * 1024 * 1024) or 0),
        )
        self.enable_sqlite = bool(getattr(config, "OBSERVATION_ENABLE_SQLITE", True))
        self.db = BotDatabase(db_path=self.db_path) if self.enable_sqlite else None
        self.retention_days = max(
            7,
            _as_int(getattr(config, "OBSERVATION_SUMMARY_RETENTION_DAYS", 30) or 30),
        )
        self.recent_event_limit = max(
            100,
            _as_int(getattr(config, "OBSERVATION_RECENT_EVENT_LIMIT", 1500) or 1500),
        )
        self.recent_decision_limit = max(
            100,
            _as_int(getattr(config, "OBSERVATION_RECENT_DECISION_LIMIT", 6000) or 6000),
        )
        self._startup_housekeeping()

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass

    def __del__(self):
        self.close()

    def log_scan_cycle(
        self,
        cycle,
        markets_scanned,
        decisions,
        signals_found,
        trades_placed,
        skip_counts=None,
        null_count=0,
        evaluated_count=0,
        weather_error=None,
        observation_mode=False,
        top_signals=None,
        paper_result=None,
        settlement_lock_candidates=0,
        timestamp=None,
    ):
        ts = timestamp or _utc_now_iso()
        paper_result = paper_result or {}
        event = {
            "timestamp": ts,
            "event_type": "scan_cycle",
            "cycle": _as_int(cycle),
            "observation_mode": bool(observation_mode),
            "markets_scanned": _as_int(markets_scanned),
            "signals_found": _as_int(signals_found),
            "trades_placed": _as_int(trades_placed),
            "diag_null": _as_int(null_count),
            "diag_evaluated": _as_int(evaluated_count),
            "diag_skips": dict(skip_counts or {}),
            "weather_api_error": weather_error or "",
            "paper_entries": len(paper_result.get("executed", []) or []),
            "paper_filled_pending": len(paper_result.get("filled_pending", []) or []),
            "paper_resting_orders": len(paper_result.get("queued", []) or []),
            "paper_expired_pending": len(paper_result.get("expired_pending", []) or []),
            "paper_blocked_reasons": dict(paper_result.get("blocked_reasons", {}) or {}),
            "settlement_lock_candidates": _as_int(settlement_lock_candidates),
            "top_signals": list(top_signals or [])[:5],
        }
        self._append_jsonl(self.events_file, event)
        self._maybe_trim_canonical_history(
            self.events_file,
            max_bytes=self.max_events_file_bytes,
            max_rows=max(self.recent_event_limit * 8, 5000),
        )
        if self.enable_recent_mirrors:
            self._append_recent_rows(
                self.recent_events_file,
                [event],
                max_rows=self.recent_event_limit,
                max_bytes=self._history_byte_budget(self.recent_events_file, self.recent_event_limit),
            )

        decision_rows = []
        for decision in decisions or []:
            decision_row = self._build_decision_row(
                decision=decision,
                cycle=cycle,
                timestamp=ts,
                observation_mode=observation_mode,
            )
            decision_rows.append(decision_row)
        self._append_many_jsonl(self.decisions_file, decision_rows)
        self._maybe_trim_canonical_history(
            self.decisions_file,
            max_bytes=self.max_decisions_file_bytes,
            max_rows=max(self.recent_decision_limit * 12, 50000),
        )
        if self.enable_recent_mirrors:
            self._append_recent_rows(
                self.recent_decisions_file,
                decision_rows,
                max_rows=self.recent_decision_limit,
                max_bytes=self._history_byte_budget(self.recent_decisions_file, self.recent_decision_limit),
            )

        if self.db is not None:
            try:
                self.db.record_scan_cycle(event, decision_rows)
            except Exception:
                pass

        self._update_daily_summary(scan_event=event, decision_rows=decision_rows)
        if self.enable_recent_mirrors:
            self._update_recent_cache(scan_event=event, decision_rows=decision_rows)
        return event

    def log_paper_event(self, event_type, payload=None, timestamp=None):
        payload = payload or {}
        row = {
            "timestamp": timestamp or payload.get("resolved_at") or payload.get("placed_at") or payload.get("opened_at") or _utc_now_iso(),
            "event_type": event_type,
            "ticker": payload.get("ticker", ""),
            "side": payload.get("side", ""),
            "contracts": _as_int(payload.get("contracts", 0)),
            "strategy": payload.get("strategy", ""),
            "target_date": payload.get("target_date", ""),
            "cycle": _as_int(payload.get("cycle", 0)),
            "status": payload.get("status", ""),
            "limit_price_cents": _as_int(payload.get("limit_price_cents", 0)),
            "current_price_cents": _as_int(payload.get("current_price_cents", 0)),
            "entry_price_cents": _as_int(payload.get("entry_price_cents", 0)),
            "reserved_cost_cents": _as_int(payload.get("reserved_cost_cents", 0)),
            "cost_cents": _as_int(payload.get("cost_cents", 0)),
            "gross_profit_cents": _as_int(payload.get("gross_profit_cents", 0)),
            "net_profit_cents": _as_int(payload.get("net_profit_cents", 0)),
            "market_result": payload.get("market_result", ""),
            "confirmation_verdict": payload.get("confirmation_verdict", ""),
        }
        self._append_jsonl(self.events_file, row)
        self._maybe_trim_canonical_history(
            self.events_file,
            max_bytes=self.max_events_file_bytes,
            max_rows=max(self.recent_event_limit * 8, 5000),
        )
        if self.enable_recent_mirrors:
            self._append_recent_rows(
                self.recent_events_file,
                [row],
                max_rows=self.recent_event_limit,
                max_bytes=self._history_byte_budget(self.recent_events_file, self.recent_event_limit),
            )
        if self.db is not None:
            try:
                self.db.record_paper_event(row)
            except Exception:
                pass
        self._update_daily_summary(paper_event=row)
        if self.enable_recent_mirrors:
            self._update_recent_cache(paper_event=row)
        return row

    def load_daily_summary(self, days=7):
        summary = self._read_daily_summary()
        rows = list((summary.get("days", {}) or {}).values())
        rows.sort(key=lambda row: row.get("date", ""))
        if days and days > 0:
            rows = rows[-days:]
        return rows

    @staticmethod
    def _history_byte_budget(path, max_rows):
        max_rows = max(1, _as_int(max_rows))
        per_row = 1024 if str(path).endswith(".jsonl") and "decisions" not in str(path) else 2048
        floor = 512 * 1024 if "decisions" in str(path) else 256 * 1024
        ceiling = 16 * 1024 * 1024 if "decisions" in str(path) else 4 * 1024 * 1024
        return min(max(max_rows * per_row, floor), ceiling)

    def _startup_housekeeping(self):
        self._maybe_trim_canonical_history(
            self.events_file,
            max_bytes=self.max_events_file_bytes,
            max_rows=max(self.recent_event_limit * 8, 5000),
        )
        self._maybe_trim_canonical_history(
            self.decisions_file,
            max_bytes=self.max_decisions_file_bytes,
            max_rows=max(self.recent_decision_limit * 12, 50000),
        )
        if not self.enable_recent_mirrors:
            self._delete_file_if_present(self.recent_events_file)
            self._delete_file_if_present(self.recent_decisions_file)
            self._delete_file_if_present(self.recent_cache_file)
        if not self.enable_sqlite:
            self._delete_file_if_present(self.db_path)

    @staticmethod
    def _delete_file_if_present(path):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def get_recent_history(
        self,
        hours=72,
        event_limit=200,
        decision_limit=500,
        include_decisions=False,
        prefer_db=True,
        fast_mode=False,
        cached_only=False,
    ):
        hours = max(1, min(_as_int(hours), 24 * 14))
        event_limit = max(1, min(_as_int(event_limit), 1000))
        decision_limit = max(1, min(_as_int(decision_limit), 5000))
        all_events = []
        all_decisions = []

        if fast_mode:
            events_path = self.recent_events_file if cached_only else self.events_file
            decisions_path = self.recent_decisions_file if cached_only else self.decisions_file
            all_events = self._read_recent_jsonl(
                events_path,
                hours=hours,
                max_rows=event_limit,
                max_bytes=self._history_byte_budget(events_path, event_limit),
            )
            if include_decisions:
                all_decisions = self._read_recent_jsonl(
                    decisions_path,
                    hours=hours,
                    max_rows=decision_limit,
                    max_bytes=self._history_byte_budget(decisions_path, decision_limit),
                )
        else:
            if prefer_db:
                try:
                    all_events = self.db.fetch_recent_events(
                        hours=hours,
                        max_rows=max(event_limit * 4, 1000),
                    )
                    if include_decisions:
                        all_decisions = self.db.fetch_recent_decisions(
                            hours=hours,
                            max_rows=max(decision_limit * 4, 5000),
                        )
                except Exception:
                    all_events = []
                    all_decisions = []
            if not all_events:
                all_events = self._read_recent_jsonl(
                    self.events_file,
                    hours=hours,
                    max_rows=max(event_limit * 4, 1000),
                    max_bytes=self._history_byte_budget(self.events_file, max(event_limit * 4, 1000)),
                )
            if include_decisions and not all_decisions:
                all_decisions = self._read_recent_jsonl(
                    self.decisions_file,
                    hours=hours,
                    max_rows=max(decision_limit * 4, 5000),
                    max_bytes=self._history_byte_budget(self.decisions_file, max(decision_limit * 4, 5000)),
                )

        recent_events = all_events[-event_limit:]
        recent_decisions = all_decisions[-decision_limit:] if include_decisions else []

        daily_rows = []
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for row in self.load_daily_summary(days=self.retention_days):
            day_dt = _summary_day_start_utc(row.get("date", ""))
            if day_dt and day_dt >= cutoff - timedelta(days=1):
                daily_rows.append(row)

        return {
            "generated_at": _utc_now_iso(),
            "hours": hours,
            "summary": self._summarize_recent(
                recent_events if fast_mode else all_events,
                recent_decisions if fast_mode else all_decisions,
            ),
            "daily_summary": daily_rows,
            "recent_events": recent_events,
            "recent_decisions": recent_decisions if include_decisions else [],
        }

    def get_cached_history(
        self,
        hours=72,
        event_limit=200,
        decision_limit=500,
        include_decisions=False,
    ):
        hours = max(1, min(_as_int(hours), 24 * 14))
        event_limit = max(1, min(_as_int(event_limit), 1000))
        decision_limit = max(1, min(_as_int(decision_limit), 5000))
        if not self.enable_recent_mirrors:
            payload = self.get_recent_history(
                hours=hours,
                event_limit=event_limit,
                decision_limit=decision_limit,
                include_decisions=True,
                prefer_db=False,
                fast_mode=True,
                cached_only=False,
            )
            if not include_decisions:
                payload["recent_decisions"] = []
            return payload

        cache = self._read_recent_cache()
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

        def _within_hours(row):
            ts = _parse_timestamp((row or {}).get("timestamp"))
            return ts is not None and ts >= cutoff

        filtered_events = [row for row in (cache.get("recent_events", []) or []) if _within_hours(row)]
        filtered_decisions = [row for row in (cache.get("recent_decisions", []) or []) if _within_hours(row)]
        recent_events = filtered_events[-event_limit:]
        recent_decisions = filtered_decisions[-decision_limit:] if include_decisions else []

        daily_rows = []
        for row in self.load_daily_summary(days=self.retention_days):
            day_dt = _summary_day_start_utc(row.get("date", ""))
            if day_dt and day_dt >= cutoff - timedelta(days=1):
                daily_rows.append(row)

        return {
            "generated_at": _utc_now_iso(),
            "hours": hours,
            # Public history should still report decision counts even when the raw
            # decision rows are omitted from the response body.
            "summary": self._summarize_recent(filtered_events, filtered_decisions),
            "daily_summary": daily_rows,
            "recent_events": recent_events,
            "recent_decisions": recent_decisions if include_decisions else [],
        }

    def _summarize_recent(self, events, decisions):
        skip_reasons = Counter()
        blocked_reasons = Counter()
        event_counts = Counter()
        summary = {
            "scan_cycles": 0,
            "markets_scanned": 0,
            "signals_found": 0,
            "trades_placed_live": 0,
            "diag_null": 0,
            "diag_evaluated": 0,
            "weather_errors": 0,
            "paper_entries": 0,
            "paper_filled_pending": 0,
            "paper_resting_orders": 0,
            "paper_expired_pending": 0,
            "paper_resolved": 0,
            "paper_wins": 0,
            "paper_losses": 0,
            "paper_net_profit_cents": 0,
            "settlement_lock_candidates": 0,
            "decision_rows": len(decisions or []),
            "buy_decisions": 0,
            "skip_decisions": 0,
            "event_counts": {},
            "skip_reasons": {},
            "paper_blocked_reasons": {},
        }

        for event in events or []:
            event_type = event.get("event_type", "unknown")
            event_counts[event_type] += 1
            if event_type == "scan_cycle":
                summary["scan_cycles"] += 1
                summary["markets_scanned"] += _as_int(event.get("markets_scanned", 0))
                summary["signals_found"] += _as_int(event.get("signals_found", 0))
                summary["trades_placed_live"] += _as_int(event.get("trades_placed", 0))
                summary["diag_null"] += _as_int(event.get("diag_null", 0))
                summary["diag_evaluated"] += _as_int(event.get("diag_evaluated", 0))
                summary["paper_entries"] += _as_int(event.get("paper_entries", 0))
                summary["paper_filled_pending"] += _as_int(event.get("paper_filled_pending", 0))
                summary["paper_resting_orders"] += _as_int(event.get("paper_resting_orders", 0))
                summary["paper_expired_pending"] += _as_int(event.get("paper_expired_pending", 0))
                summary["settlement_lock_candidates"] += _as_int(event.get("settlement_lock_candidates", 0))
                if event.get("weather_api_error"):
                    summary["weather_errors"] += 1
                skip_reasons.update(event.get("diag_skips", {}) or {})
                blocked_reasons.update(event.get("paper_blocked_reasons", {}) or {})
            elif event_type == "paper_trade_resolved":
                summary["paper_resolved"] += 1
                summary["paper_net_profit_cents"] += _as_int(event.get("net_profit_cents", 0))
                if event.get("status") == "win":
                    summary["paper_wins"] += 1
                elif event.get("status") == "loss":
                    summary["paper_losses"] += 1

        for decision in decisions or []:
            if decision.get("signal") == "buy":
                summary["buy_decisions"] += 1
            else:
                summary["skip_decisions"] += 1

        summary["event_counts"] = dict(event_counts)
        summary["skip_reasons"] = dict(skip_reasons)
        summary["paper_blocked_reasons"] = dict(blocked_reasons)
        return summary

    def _build_decision_row(self, decision, cycle, timestamp, observation_mode):
        return {
            "timestamp": timestamp,
            "cycle": _as_int(cycle),
            "observation_mode": bool(observation_mode),
            "ticker": decision.get("ticker", ""),
            "event_ticker": decision.get("event_ticker", ""),
            "city_code": decision.get("city_code", ""),
            "target_date": decision.get("target_date", ""),
            "signal": decision.get("signal", "skip"),
            "execution_status": decision.get("execution_status", ""),
            "side": decision.get("side", ""),
            "skip_reason": decision.get("skip_reason"),
            "strategy": decision.get("strategy", ""),
            "execution_style": decision.get("execution_style", ""),
            "edge": round(_as_float(decision.get("edge", 0)), 4),
            "fee_adjusted_edge": round(_as_float(decision.get("fee_adjusted_edge", 0)), 4),
            "our_prob": round(_as_float(decision.get("our_prob", 0)), 4),
            "market_prob": round(_as_float(decision.get("market_prob", 0)), 4),
            "price_cents": _as_int(decision.get("price_cents", 0)),
            "entry_price_cents": _as_int(decision.get("entry_price_cents", 0)),
            "limit_price_cents": _as_int(decision.get("limit_price", 0)),
            "risk_price_cents": _as_int(decision.get("risk_price_cents", 0)),
            "yes_price_cents": _as_int(decision.get("yes_price_cents", 0)),
            "no_price_cents": _as_int(decision.get("no_price_cents", 0)),
            "predicted_high": decision.get("predicted_high"),
            "todays_high_snapshot": decision.get("todays_high_snapshot"),
            "confirmation_verdict": decision.get("confirmation_verdict", ""),
            "market_title": decision.get("market_title", ""),
            "market_subtitle": decision.get("market_subtitle", ""),
            "strike_type": decision.get("strike_type", ""),
            "floor_strike": decision.get("floor_strike"),
            "cap_strike": decision.get("cap_strike"),
        }

    def _read_daily_summary(self):
        try:
            if os.path.exists(self.daily_summary_file):
                with open(self.daily_summary_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                    if isinstance(data, dict):
                        data.setdefault("days", {})
                        normalized = {}
                        for day_str, row in (data.get("days", {}) or {}).items():
                            base = self._blank_day(day_str)
                            if isinstance(row, dict):
                                base.update(row)
                            normalized[day_str] = base
                        data["days"] = normalized
                        return data
        except Exception:
            pass
        return {"updated_at": "", "days": {}}

    def _blank_day(self, day_str):
        return {
            "date": day_str,
            "scan_cycles": 0,
            "markets_scanned": 0,
            "signals_found": 0,
            "trades_placed_live": 0,
            "diag_null": 0,
            "diag_evaluated": 0,
            "weather_errors": 0,
            "decision_rows": 0,
            "buy_decisions": 0,
            "skip_decisions": 0,
            "paper_entries": 0,
            "paper_filled_pending": 0,
            "paper_resting_orders": 0,
            "paper_expired_pending": 0,
            "paper_resolved": 0,
            "paper_wins": 0,
            "paper_losses": 0,
            "paper_net_profit_cents": 0,
            "settlement_lock_candidates": 0,
            "skip_reasons": {},
            "paper_blocked_reasons": {},
        }

    def _update_daily_summary(self, scan_event=None, decision_rows=None, paper_event=None):
        ts_value = ""
        if scan_event:
            ts_value = scan_event.get("timestamp", "")
        elif paper_event:
            ts_value = paper_event.get("timestamp", "")
        day_dt = _parse_timestamp(ts_value)
        if day_dt is None:
            day_dt = datetime.now(timezone.utc)
        day_str = day_dt.astimezone(MARKET_TZ).strftime("%Y-%m-%d")

        summary = self._read_daily_summary()
        days = summary.setdefault("days", {})
        row = self._blank_day(day_str)
        existing = days.get(day_str, {})
        if isinstance(existing, dict):
            row.update(existing)

        if scan_event:
            row["scan_cycles"] += 1
            row["markets_scanned"] += _as_int(scan_event.get("markets_scanned", 0))
            row["signals_found"] += _as_int(scan_event.get("signals_found", 0))
            row["trades_placed_live"] += _as_int(scan_event.get("trades_placed", 0))
            row["diag_null"] += _as_int(scan_event.get("diag_null", 0))
            row["diag_evaluated"] += _as_int(scan_event.get("diag_evaluated", 0))
            row["decision_rows"] += len(decision_rows or [])
            row["buy_decisions"] += sum(1 for decision in (decision_rows or []) if decision.get("signal") == "buy")
            row["skip_decisions"] += sum(1 for decision in (decision_rows or []) if decision.get("signal") != "buy")
            row["paper_entries"] += _as_int(scan_event.get("paper_entries", 0))
            row["paper_filled_pending"] += _as_int(scan_event.get("paper_filled_pending", 0))
            row["paper_resting_orders"] += _as_int(scan_event.get("paper_resting_orders", 0))
            row["paper_expired_pending"] += _as_int(scan_event.get("paper_expired_pending", 0))
            row["settlement_lock_candidates"] += _as_int(scan_event.get("settlement_lock_candidates", 0))
            if scan_event.get("weather_api_error"):
                row["weather_errors"] += 1
            self._merge_counter(row["skip_reasons"], scan_event.get("diag_skips", {}) or {})
            self._merge_counter(row["paper_blocked_reasons"], scan_event.get("paper_blocked_reasons", {}) or {})

        if paper_event and paper_event.get("event_type") == "paper_trade_resolved":
            row["paper_resolved"] += 1
            row["paper_net_profit_cents"] += _as_int(paper_event.get("net_profit_cents", 0))
            if paper_event.get("status") == "win":
                row["paper_wins"] += 1
            elif paper_event.get("status") == "loss":
                row["paper_losses"] += 1

        days[day_str] = row
        summary["updated_at"] = _utc_now_iso()

        all_days = sorted(days.keys())
        while len(all_days) > self.retention_days:
            oldest = all_days.pop(0)
            days.pop(oldest, None)

        try:
            config.atomic_json_save(self.daily_summary_file, summary)
        except Exception:
            pass

    @staticmethod
    def _merge_counter(target, incoming):
        for key, value in (incoming or {}).items():
            target[key] = _as_int(target.get(key, 0)) + _as_int(value)

    def _blank_recent_cache(self):
        return {
            "updated_at": "",
            "recent_events": [],
            "recent_decisions": [],
        }

    def _read_recent_cache(self):
        try:
            if os.path.exists(self.recent_cache_file):
                with open(self.recent_cache_file, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
                    if isinstance(data, dict):
                        data.setdefault("updated_at", "")
                        data.setdefault("recent_events", [])
                        data.setdefault("recent_decisions", [])
                        return data
        except Exception:
            pass
        return self._blank_recent_cache()

    def _trim_recent_rows(self, rows, max_rows):
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.retention_days)
        kept = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ts = _parse_timestamp(row.get("timestamp"))
            if ts is None:
                continue
            if ts < cutoff:
                continue
            kept.append(row)
        if max_rows and len(kept) > max_rows:
            kept = kept[-max_rows:]
        return kept

    def _update_recent_cache(self, scan_event=None, decision_rows=None, paper_event=None):
        cache = self._read_recent_cache()
        if scan_event:
            cache["recent_events"].append(scan_event)
        if decision_rows:
            cache["recent_decisions"].extend([row for row in decision_rows if isinstance(row, dict)])
        if paper_event:
            cache["recent_events"].append(paper_event)

        cache["recent_events"] = self._trim_recent_rows(
            cache.get("recent_events", []),
            self.recent_event_limit,
        )
        cache["recent_decisions"] = self._trim_recent_rows(
            cache.get("recent_decisions", []),
            self.recent_decision_limit,
        )
        cache["updated_at"] = _utc_now_iso()
        try:
            config.atomic_json_save(self.recent_cache_file, cache)
        except Exception:
            pass

    def _append_jsonl(self, path, row):
        self._append_many_jsonl(path, [row])

    def _append_many_jsonl(self, path, rows):
        try:
            rows = [row for row in (rows or []) if isinstance(row, dict)]
            if not rows:
                return
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(row, separators=(",", ":"), default=str))
                    handle.write("\n")
        except Exception:
            pass

    def _rewrite_jsonl(self, path, rows):
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    handle.write(json.dumps(row, separators=(",", ":"), default=str))
                    handle.write("\n")
        except Exception:
            pass

    def _append_recent_rows(self, path, rows, max_rows, max_bytes):
        rows = [row for row in (rows or []) if isinstance(row, dict)]
        if not rows:
            return
        self._append_many_jsonl(path, rows)
        trimmed = self._read_recent_jsonl(
            path,
            hours=self.retention_days * 24,
            max_rows=max_rows,
            max_bytes=max(max_bytes, self._history_byte_budget(path, max_rows)),
        )
        self._rewrite_jsonl(path, trimmed)

    def _maybe_trim_canonical_history(self, path, max_bytes, max_rows):
        try:
            if not os.path.exists(path):
                return
            if os.path.getsize(path) <= max_bytes:
                return
        except Exception:
            return
        trimmed = self._read_recent_jsonl(
            path,
            hours=self.retention_days * 24,
            max_rows=max_rows,
            max_bytes=max_bytes,
        )
        self._rewrite_jsonl(path, trimmed)

    def _tail_lines(self, path, max_bytes):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                file_size = handle.tell()
                if file_size <= 0:
                    return []
                block_size = 65536
                remaining = min(file_size, max(65536, int(max_bytes or 0)))
                chunks = deque()
                while remaining > 0 and handle.tell() > 0:
                    read_size = min(block_size, remaining, handle.tell())
                    handle.seek(-read_size, os.SEEK_CUR)
                    chunk = handle.read(read_size)
                    handle.seek(-read_size, os.SEEK_CUR)
                    chunks.appendleft(chunk)
                    remaining -= read_size
                    if handle.tell() == 0:
                        break
                payload = b"".join(chunks).decode("utf-8", errors="ignore")
                return payload.splitlines()
        except Exception:
            return []

    def _read_recent_jsonl(self, path, hours=72, max_rows=None, max_bytes=None):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max(1, _as_int(hours)))
        rows = deque()
        try:
            if not os.path.exists(path):
                return []
            if max_bytes is None:
                max_bytes = 32 * 1024 * 1024 if path == self.decisions_file else 4 * 1024 * 1024
            lines = self._tail_lines(path, max_bytes=max_bytes)
            if not lines:
                return []
            for raw_line in reversed(lines):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_timestamp(row.get("timestamp"))
                if ts is None:
                    continue
                if ts < cutoff:
                    break
                rows.appendleft(row)
                if max_rows and len(rows) >= int(max_rows):
                    break
        except Exception:
            return []
        return list(rows)
