"""
Strategy registry and scorecards.

This separates strategy definitions from the main bot loop and gives us
strategy-level paper scorecards plus simple promotion gates before live trading
is ever re-enabled.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from bot_db import BotDatabase
import config
from paper_challengers import PaperChallengerEngine


def get_strategy_definitions():
    return {
        "S1-Weather": {
            "label": "Directional Weather Ensemble",
            "enabled": True,
            "live_enabled": bool(getattr(config, "ALLOW_LIVE_WEATHER_STRATEGY", False)),
            "paper_entry_enabled": True,
            "paper_only": False,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 75,
                "min_profit_factor": 1.15,
                "min_fill_rate": 0.20,
                "max_drawdown_cents": 500,
            },
        },
        "S3-SettlementLock": {
            "label": "Settlement Lock",
            "enabled": bool(getattr(config, "ENABLE_SETTLEMENT_LOCK_STRATEGY", False)),
            "live_enabled": bool(getattr(config, "ALLOW_LIVE_SETTLEMENT_LOCK_TRADES", False)),
            "paper_entry_enabled": bool(getattr(config, "ENABLE_SETTLEMENT_LOCK_STRATEGY", False)),
            "paper_only": False,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 25,
                "min_profit_factor": 1.05,
                "min_fill_rate": 0.35,
                "max_drawdown_cents": 250,
            },
        },
        "S4-NextDayNoPaper": {
            "label": "Paper Challenger: Next-Day NO",
            "enabled": bool(getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)),
            "live_enabled": False,
            "paper_entry_enabled": bool(
                getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)
                and getattr(config, "PAPER_CHALLENGER_ALLOW_NEXT_DAY_NO", False)
            ),
            "paper_only": True,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 30,
                "min_profit_factor": 1.05,
                "min_fill_rate": 0.95,
                "max_drawdown_cents": 400,
            },
        },
        "S5-SoftSettlementLockPaper": {
            "label": "Paper Challenger: Soft Settlement Lock",
            "enabled": bool(getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)),
            "live_enabled": False,
            "paper_entry_enabled": bool(
                getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)
                and getattr(config, "PAPER_CHALLENGER_ALLOW_SOFT_SETTLEMENT_LOCK", False)
            ),
            "paper_only": True,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 20,
                "min_profit_factor": 1.03,
                "min_fill_rate": 0.95,
                "max_drawdown_cents": 300,
            },
        },
        "S6-TightNextDayNoPaper": {
            "label": "Paper Challenger: Tight Next-Day NO",
            "enabled": bool(getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)),
            "live_enabled": False,
            "paper_entry_enabled": bool(
                getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)
                and getattr(config, "PAPER_CHALLENGER_ALLOW_TIGHT_NEXT_DAY_NO", False)
            ),
            "paper_only": True,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 20,
                "min_profit_factor": 1.05,
                "min_fill_rate": 0.95,
                "max_drawdown_cents": 250,
            },
        },
        "S7-AfternoonNOSweetSpot": {
            "label": "Paper Challenger: Afternoon NO Sweet Spot",
            "enabled": bool(getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)),
            "live_enabled": False,
            "paper_entry_enabled": bool(
                getattr(config, "ENABLE_OBSERVATION_CHALLENGER_STRATEGIES", False)
                and getattr(config, "PAPER_CHALLENGER_ALLOW_AFTERNOON_NO_SWEET_SPOT", False)
            ),
            "paper_only": True,
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 30,
                "min_profit_factor": 1.10,
                "min_fill_rate": 0.90,
                "max_drawdown_cents": 300,
            },
        },
    }


class StrategyRegistry:
    def __init__(self, weather_strategy, settlement_lock, challenger_engine=None):
        self.weather_strategy = weather_strategy
        self.settlement_lock = settlement_lock
        self.challenger_engine = challenger_engine or PaperChallengerEngine()
        self._scorecard_cache = {}
        self._scorecard_cache_at = None

    @staticmethod
    def _with_observation_metadata(signal, todays_high=None):
        if not signal:
            return signal
        row = dict(signal)
        if todays_high is not None and row.get("todays_high_snapshot") is None:
            row["todays_high_snapshot"] = todays_high
        return row

    def _select_paper_challengers(self, candidates, existing_signals):
        if not candidates:
            return []

        raw_max_total = int(getattr(config, "PAPER_CHALLENGER_MAX_SIGNALS_PER_CYCLE", 0) or 0)
        max_total = raw_max_total if raw_max_total > 0 else None

        raw_max_per_city = int(getattr(config, "PAPER_CHALLENGER_MAX_PER_CITY", 0) or 0)
        max_per_city = raw_max_per_city if raw_max_per_city > 0 else None
        existing_tickers = {
            row.get("ticker", "")
            for row in existing_signals or []
            if row.get("ticker", "")
        }
        selected = []
        city_counts = {}
        seen_tickers = set(existing_tickers)
        ranked = sorted(
            candidates,
            key=lambda row: (
                -float(row.get("fee_adjusted_edge", 0) or 0),
                -float(row.get("edge", 0) or 0),
                int(row.get("price_cents", 0) or 0),
            ),
        )
        for candidate in ranked:
            ticker = candidate.get("ticker", "")
            if not ticker or ticker in seen_tickers:
                continue
            city_code = candidate.get("city_code", "")
            if city_code and max_per_city is not None and city_counts.get(city_code, 0) >= max_per_city:
                continue
            selected.append(candidate)
            seen_tickers.add(ticker)
            if city_code:
                city_counts[city_code] = city_counts.get(city_code, 0) + 1
            if max_total is not None and len(selected) >= max_total:
                break
        return selected

    def _get_strategy_statuses(self, observation_mode=False):
        if not observation_mode:
            return {}

        ttl_seconds = max(
            0,
            int(getattr(config, "PAPER_STRATEGY_STATUS_CACHE_SECONDS", 300) or 300),
        )
        now = datetime.now(timezone.utc)
        if (
            self._scorecard_cache
            and self._scorecard_cache_at is not None
            and ttl_seconds > 0
            and (now - self._scorecard_cache_at).total_seconds() < ttl_seconds
        ):
            return self._scorecard_cache

        try:
            cards = build_strategy_scorecards()
        except Exception as exc:
            print(f"[STRATEGY-STATUS] scorecard refresh failed: {exc}")
            return self._scorecard_cache or {}

        statuses = {
            row.get("strategy", ""): row
            for row in cards
            if row.get("strategy")
        }
        self._scorecard_cache = statuses
        self._scorecard_cache_at = now
        return statuses

    def _build_next_day_shadow_signal(self, market, signal, todays_high=None, strategy_statuses=None):
        if not signal or signal.get("skip_reason") != "next_day_directional_blocked":
            return None
        if signal.get("side") != "no":
            return None
        if signal.get("strike_type") != "between":
            return None
        if not hasattr(self.weather_strategy, "evaluate_market"):
            return None
        try:
            shadow_signal = self.weather_strategy.evaluate_market(
                market,
                todays_high=todays_high,
                allow_next_day_directional_override=True,
            )
        except TypeError:
            return None
        if not shadow_signal:
            return None
        return self._with_observation_metadata(shadow_signal, todays_high=todays_high)

    def evaluate_markets(self, markets, observed_highs, balance_cents, observation_mode=False):
        buy_signals = []
        paper_lock_signals = []
        paper_challenger_candidates = []
        all_decisions = []
        all_evaluated = []
        skip_counts = {}
        null_count = 0
        strategy_statuses = self._get_strategy_statuses(observation_mode=observation_mode)

        settlement_lock_tickers = set()

        for market in markets:
            city_code = market.get("_city_code", "")
            todays_high = (observed_highs or {}).get(city_code)
            paper_lock = self.settlement_lock.evaluate_market(market, todays_high=todays_high)
            if paper_lock:
                paper_lock_signals.append(paper_lock)

            settlement_signal = None
            if getattr(config, "ENABLE_SETTLEMENT_LOCK_STRATEGY", False) and paper_lock:
                settlement_signal = self.settlement_lock.build_trade_signal(
                    paper_lock,
                    market,
                    balance_cents=balance_cents,
                )
                settlement_signal = self._with_observation_metadata(settlement_signal, todays_high=todays_high)
                if settlement_signal and (
                    observation_mode
                    or getattr(config, "ALLOW_LIVE_SETTLEMENT_LOCK_TRADES", False)
                ):
                    buy_signals.append(settlement_signal)
                    all_decisions.append(settlement_signal)
                    settlement_lock_tickers.add(market.get("ticker", ""))
                    if settlement_signal.get("city_code") and settlement_signal.get("predicted_high") is not None:
                        all_evaluated.append(settlement_signal)
                elif settlement_signal and not observation_mode:
                    skip_counts["settlement_lock_live_disabled"] = skip_counts.get("settlement_lock_live_disabled", 0) + 1

            # Skip weather strategy for tickers already claimed by S3 (avoids
            # duplicate CASE 1 signal and wasted ensemble fetch).
            ticker = market.get("ticker", "")
            if ticker and ticker in settlement_lock_tickers:
                continue

            signal = self.weather_strategy.evaluate_market(market, todays_high=todays_high)
            signal = self._with_observation_metadata(signal, todays_high=todays_high)
            # Only suppress weather buy if S3 already claimed this ticker
            if ticker not in settlement_lock_tickers and signal and signal.get("signal") == "buy":
                buy_signals.append(signal)
            if signal:
                all_decisions.append(signal)
                if signal.get("city_code") and signal.get("predicted_high") is not None:
                    all_evaluated.append(signal)
                if observation_mode:
                    shadow_signal = self._build_next_day_shadow_signal(
                        market,
                        signal,
                        todays_high=todays_high,
                        strategy_statuses=strategy_statuses,
                    )
                    paper_challenger_candidates.extend(
                        self.challenger_engine.generate(
                            market,
                            signal,
                            todays_high=todays_high,
                            observation_mode=observation_mode,
                            next_day_shadow_signal=shadow_signal,
                            strategy_statuses=strategy_statuses,
                            balance_cents=balance_cents,
                        )
                    )

            if signal is None:
                null_count += 1
            elif signal.get("skip_reason"):
                reason = signal.get("skip_reason", "?")
                skip_counts[reason] = skip_counts.get(reason, 0) + 1

        selected_challengers = self._select_paper_challengers(paper_challenger_candidates, buy_signals)
        if selected_challengers:
            buy_signals.extend(selected_challengers)
            all_decisions.extend(selected_challengers)
            for row in selected_challengers:
                if row.get("city_code") and row.get("predicted_high") is not None:
                    all_evaluated.append(row)

        return {
            "buy_signals": buy_signals,
            "paper_lock_signals": paper_lock_signals,
            "all_decisions": all_decisions,
            "all_evaluated": all_evaluated,
            "skip_counts": skip_counts,
            "null_count": null_count,
        }


def _compute_drawdown_cents(profit_series):
    cumulative = 0
    peak = 0
    max_drawdown = 0
    for profit in profit_series:
        cumulative += int(profit or 0)
        if cumulative > peak:
            peak = cumulative
        drawdown = peak - cumulative
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    return max_drawdown


def _parse_utc_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _record_verdict_outcome(card, verdict, pnl):
    verdict_name = str(verdict or "").strip().upper()
    if not verdict_name:
        return
    verdict_stats = card.setdefault("verdict_stats", {})
    stats = verdict_stats.setdefault(
        verdict_name,
        {
            "resolved": 0,
            "wins": 0,
            "losses": 0,
            "net_profit_cents": 0,
        },
    )
    stats["resolved"] += 1
    stats["net_profit_cents"] += pnl
    if pnl > 0:
        stats["wins"] += 1
    elif pnl < 0:
        stats["losses"] += 1

    if verdict_name == "STRONG":
        card["strong_resolved"] += 1
        if pnl > 0:
            card["strong_wins"] += 1
        elif pnl < 0:
            card["strong_losses"] += 1
    elif verdict_name == "CONFIRM":
        card["confirm_resolved"] += 1
        if pnl > 0:
            card["confirm_wins"] += 1
        elif pnl < 0:
            card["confirm_losses"] += 1


def _record_resolved_trade(card, resolved_profit_series, pnl, verdict=""):
    pnl = int(pnl or 0)
    card["paper_resolved"] += 1
    card["net_profit_cents"] += pnl
    resolved_profit_series.append(pnl)
    if pnl > 0:
        card["wins"] += 1
        card["gross_profit_cents"] += pnl
    elif pnl < 0:
        card["losses"] += 1
        card["gross_loss_cents"] += abs(pnl)
    _record_verdict_outcome(card, verdict, pnl)


def _merge_settlement_lock_scorecard(scorecards, resolved_profits, hours):
    card = scorecards.get("S3-SettlementLock")
    if not card:
        return
    try:
        if not os.path.exists(config.PAPER_LOCKS_FILE):
            return
        with open(config.PAPER_LOCKS_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:
        return

    if not isinstance(state, dict):
        return

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=int(hours or 0))
    active_rows = list((state.get("active", {}) or {}).values())
    history_rows = list(state.get("history", []) or [])

    filled_entries = 0
    for row in active_rows:
        first_seen_at = _parse_utc_timestamp(row.get("first_seen_at") or row.get("timestamp"))
        if first_seen_at and first_seen_at >= cutoff:
            filled_entries += 1

    for row in history_rows:
        first_seen_at = _parse_utc_timestamp(row.get("first_seen_at") or row.get("timestamp"))
        if first_seen_at and first_seen_at >= cutoff:
            filled_entries += 1

        resolved_at = _parse_utc_timestamp(row.get("resolved_at"))
        if not resolved_at or resolved_at < cutoff:
            continue
        if row.get("status") not in ("win", "loss"):
            continue

        pnl = int(row.get("net_profit_cents", row.get("entry_profit_cents", 0)) or 0)
        verdict = row.get("confirmation_verdict", "SETTLEMENT_LOCK")
        _record_resolved_trade(card, resolved_profits["S3-SettlementLock"], pnl, verdict=verdict)

    card["paper_filled"] += filled_entries


def build_strategy_scorecards(hours=None, db=None):
    hours = int(hours or getattr(config, "STRATEGY_SCORECARD_WINDOW_HOURS", 24 * 14) or (24 * 14))
    definitions = get_strategy_definitions()
    owns_db = db is None
    use_sqlite = bool(getattr(config, "OBSERVATION_ENABLE_SQLITE", True))
    if use_sqlite:
        db = db or BotDatabase()
    try:
        if use_sqlite:
            decisions = db.fetch_recent_decisions(hours=hours, max_rows=50000)
            events = db.fetch_recent_events(hours=hours, max_rows=20000)
        else:
            from observation_journal import ObservationJournal
            journal = ObservationJournal()
            history = journal.get_recent_history(
                hours=hours,
                event_limit=20000,
                decision_limit=100000,
                include_decisions=True,
                prefer_db=False,
                fast_mode=True,
                cached_only=False,
            )
            events = history.get("recent_events", []) or []
            decisions = history.get("recent_decisions", []) or []
            journal.close()

        scorecards = {}
        for strategy_id, definition in definitions.items():
            scorecards[strategy_id] = {
                "strategy": strategy_id,
                "label": definition.get("label", strategy_id),
                "enabled": bool(definition.get("enabled", True)),
                "live_enabled": bool(definition.get("live_enabled", False)),
                "paper_entry_enabled": bool(definition.get("paper_entry_enabled", True)),
                "paper_only": bool(definition.get("paper_only", False)),
                "paper_default": bool(definition.get("paper_default", True)),
                "hours": hours,
                "buy_decisions": 0,
                "skip_decisions": 0,
                "paper_blocked": 0,
                "paper_queued": 0,
                "paper_filled": 0,
                "paper_expired": 0,
                "paper_resolved": 0,
                "wins": 0,
                "losses": 0,
                "gross_profit_cents": 0,
                "gross_loss_cents": 0,
                "net_profit_cents": 0,
                "fill_rate": None,
                "profit_factor": None,
                "expectancy_cents": None,
                "max_drawdown_cents": 0,
                "verdict_stats": {},
                "strong_resolved": 0,
                "strong_wins": 0,
                "strong_losses": 0,
                "confirm_resolved": 0,
                "confirm_wins": 0,
                "confirm_losses": 0,
                "promotion_gate": definition.get("promotion_gate", {}),
                "paper_entry_blockers": [],
                "promotion_blockers": [],
                "eligible_for_paper_entries": False,
                "eligible_for_live": False,
                "last_updated_at": datetime.now(timezone.utc).isoformat(),
            }

        for row in decisions:
            strategy_id = row.get("strategy", "")
            if strategy_id not in scorecards:
                continue
            if row.get("signal") == "buy":
                scorecards[strategy_id]["buy_decisions"] += 1
            else:
                scorecards[strategy_id]["skip_decisions"] += 1
            if row.get("execution_status") == "paper_blocked":
                scorecards[strategy_id]["paper_blocked"] += 1

        resolved_profits = {key: [] for key in scorecards}
        has_s3_event_data = False
        for event in events:
            event_type = event.get("event_type", "")
            strategy_id = event.get("strategy", "")
            if strategy_id not in scorecards:
                continue
            if strategy_id == "S3-SettlementLock":
                has_s3_event_data = True
            if event_type == "paper_order_queued":
                scorecards[strategy_id]["paper_queued"] += 1
            elif event_type == "paper_order_filled":
                scorecards[strategy_id]["paper_filled"] += 1
            elif event_type == "paper_order_expired_unfilled":
                scorecards[strategy_id]["paper_expired"] += 1
            elif event_type == "paper_trade_resolved":
                pnl = int(event.get("net_profit_cents", 0) or 0)
                verdict = event.get("confirmation_verdict", "")
                _record_resolved_trade(
                    scorecards[strategy_id],
                    resolved_profits[strategy_id],
                    pnl,
                    verdict=verdict,
                )

        if not has_s3_event_data:
            _merge_settlement_lock_scorecard(scorecards, resolved_profits, hours)

        for strategy_id, card in scorecards.items():
            closed_fill_trials = card["paper_filled"] + card["paper_expired"]
            if closed_fill_trials > 0:
                card["fill_rate"] = round(card["paper_filled"] / float(closed_fill_trials), 3)
            total_resolved = card["paper_resolved"]
            if total_resolved > 0:
                card["expectancy_cents"] = round(card["net_profit_cents"] / float(total_resolved), 1)
            if card["gross_loss_cents"] > 0:
                card["profit_factor"] = round(card["gross_profit_cents"] / float(card["gross_loss_cents"]), 3)
            elif card["gross_profit_cents"] > 0:
                card["profit_factor"] = None
            card["max_drawdown_cents"] = _compute_drawdown_cents(resolved_profits.get(strategy_id, []))

            paper_blockers = []
            if not card["enabled"]:
                paper_blockers.append("strategy_disabled")
            if not card["paper_entry_enabled"]:
                paper_blockers.append("paper_disabled_by_config")
            if (
                card["paper_only"]
                and getattr(config, "PAPER_STRATEGY_AUTOKILL_ENABLED", False)
                and card["paper_resolved"] >= int(getattr(config, "PAPER_STRATEGY_AUTOKILL_MIN_RESOLVED", 5) or 5)
            ):
                min_pf = float(getattr(config, "PAPER_STRATEGY_AUTOKILL_MIN_PROFIT_FACTOR", 1.0) or 1.0)
                min_expectancy = float(getattr(config, "PAPER_STRATEGY_AUTOKILL_MIN_EXPECTANCY_CENTS", 0.0) or 0.0)
                profit_factor = card["profit_factor"]
                expectancy = float(card["expectancy_cents"] or 0.0)
                if expectancy <= min_expectancy and (profit_factor is None or profit_factor < min_pf):
                    paper_blockers.append("auto_killed_negative_paper_performance")
            card["paper_entry_blockers"] = paper_blockers
            card["eligible_for_paper_entries"] = len(paper_blockers) == 0

            gate = card["promotion_gate"]
            blockers = []
            if not card["enabled"]:
                blockers.append("strategy_disabled")
            if not card["live_enabled"]:
                blockers.append("live_disabled_by_config")
            if card["paper_resolved"] < int(gate.get("min_resolved", 0) or 0):
                blockers.append("insufficient_resolved_paper_trades")
            profit_factor = card["profit_factor"]
            min_pf = gate.get("min_profit_factor")
            if min_pf is not None:
                if profit_factor is None:
                    blockers.append("missing_profit_factor_history")
                elif profit_factor < float(min_pf):
                    blockers.append("profit_factor_below_gate")
            fill_rate = card["fill_rate"]
            min_fill = gate.get("min_fill_rate")
            if min_fill is not None:
                if fill_rate is None:
                    blockers.append("missing_fill_rate_history")
                elif fill_rate < float(min_fill):
                    blockers.append("fill_rate_below_gate")
            if card["max_drawdown_cents"] > int(gate.get("max_drawdown_cents", 0) or 0):
                blockers.append("drawdown_above_gate")
            if (card["expectancy_cents"] or 0) <= 0:
                blockers.append("non_positive_expectancy")
            card["promotion_blockers"] = blockers
            card["eligible_for_live"] = len(blockers) == 0

        return [scorecards[key] for key in sorted(scorecards.keys())]
    finally:
        if use_sqlite and owns_db:
            db.close()
