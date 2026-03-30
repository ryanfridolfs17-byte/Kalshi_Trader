"""
Strategy registry and scorecards.

This separates strategy definitions from the main bot loop and gives us
strategy-level paper scorecards plus simple promotion gates before live trading
is ever re-enabled.
"""

from datetime import datetime, timezone

from bot_db import BotDatabase
import config


def get_strategy_definitions():
    return {
        "S1-Weather": {
            "label": "Directional Weather Ensemble",
            "enabled": True,
            "live_enabled": bool(getattr(config, "ALLOW_LIVE_WEATHER_STRATEGY", False)),
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
            "paper_default": True,
            "promotion_gate": {
                "min_resolved": 25,
                "min_profit_factor": 1.05,
                "min_fill_rate": 0.35,
                "max_drawdown_cents": 250,
            },
        },
    }


class StrategyRegistry:
    def __init__(self, weather_strategy, settlement_lock):
        self.weather_strategy = weather_strategy
        self.settlement_lock = settlement_lock

    def evaluate_markets(self, markets, observed_highs, balance_cents, observation_mode=False):
        buy_signals = []
        paper_lock_signals = []
        all_decisions = []
        all_evaluated = []
        skip_counts = {}
        null_count = 0

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
                if settlement_signal and (
                    observation_mode
                    or getattr(config, "ALLOW_LIVE_SETTLEMENT_LOCK_TRADES", False)
                ):
                    buy_signals.append(settlement_signal)
                    all_decisions.append(settlement_signal)
                    if settlement_signal.get("city_code") and settlement_signal.get("predicted_high") is not None:
                        all_evaluated.append(settlement_signal)
                elif settlement_signal and not observation_mode:
                    skip_counts["settlement_lock_live_disabled"] = skip_counts.get("settlement_lock_live_disabled", 0) + 1

            signal = self.weather_strategy.evaluate_market(market, todays_high=todays_high)
            if settlement_signal is None and signal and signal.get("signal") == "buy":
                buy_signals.append(signal)
            if signal:
                all_decisions.append(signal)
                if signal.get("city_code") and signal.get("predicted_high") is not None:
                    all_evaluated.append(signal)

            if signal is None:
                null_count += 1
            elif signal.get("skip_reason"):
                reason = signal.get("skip_reason", "?")
                skip_counts[reason] = skip_counts.get(reason, 0) + 1

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


def build_strategy_scorecards(hours=None, db=None):
    hours = int(hours or getattr(config, "STRATEGY_SCORECARD_WINDOW_HOURS", 24 * 14) or (24 * 14))
    owns_db = db is None
    db = db or BotDatabase()
    definitions = get_strategy_definitions()
    try:
        decisions = db.fetch_recent_decisions(hours=hours, max_rows=50000)
        events = db.fetch_recent_events(hours=hours, max_rows=20000)

        scorecards = {}
        for strategy_id, definition in definitions.items():
            scorecards[strategy_id] = {
                "strategy": strategy_id,
                "label": definition.get("label", strategy_id),
                "enabled": bool(definition.get("enabled", True)),
                "live_enabled": bool(definition.get("live_enabled", False)),
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
                "promotion_gate": definition.get("promotion_gate", {}),
                "promotion_blockers": [],
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
        for event in events:
            event_type = event.get("event_type", "")
            strategy_id = event.get("strategy", "")
            if strategy_id not in scorecards:
                continue
            if event_type == "paper_order_queued":
                scorecards[strategy_id]["paper_queued"] += 1
            elif event_type == "paper_order_filled":
                scorecards[strategy_id]["paper_filled"] += 1
            elif event_type == "paper_order_expired_unfilled":
                scorecards[strategy_id]["paper_expired"] += 1
            elif event_type == "paper_trade_resolved":
                pnl = int(event.get("net_profit_cents", 0) or 0)
                scorecards[strategy_id]["paper_resolved"] += 1
                scorecards[strategy_id]["net_profit_cents"] += pnl
                resolved_profits[strategy_id].append(pnl)
                if pnl > 0:
                    scorecards[strategy_id]["wins"] += 1
                    scorecards[strategy_id]["gross_profit_cents"] += pnl
                elif pnl < 0:
                    scorecards[strategy_id]["losses"] += 1
                    scorecards[strategy_id]["gross_loss_cents"] += abs(pnl)

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
        if owns_db:
            db.close()
