"""
Backfill the SQLite observation store from older JSON artifacts.

This bridges the gap between the pre-journal learning files and the new
SQLite-backed observation ledger so strategy scorecards can use historical
evidence immediately instead of only learning from post-upgrade cycles.
"""

import argparse
import json
import os
from collections import Counter, defaultdict

import config
from observation_journal import ObservationJournal


def _observation_paths(state_dir=None):
    if state_dir is None:
        return {
            "events": config.OBSERVATION_EVENTS_FILE,
            "decisions": config.SCAN_DECISIONS_FILE,
            "daily_summary": config.OBSERVATION_DAILY_SUMMARY_FILE,
            "db": config.BOT_DB_FILE,
        }
    base = state_dir
    return {
        "events": os.path.join(base, "observation_events.jsonl"),
        "decisions": os.path.join(base, "scan_decisions.jsonl"),
        "daily_summary": os.path.join(base, "observation_daily_summary.json"),
        "db": os.path.join(base, "bot_data.sqlite3"),
    }


def _legacy_paths(state_dir=None):
    if state_dir is None:
        return {
            "learning_state": config.LEARNING_STATE_FILE,
            "trade_log": config.TRADE_LOG_FILE,
            "paper_locks": config.PAPER_LOCKS_FILE,
        }
    base = state_dir
    return {
        "learning_state": os.path.join(base, "learning_state.json"),
        "trade_log": os.path.join(base, "trade_history.json"),
        "paper_locks": os.path.join(base, "paper_locks.json"),
    }


def _read_json(path, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
    except Exception:
        pass
    return default


def _event_ticker_from_ticker(ticker):
    if not ticker or "-" not in ticker:
        return ticker or ""
    head, tail = ticker.rsplit("-", 1)
    if tail.startswith(("B", "T")):
        return head
    return ticker


def _yes_no_prices(side, price_cents):
    price_cents = int(price_cents or 0)
    if side == "yes" and price_cents > 0:
        return price_cents, max(0, 100 - price_cents)
    if side == "no" and price_cents > 0:
        return max(0, 100 - price_cents), price_cents
    return 0, 0


def _decision_from_snapshot(row):
    side = row.get("side", "")
    price_cents = int(row.get("price_cents", 0) or 0)
    yes_price, no_price = _yes_no_prices(side, price_cents)
    return {
        "timestamp": row.get("timestamp", ""),
        "ticker": row.get("ticker", ""),
        "event_ticker": _event_ticker_from_ticker(row.get("ticker", "")),
        "city_code": row.get("city_code", ""),
        "target_date": row.get("target_date", ""),
        "signal": row.get("signal", "skip"),
        "execution_status": row.get("execution_status", ""),
        "side": side,
        "skip_reason": row.get("skip_reason", ""),
        "strategy": row.get("strategy", "") or "S1-Weather",
        "execution_style": row.get("execution_style", ""),
        "edge": row.get("edge", 0),
        "fee_adjusted_edge": row.get("fee_adjusted_edge", 0),
        "our_prob": row.get("our_prob", 0),
        "market_prob": row.get("market_prob", 0),
        "price_cents": price_cents,
        "entry_price_cents": int(row.get("entry_price_cents", price_cents) or price_cents),
        "limit_price_cents": int(row.get("limit_price_cents", row.get("limit_price", 0)) or 0),
        "risk_price_cents": int(row.get("risk_price_cents", price_cents) or price_cents),
        "yes_price_cents": int(row.get("yes_price_cents", yes_price) or yes_price),
        "no_price_cents": int(row.get("no_price_cents", no_price) or no_price),
        "predicted_high": row.get("predicted_high", row.get("forecast_mean")),
        "todays_high_snapshot": row.get("todays_high_snapshot"),
        "confirmation_verdict": row.get("confirmation_verdict", row.get("confirmation", "")),
        "market_title": row.get("title", row.get("market_title", "")),
        "market_subtitle": row.get("subtitle", row.get("market_subtitle", "")),
        "strike_type": row.get("strike_type", ""),
        "floor_strike": row.get("floor_strike"),
        "cap_strike": row.get("cap_strike"),
        "source": "legacy_scan_snapshot",
    }


def _decision_from_lock_row(row):
    signal = row.get("signal", "skip")
    price_cents = int(row.get("price_cents", 0) or 0)
    side = row.get("lock_side", row.get("side_seen", ""))
    yes_price, no_price = _yes_no_prices(side, price_cents)
    return {
        "timestamp": row.get("timestamp", ""),
        "ticker": row.get("ticker", ""),
        "event_ticker": _event_ticker_from_ticker(row.get("ticker", "")),
        "city_code": row.get("city_code", ""),
        "target_date": row.get("target_date", ""),
        "signal": signal,
        "execution_status": "retrospective_lock_candidate",
        "side": side,
        "skip_reason": row.get("skip_reason", row.get("score_block_reason", "")),
        "strategy": "S3-SettlementLock",
        "execution_style": "",
        "edge": 0,
        "fee_adjusted_edge": 0,
        "our_prob": 0,
        "market_prob": 0,
        "price_cents": price_cents,
        "entry_price_cents": price_cents,
        "limit_price_cents": price_cents,
        "risk_price_cents": price_cents,
        "yes_price_cents": yes_price,
        "no_price_cents": no_price,
        "predicted_high": None,
        "todays_high_snapshot": row.get("actual_high_f"),
        "confirmation_verdict": row.get("confirmation_verdict", ""),
        "market_title": row.get("title", ""),
        "market_subtitle": row.get("subtitle", ""),
        "strike_type": row.get("strike_type", ""),
        "floor_strike": row.get("floor_strike"),
        "cap_strike": row.get("cap_strike"),
        "source": "retrospective_settlement_lock",
        "lock_type": row.get("lock_type", ""),
    }


def _paper_event_from_buy_fill(row):
    price_cents = int(row.get("price_cents", 0) or 0)
    return {
        "timestamp": row.get("timestamp", ""),
        "ticker": row.get("ticker", ""),
        "side": row.get("side", ""),
        "contracts": int(row.get("contracts", 0) or 0),
        "strategy": row.get("strategy", "") or "S1-Weather",
        "target_date": row.get("target_date", ""),
        "cycle": 0,
        "status": "active",
        "limit_price_cents": int(row.get("limit_price_cents", price_cents) or price_cents),
        "current_price_cents": price_cents,
        "entry_price_cents": price_cents,
        "reserved_cost_cents": int(row.get("cost_cents", price_cents * int(row.get("contracts", 0) or 0)) or 0),
        "cost_cents": int(row.get("cost_cents", price_cents * int(row.get("contracts", 0) or 0)) or 0),
        "gross_profit_cents": 0,
        "net_profit_cents": 0,
        "market_result": row.get("result", ""),
        "confirmation_verdict": row.get("confirmation_verdict", row.get("confirmation", "")),
        "source": "legacy_live_fill",
        "order_id": row.get("order_id", ""),
    }


def _resolved_event_from_trade(row):
    result = row.get("result", "")
    if result not in ("win", "loss"):
        return None
    profit_cents = int(row.get("profit_cents", 0) or 0)
    price_cents = int(row.get("price_cents", 0) or 0)
    return {
        "timestamp": row.get("settled_at", row.get("timestamp", "")),
        "ticker": row.get("ticker", ""),
        "side": row.get("side", ""),
        "contracts": int(row.get("contracts", 0) or 0),
        "strategy": row.get("strategy", "") or "S1-Weather",
        "target_date": row.get("target_date", ""),
        "cycle": 0,
        "status": result,
        "limit_price_cents": int(row.get("limit_price_cents", price_cents) or price_cents),
        "current_price_cents": int(row.get("exit_price_cents", 0) or 0),
        "entry_price_cents": price_cents,
        "reserved_cost_cents": int(row.get("cost_cents", price_cents * int(row.get("contracts", 0) or 0)) or 0),
        "cost_cents": int(row.get("cost_cents", price_cents * int(row.get("contracts", 0) or 0)) or 0),
        "gross_profit_cents": profit_cents,
        "net_profit_cents": profit_cents,
        "market_result": result,
        "confirmation_verdict": row.get("confirmation_verdict", row.get("confirmation", "")),
        "source": "legacy_live_fill",
        "order_id": row.get("order_id", ""),
    }


def _resolved_event_from_lock_row(row):
    if not row.get("can_score"):
        return None
    estimated_profit = int(row.get("estimated_profit_cents", 0) or 0)
    return {
        "timestamp": row.get("timestamp", ""),
        "ticker": row.get("ticker", ""),
        "side": row.get("lock_side", ""),
        "contracts": 1,
        "strategy": "S3-SettlementLock",
        "target_date": row.get("target_date", ""),
        "cycle": 0,
        "status": "win" if estimated_profit > 0 else "loss" if estimated_profit < 0 else "flat",
        "limit_price_cents": int(row.get("price_cents", 0) or 0),
        "current_price_cents": 0,
        "entry_price_cents": int(row.get("price_cents", 0) or 0),
        "reserved_cost_cents": int(row.get("price_cents", 0) or 0),
        "cost_cents": int(row.get("price_cents", 0) or 0),
        "gross_profit_cents": estimated_profit,
        "net_profit_cents": estimated_profit,
        "market_result": row.get("lock_side", ""),
        "confirmation_verdict": row.get("confirmation_verdict", ""),
        "source": "retrospective_settlement_lock",
        "lock_type": row.get("lock_type", ""),
    }


def _remove_if_exists(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def _reset_observation_store(state_dir=None):
    paths = _observation_paths(state_dir=state_dir)
    for path in paths.values():
        _remove_if_exists(path)


def import_observation_export(payload, replace=False, state_dir=None):
    paths = _observation_paths(state_dir=state_dir)
    legacy = _legacy_paths(state_dir=state_dir)
    if replace:
        _reset_observation_store(state_dir=state_dir)

    payload = payload or {}
    journal = ObservationJournal(
        events_file=paths["events"],
        decisions_file=paths["decisions"],
        daily_summary_file=paths["daily_summary"],
        db_path=paths["db"],
    )
    summary = {
        "scan_cycles_imported": 0,
        "scan_decisions_imported": 0,
        "paper_events_imported": 0,
    }

    try:
        recent_events = list(payload.get("recent_events", []) or [])
        recent_decisions = list(payload.get("recent_decisions", []) or [])
        decision_groups = defaultdict(list)
        for row in recent_decisions:
            if not isinstance(row, dict):
                continue
            key = (
                int(row.get("cycle", 0) or 0),
                str(row.get("timestamp", "") or ""),
            )
            decision_groups[key].append(row)

        scan_events = []
        paper_events = []
        for row in recent_events:
            if not isinstance(row, dict):
                continue
            if row.get("event_type") == "scan_cycle":
                scan_events.append(row)
            else:
                paper_events.append(row)

        scan_events.sort(key=lambda row: row.get("timestamp", ""))
        paper_events.sort(key=lambda row: row.get("timestamp", ""))

        for row in scan_events:
            key = (
                int(row.get("cycle", 0) or 0),
                str(row.get("timestamp", "") or ""),
            )
            decisions = decision_groups.get(key, [])
            journal.log_scan_cycle(
                cycle=row.get("cycle", 0),
                markets_scanned=row.get("markets_scanned", 0),
                decisions=decisions,
                signals_found=row.get("signals_found", 0),
                trades_placed=row.get("trades_placed", 0),
                skip_counts=row.get("diag_skips", {}),
                null_count=row.get("diag_null", 0),
                evaluated_count=row.get("diag_evaluated", 0),
                weather_error=row.get("weather_api_error", ""),
                observation_mode=row.get("observation_mode", True),
                top_signals=row.get("top_signals", []),
                paper_result={
                    "executed": [{}] * int(row.get("paper_entries", 0) or 0),
                    "filled_pending": [{}] * int(row.get("paper_filled_pending", 0) or 0),
                    "queued": [{}] * int(row.get("paper_resting_orders", 0) or 0),
                    "expired_pending": [{}] * int(row.get("paper_expired_pending", 0) or 0),
                    "blocked_reasons": row.get("paper_blocked_reasons", {}) or {},
                },
                settlement_lock_candidates=row.get("settlement_lock_candidates", 0),
                timestamp=row.get("timestamp", ""),
            )
            summary["scan_cycles_imported"] += 1
            summary["scan_decisions_imported"] += len(decisions)

        for row in paper_events:
            journal.log_paper_event(
                row.get("event_type", ""),
                row,
                timestamp=row.get("timestamp", ""),
            )
            summary["paper_events_imported"] += 1

        daily_rows = payload.get("daily_summary")
        if isinstance(daily_rows, list):
            normalized = {
                "updated_at": payload.get("generated_at", ""),
                "days": {
                    str(row.get("date", "")): row
                    for row in daily_rows
                    if isinstance(row, dict) and row.get("date")
                },
            }
            if normalized["days"]:
                config.atomic_json_save(paths["daily_summary"], normalized)
    finally:
        journal.close()

    return summary


def run_backfill(replace=False, include_live_trades=True, include_retro_locks=True, state_dir=None):
    paths = _observation_paths(state_dir=state_dir)
    legacy = _legacy_paths(state_dir=state_dir)
    if replace:
        _reset_observation_store(state_dir=state_dir)

    journal = ObservationJournal(
        events_file=paths["events"],
        decisions_file=paths["decisions"],
        daily_summary_file=paths["daily_summary"],
        db_path=paths["db"],
    )
    summary = {
        "scan_days_backfilled": 0,
        "scan_decisions_backfilled": 0,
        "scan_buy_decisions": 0,
        "legacy_live_fill_events": 0,
        "legacy_live_resolved_events": 0,
        "legacy_live_unknown_events": 0,
        "retrospective_lock_decisions": 0,
        "retrospective_lock_resolved_events": 0,
    }

    try:
        learning_state = _read_json(legacy["learning_state"], {})
        scan_snapshots = learning_state.get("scan_snapshots", {}) if isinstance(learning_state, dict) else {}

        for cycle, day in enumerate(sorted(scan_snapshots.keys()), start=1):
            rows = scan_snapshots.get(day, {}) or {}
            if not isinstance(rows, dict) or not rows:
                continue
            decisions = [_decision_from_snapshot(row) for row in rows.values() if isinstance(row, dict)]
            skip_counts = Counter()
            for row in decisions:
                if row.get("skip_reason"):
                    skip_counts[row["skip_reason"]] += 1
            buy_rows = [row for row in decisions if row.get("signal") == "buy"]
            top_signals = [
                {
                    "ticker": row.get("ticker", ""),
                    "side": row.get("side", ""),
                    "edge": row.get("edge", 0),
                    "strategy": row.get("strategy", ""),
                }
                for row in sorted(buy_rows, key=lambda item: item.get("edge", 0), reverse=True)[:5]
            ]
            timestamps = [row.get("timestamp", "") for row in decisions if row.get("timestamp")]
            cycle_ts = min(timestamps) if timestamps else "%sT00:00:00+00:00" % day
            journal.log_scan_cycle(
                cycle=cycle,
                markets_scanned=len(decisions),
                decisions=decisions,
                signals_found=len(buy_rows),
                trades_placed=0,
                skip_counts=dict(skip_counts),
                null_count=0,
                evaluated_count=len(decisions),
                weather_error="",
                observation_mode=True,
                top_signals=top_signals,
                paper_result={},
                settlement_lock_candidates=0,
                timestamp=cycle_ts,
            )
            summary["scan_days_backfilled"] += 1
            summary["scan_decisions_backfilled"] += len(decisions)
            summary["scan_buy_decisions"] += len(buy_rows)

        if include_retro_locks:
            paper_locks = _read_json(legacy["paper_locks"], {})
            retro_history = (
                (((paper_locks or {}).get("retrospective", {}) or {}).get("history", []))
                if isinstance(paper_locks, dict)
                else []
            )
            grouped = defaultdict(list)
            for row in retro_history:
                if isinstance(row, dict):
                    grouped[row.get("snapshot_day", row.get("target_date", ""))].append(row)

            for offset, day in enumerate(sorted(grouped.keys()), start=1):
                decisions = [_decision_from_lock_row(row) for row in grouped[day]]
                top_signals = [
                    {
                        "ticker": row.get("ticker", ""),
                        "side": row.get("side", ""),
                        "edge": 0,
                        "strategy": "S3-SettlementLock",
                    }
                    for row in decisions[:5]
                ]
                timestamps = [row.get("timestamp", "") for row in decisions if row.get("timestamp")]
                cycle_ts = min(timestamps) if timestamps else "%sT00:00:00+00:00" % day
                buy_count = sum(1 for row in decisions if row.get("signal") == "buy")
                skip_counts = Counter(row.get("skip_reason", "") for row in decisions if row.get("skip_reason"))
                journal.log_scan_cycle(
                    cycle=1000 + offset,
                    markets_scanned=len(decisions),
                    decisions=decisions,
                    signals_found=buy_count,
                    trades_placed=0,
                    skip_counts=dict(skip_counts),
                    null_count=0,
                    evaluated_count=len(decisions),
                    weather_error="",
                    observation_mode=True,
                    top_signals=top_signals,
                    paper_result={},
                    settlement_lock_candidates=len(decisions),
                    timestamp=cycle_ts,
                )
                summary["retrospective_lock_decisions"] += len(decisions)

                for row in grouped[day]:
                    resolved = _resolved_event_from_lock_row(row)
                    if resolved:
                        journal.log_paper_event("paper_trade_resolved", resolved, timestamp=resolved.get("timestamp", ""))
                        summary["retrospective_lock_resolved_events"] += 1

        if include_live_trades:
            trades = _read_json(legacy["trade_log"], [])
            for row in trades:
                if not isinstance(row, dict):
                    continue
                if row.get("entry_type") != "buy_fill":
                    continue
                fill_event = _paper_event_from_buy_fill(row)
                journal.log_paper_event("paper_order_filled", fill_event, timestamp=fill_event.get("timestamp", ""))
                summary["legacy_live_fill_events"] += 1

                resolved = _resolved_event_from_trade(row)
                if resolved:
                    journal.log_paper_event("paper_trade_resolved", resolved, timestamp=resolved.get("timestamp", ""))
                    summary["legacy_live_resolved_events"] += 1
                elif row.get("result") == "expired_unknown":
                    journal.log_paper_event(
                        "paper_trade_expired_unknown",
                        {
                            **fill_event,
                            "status": "expired_unknown",
                            "market_result": row.get("result", ""),
                        },
                        timestamp=row.get("settled_at", row.get("timestamp", "")),
                    )
                    summary["legacy_live_unknown_events"] += 1
    finally:
        journal.close()

    return summary


def main():
    parser = argparse.ArgumentParser(description="Backfill SQLite observation DB from legacy artifacts.")
    parser.add_argument("--replace", action="store_true", help="Reset observation DB and journal files before backfill.")
    parser.add_argument("--skip-live-trades", action="store_true", help="Do not import legacy live fills.")
    parser.add_argument("--skip-retro-locks", action="store_true", help="Do not import retrospective settlement-lock data.")
    args = parser.parse_args()

    summary = run_backfill(
        replace=args.replace,
        include_live_trades=not args.skip_live_trades,
        include_retro_locks=not args.skip_retro_locks,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
