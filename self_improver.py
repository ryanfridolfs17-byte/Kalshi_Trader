"""
Weekly Self-Improvement Engine (Phase 4)

Automated parameter tuning: measure weekly performance → diagnose shortfalls →
propose parameter changes → replay-backtest against actual trades → deploy only
if improvement >= 5%.

Runs Sunday 11 PM ET. Writes overrides to config_overrides.json (NOT config.py).
Overrides expire after 7 days and auto-revert.
"""

import json
import math
import os
from datetime import datetime, timedelta, timezone

import config

# ═══════════════════════════════════════════════════════
# PARAMETER SAFETY
# ═══════════════════════════════════════════════════════

# Only these parameters can be auto-adjusted, with hard min/max bounds
PARAM_BOUNDS = {
    "MIN_EDGE":                    (0.04, 0.20),
    "MAKER_SPREAD_BUFFER_CENTS":   (1, 5),
    "MAX_MODEL_DIVERGENCE_F":      (3.0, 6.0),
    "NEXT_DAY_SIZING_MULTIPLIER":  (0.30, 0.70),
    "ROUNDING_BUFFER_SOFT_F":      (1.0, 3.0),
    "PRE_SETTLEMENT_SIZING_MULT":  (0.50, 1.0),
    "NO_SIDE_SIZING_MULTIPLIER":   (0.30, 0.80),
    "NO_SIDE_MAX_PRICE_CENTS":     (40, 70),
}

# These parameters must NEVER be auto-adjusted (risk limits, kill switches)
NEVER_ADJUST = {
    "DAILY_LOSS_LIMIT_CENTS", "MAX_TOTAL_EXPOSURE_PCT", "MAX_PER_CITY_PCT",
    "MAX_PER_TICKER_CENTS", "MAX_CONTRACTS_PER_TICKER", "KILL_SWITCH_CONSECUTIVE_LOSSES",
    "KILL_SWITCH_MIN_SHARPE_7D", "CASE2_ENABLED", "FEE_ADJUSTED_MIN_EDGE",
    "TOTAL_DEPOSITS_CENTS", "CONSECUTIVE_LOSS_PAUSE", "CONSECUTIVE_LOSS_PAUSE_MINUTES",
    "MAX_TOTAL_EXPOSURE_CENTS", "MAX_PER_CITY_CENTS",
}

# Statuses that indicate a real settled trade (not phantom/resting/error)
SETTLED_STATUSES = {
    "demo_filled", "live_filled", "dry_run",
    "settled_win", "settled_loss",
}

SKIP_STATUSES = {
    "phantom_not_filled", "expired_dry_run", "resting", "cancelled",
    "error", "submitted", "partial",
}


# ═══════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════

def run_weekly_review():
    """Main orchestrator. Returns review dict or None if skipped."""
    print("\n" + "=" * 60)
    print("  [SELF-IMPROVE] Starting weekly review")
    print("=" * 60)

    # 1. Measure
    metrics = _measure_performance(config.SELF_IMPROVE_LOOKBACK_DAYS)
    if metrics is None:
        print("  [SELF-IMPROVE] Skipped — insufficient data")
        return None

    _print_metrics(metrics)

    # 2. Check if all targets met
    shortfalls = _find_shortfalls(metrics)
    if not shortfalls:
        print("  [SELF-IMPROVE] All targets met — no changes needed")
        review = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "shortfalls": [],
            "proposals": [],
            "deployed": False,
            "reason": "all_targets_met",
        }
        _save_improvement_log(review)
        return review

    print(f"  [SELF-IMPROVE] {len(shortfalls)} shortfall(s) detected:")
    for sf in shortfalls:
        print(f"    - {sf['metric']}: {sf['actual']:.3f} vs target {sf['target']:.3f}")

    # 3. Diagnose → propose
    proposals = _diagnose_underperformance(metrics, shortfalls)
    if not proposals:
        print("  [SELF-IMPROVE] No actionable proposals generated")
        review = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metrics": metrics,
            "shortfalls": shortfalls,
            "proposals": [],
            "deployed": False,
            "reason": "no_proposals",
        }
        _save_improvement_log(review)
        return review

    print(f"  [SELF-IMPROVE] {len(proposals)} proposal(s):")
    for p in proposals:
        print(f"    - {p['param']}: {p['current']} → {p['proposed']} ({p['reason']})")

    # 4. Replay backtest
    trades = _load_settled_trades(config.SELF_IMPROVE_LOOKBACK_DAYS)
    improvement = _replay_backtest(trades, proposals, metrics)

    print(f"  [SELF-IMPROVE] Replay improvement: {improvement:+.1f}%")

    # 5. Deploy or log
    deployed = False
    if improvement >= config.SELF_IMPROVE_MIN_IMPROVEMENT_PCT:
        _apply_overrides(proposals)
        deployed = True
        print(f"  [SELF-IMPROVE] Deployed {len(proposals)} override(s)")
    else:
        print(f"  [SELF-IMPROVE] Not deploying — improvement {improvement:.1f}% < {config.SELF_IMPROVE_MIN_IMPROVEMENT_PCT}% threshold")

    review = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metrics": metrics,
        "shortfalls": shortfalls,
        "proposals": proposals,
        "improvement_pct": round(improvement, 2),
        "deployed": deployed,
    }
    _save_improvement_log(review)

    print("  [SELF-IMPROVE] Weekly review complete")
    return review


def load_config_overrides():
    """Load and apply config overrides from file. Called at bot startup.

    Returns dict of applied overrides, or empty dict if none/expired.
    """
    filepath = config.CONFIG_OVERRIDES_FILE
    if not os.path.exists(filepath):
        return {}

    try:
        with open(filepath) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"  [SELF-IMPROVE] Could not read overrides: {e}")
        return {}

    # Check expiration
    expires_at = data.get("expires_at", "")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp_dt:
                print("  [SELF-IMPROVE] Overrides expired — reverting to defaults")
                os.remove(filepath)
                return {}
        except (ValueError, OSError):
            pass

    overrides = data.get("overrides", {})
    applied = {}

    for param, value in overrides.items():
        # Safety: never apply overrides for protected params
        if param in NEVER_ADJUST:
            print(f"  [SELF-IMPROVE] WARNING: Skipping protected param {param}")
            continue
        if param not in PARAM_BOUNDS:
            print(f"  [SELF-IMPROVE] WARNING: Skipping unknown param {param}")
            continue
        if hasattr(config, param):
            setattr(config, param, value)
            applied[param] = value
            print(f"  [SELF-IMPROVE] Override: {param} = {value}")

    return applied


# ═══════════════════════════════════════════════════════
# MEASUREMENT
# ═══════════════════════════════════════════════════════

def _measure_performance(days_back=7):
    """Compute weekly performance metrics from P&L and trade history."""
    # Load P&L data
    pnl_data = _load_pnl_history()
    if not pnl_data:
        return None

    date_pnl = pnl_data.get("date_pnl", {})
    if not date_pnl:
        return None

    # Filter to lookback window
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    recent_dates = sorted(d for d in date_pnl if d >= cutoff)

    if not recent_dates:
        return None

    # Daily P&L series
    daily_pnls = []
    total_wins = 0
    total_losses = 0

    for d in recent_dates:
        day = date_pnl[d]
        pnl_cents = day.get("pnl_cents", 0)
        daily_pnls.append(pnl_cents)
        total_wins += day.get("wins", 0)
        total_losses += day.get("losses", 0)

    total_trades = total_wins + total_losses
    if total_trades < config.SELF_IMPROVE_MIN_TRADES:
        print(f"  [SELF-IMPROVE] Only {total_trades} trades in {days_back}d "
              f"(need {config.SELF_IMPROVE_MIN_TRADES})")
        return None

    # Sharpe (annualized from daily, sqrt(7) for weekly)
    if len(daily_pnls) >= 2:
        mean_pnl = sum(daily_pnls) / len(daily_pnls)
        variance = sum((p - mean_pnl) ** 2 for p in daily_pnls) / len(daily_pnls)
        std_pnl = math.sqrt(variance) if variance > 0 else 1.0
        sharpe = (mean_pnl / std_pnl) * math.sqrt(len(daily_pnls))
    else:
        sharpe = 0.0

    # Max drawdown as % of starting balance
    balance_cents = getattr(config, "TOTAL_DEPOSITS_CENTS", 10000)
    current_balance = pnl_data.get("balance_cents")
    if current_balance is not None:
        balance_cents = current_balance

    cumulative = 0
    peak = 0
    max_dd_cents = 0
    for p in daily_pnls:
        cumulative += p
        peak = max(peak, cumulative)
        dd = peak - cumulative
        max_dd_cents = max(max_dd_cents, dd)

    max_dd_pct = max_dd_cents / balance_cents if balance_cents > 0 else 0

    # Win rate
    win_rate = total_wins / total_trades if total_trades > 0 else 0

    # Edge realization from trade log
    trades = _load_settled_trades(days_back)
    edge_realization = _compute_edge_realization(trades)

    # Breakdowns for diagnosis
    breakdowns = _compute_breakdowns(trades)

    return {
        "sharpe": round(sharpe, 3),
        "max_dd_pct": round(max_dd_pct, 4),
        "win_rate": round(win_rate, 4),
        "edge_realization": round(edge_realization, 4),
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": total_losses,
        "total_pnl_cents": sum(daily_pnls),
        "days_counted": len(recent_dates),
        "balance_cents": balance_cents,
        "breakdowns": breakdowns,
    }


def _compute_edge_realization(trades):
    """Edge realization = mean(actual_return) / mean(predicted_edge).

    Predicted edge is entry edge; actual return is profit/cost.
    """
    if not trades:
        return 0.0

    predicted_edges = []
    actual_returns = []

    for t in trades:
        edge = t.get("edge", 0)
        cost = t.get("cost_cents", 0)
        profit = t.get("profit_cents")

        if edge <= 0 or cost <= 0 or profit is None:
            continue

        predicted_edges.append(edge)
        actual_returns.append(profit / cost)

    if not predicted_edges:
        return 0.0

    mean_predicted = sum(predicted_edges) / len(predicted_edges)
    mean_actual = sum(actual_returns) / len(actual_returns)

    if mean_predicted <= 0:
        return 0.0

    return mean_actual / mean_predicted


def _compute_breakdowns(trades):
    """Break down performance by side, city, strategy for diagnosis."""
    by_side = {"YES": {"wins": 0, "losses": 0, "pnl": 0},
               "NO": {"wins": 0, "losses": 0, "pnl": 0}}
    by_city = {}
    by_strategy = {}

    for t in trades:
        profit = t.get("profit_cents")
        if profit is None:
            continue

        side = t.get("side", "")
        city = t.get("city_code", t.get("city", ""))
        strat = t.get("strategy", "unknown")

        # By side
        if side in by_side:
            bucket = by_side[side]
            if profit > 0:
                bucket["wins"] += 1
            elif profit < 0:
                bucket["losses"] += 1
            bucket["pnl"] += profit

        # By city
        if city:
            if city not in by_city:
                by_city[city] = {"wins": 0, "losses": 0, "pnl": 0}
            if profit > 0:
                by_city[city]["wins"] += 1
            elif profit < 0:
                by_city[city]["losses"] += 1
            by_city[city]["pnl"] += profit

        # By strategy
        if strat not in by_strategy:
            by_strategy[strat] = {"wins": 0, "losses": 0, "pnl": 0}
        if profit > 0:
            by_strategy[strat]["wins"] += 1
        elif profit < 0:
            by_strategy[strat]["losses"] += 1
        by_strategy[strat]["pnl"] += profit

    return {
        "by_side": by_side,
        "by_city": by_city,
        "by_strategy": by_strategy,
    }


# ═══════════════════════════════════════════════════════
# DIAGNOSIS
# ═══════════════════════════════════════════════════════

def _find_shortfalls(metrics):
    """Compare metrics against targets, return list of shortfalls."""
    shortfalls = []

    checks = [
        ("sharpe", metrics["sharpe"], config.SELF_IMPROVE_TARGET_SHARPE, "above"),
        ("max_dd_pct", metrics["max_dd_pct"], config.SELF_IMPROVE_TARGET_MAX_DD_PCT, "below"),
        ("win_rate", metrics["win_rate"], config.SELF_IMPROVE_TARGET_WIN_RATE, "above"),
        ("edge_realization", metrics["edge_realization"], config.SELF_IMPROVE_TARGET_EDGE_REALIZATION, "above"),
    ]

    for metric_name, actual, target, direction in checks:
        if direction == "above" and actual < target:
            shortfalls.append({"metric": metric_name, "actual": actual,
                               "target": target, "direction": direction})
        elif direction == "below" and actual > target:
            shortfalls.append({"metric": metric_name, "actual": actual,
                               "target": target, "direction": direction})

    return shortfalls


def _diagnose_underperformance(metrics, shortfalls):
    """Map shortfalls to max 3 parameter proposals."""
    proposals = []
    max_proposals = config.SELF_IMPROVE_MAX_PROPOSALS
    shortfall_names = {sf["metric"] for sf in shortfalls}
    breakdowns = metrics.get("breakdowns", {})

    # Priority 1: Edge realization too low → raise MIN_EDGE
    if "edge_realization" in shortfall_names and len(proposals) < max_proposals:
        sf = next(s for s in shortfalls if s["metric"] == "edge_realization")
        gap = sf["target"] - sf["actual"]
        current = getattr(config, "MIN_EDGE", 0.10)
        # Increase proportional to gap (bigger gap → bigger increase)
        adjustment = current * min(gap, 0.30)  # Cap raw adjustment
        proposed = _clamp_proposal("MIN_EDGE", current, current + adjustment)
        if proposed is not None and proposed != current:
            proposals.append({
                "param": "MIN_EDGE",
                "current": current,
                "proposed": proposed,
                "reason": f"edge_realization {sf['actual']:.2f} < {sf['target']:.2f}",
            })

    # Priority 2: Win rate too low → adjust NO sizing or rounding buffer
    if "win_rate" in shortfall_names and len(proposals) < max_proposals:
        sf = next(s for s in shortfalls if s["metric"] == "win_rate")
        by_side = breakdowns.get("by_side", {})

        no_data = by_side.get("NO", {})
        no_total = no_data.get("wins", 0) + no_data.get("losses", 0)
        no_wr = no_data["wins"] / no_total if no_total > 0 else 1.0

        yes_data = by_side.get("YES", {})
        yes_total = yes_data.get("wins", 0) + yes_data.get("losses", 0)
        yes_wr = yes_data["wins"] / yes_total if yes_total > 0 else 1.0

        if no_total >= 3 and no_wr < yes_wr:
            # NO side dragging win rate down → reduce NO sizing
            current = getattr(config, "NO_SIDE_SIZING_MULTIPLIER", 0.40)
            proposed = _clamp_proposal("NO_SIDE_SIZING_MULTIPLIER", current,
                                       current * 0.80)  # 20% reduction
            if proposed is not None and proposed != current:
                proposals.append({
                    "param": "NO_SIDE_SIZING_MULTIPLIER",
                    "current": current,
                    "proposed": proposed,
                    "reason": f"NO win_rate {no_wr:.2f} < YES {yes_wr:.2f}",
                })
        else:
            # General win rate issue → widen rounding buffer
            current = getattr(config, "ROUNDING_BUFFER_SOFT_F", 2.0)
            proposed = _clamp_proposal("ROUNDING_BUFFER_SOFT_F", current,
                                       current + 0.5)
            if proposed is not None and proposed != current:
                proposals.append({
                    "param": "ROUNDING_BUFFER_SOFT_F",
                    "current": current,
                    "proposed": proposed,
                    "reason": f"win_rate {sf['actual']:.2f} < {sf['target']:.2f}",
                })

    # Priority 3: Drawdown too high → reduce sizing multipliers
    if "max_dd_pct" in shortfall_names and len(proposals) < max_proposals:
        sf = next(s for s in shortfalls if s["metric"] == "max_dd_pct")

        # Check if next-day trades contributed to losses
        current = getattr(config, "PRE_SETTLEMENT_SIZING_MULT", 0.75)
        proposed = _clamp_proposal("PRE_SETTLEMENT_SIZING_MULT", current,
                                   current * 0.85)  # 15% reduction
        if proposed is not None and proposed != current:
            proposals.append({
                "param": "PRE_SETTLEMENT_SIZING_MULT",
                "current": current,
                "proposed": proposed,
                "reason": f"max_dd {sf['actual']:.1%} > {sf['target']:.1%}",
            })

    # Priority 4: Sharpe too low → widen maker spread for better fills
    if "sharpe" in shortfall_names and len(proposals) < max_proposals:
        sf = next(s for s in shortfalls if s["metric"] == "sharpe")
        current = getattr(config, "MAKER_SPREAD_BUFFER_CENTS", 2)
        proposed = _clamp_proposal("MAKER_SPREAD_BUFFER_CENTS", current,
                                   current + 1)
        if proposed is not None and proposed != current:
            proposals.append({
                "param": "MAKER_SPREAD_BUFFER_CENTS",
                "current": current,
                "proposed": proposed,
                "reason": f"sharpe {sf['actual']:.2f} < {sf['target']:.2f}",
            })

    return proposals[:max_proposals]


def _clamp_proposal(param, current, proposed):
    """Enforce max 25% change and hard bounds. Returns clamped value or None."""
    if param not in PARAM_BOUNDS:
        return None

    lo, hi = PARAM_BOUNDS[param]
    max_change = config.SELF_IMPROVE_MAX_CHANGE_PCT

    # Enforce max % change from current
    if current > 0:
        max_delta = current * max_change
        # For integer params, ensure at least ±1 change is possible
        if isinstance(current, int):
            max_delta = max(max_delta, 1.0)
        if proposed > current:
            proposed = min(proposed, current + max_delta)
        else:
            proposed = max(proposed, current - max_delta)

    # Enforce hard bounds
    proposed = max(lo, min(hi, proposed))

    # Round appropriately
    if isinstance(current, int):
        # Use math.floor(x + 0.5) to avoid Python's banker's rounding
        # (round(2.5) = 2, but we want 3)
        proposed = int(math.floor(proposed + 0.5))
    else:
        proposed = round(proposed, 4)

    return proposed


# ═══════════════════════════════════════════════════════
# REPLAY BACKTEST
# ═══════════════════════════════════════════════════════

def _replay_backtest(trades, proposals, original_metrics):
    """Replay actual trades with proposed changes. Returns improvement %.

    For each historical trade, ask: with the proposed parameter changes,
    would this trade have been taken? At what size? P&L scaling is linear
    because our limit orders don't move the market.
    """
    if not trades or not proposals:
        return 0.0

    # Build proposal lookup
    param_changes = {p["param"]: p for p in proposals}

    original_pnl = 0
    hypothetical_pnl = 0

    # Get proposed values (fall back to current config)
    proposed_min_edge = param_changes.get("MIN_EDGE", {}).get(
        "proposed", getattr(config, "MIN_EDGE", 0.10))
    proposed_divergence = param_changes.get("MAX_MODEL_DIVERGENCE_F", {}).get(
        "proposed", getattr(config, "MAX_MODEL_DIVERGENCE_F", 4.0))

    # Sizing multiplier ratios
    sizing_ratios = {}
    for param in ("NO_SIDE_SIZING_MULTIPLIER", "NEXT_DAY_SIZING_MULTIPLIER",
                  "PRE_SETTLEMENT_SIZING_MULT"):
        if param in param_changes:
            current = param_changes[param]["current"]
            proposed = param_changes[param]["proposed"]
            if current > 0:
                sizing_ratios[param] = proposed / current

    for t in trades:
        profit = t.get("profit_cents")
        if profit is None:
            continue

        original_pnl += profit
        edge = t.get("edge", 0)
        side = t.get("side", "")
        confirmation = t.get("confirmation", "")

        # Confirmed outcomes and arbitrage always kept at 1.0x
        if confirmation == "CONFIRMED_OUTCOME" or t.get("strategy") == "S2-Arbitrage":
            hypothetical_pnl += profit
            continue

        # Check MIN_EDGE filter
        if "MIN_EDGE" in param_changes and edge < proposed_min_edge:
            # Trade would not have been taken
            continue

        # Check model divergence (if model_predictions recorded)
        model_preds = t.get("model_predictions", {})
        if "MAX_MODEL_DIVERGENCE_F" in param_changes and model_preds:
            temps = [v for v in model_preds.values() if isinstance(v, (int, float))]
            if len(temps) >= 2:
                spread = max(temps) - min(temps)
                if spread > proposed_divergence:
                    continue

        # Apply sizing ratio changes
        size_mult = 1.0
        if side == "NO" and "NO_SIDE_SIZING_MULTIPLIER" in sizing_ratios:
            size_mult *= sizing_ratios["NO_SIDE_SIZING_MULTIPLIER"]
        if "PRE_SETTLEMENT_SIZING_MULT" in sizing_ratios:
            # Only apply to trades placed before settlement hour (10 AM ET)
            timestamp = t.get("timestamp", "")
            if timestamp and "T" in timestamp:
                try:
                    hour = int(timestamp.split("T")[1][:2])
                    if hour < 10:  # Pre-settlement trades
                        size_mult *= sizing_ratios["PRE_SETTLEMENT_SIZING_MULT"]
                except (ValueError, IndexError):
                    pass
        if "NEXT_DAY_SIZING_MULTIPLIER" in sizing_ratios:
            # Only apply to next-day trades (heuristic: placed before noon)
            timestamp = t.get("timestamp", "")
            if timestamp and "T" in timestamp:
                try:
                    hour = int(timestamp.split("T")[1][:2])
                    if hour < 12:  # Rough next-day heuristic
                        size_mult *= sizing_ratios["NEXT_DAY_SIZING_MULTIPLIER"]
                except (ValueError, IndexError):
                    pass

        hypothetical_pnl += profit * size_mult

    # Compute improvement %
    if original_pnl == 0:
        return 0.0

    # Improvement = (hypothetical - original) / |original| × 100
    # Positive = better (more profit or less loss), negative = worse
    improvement = ((hypothetical_pnl - original_pnl) / abs(original_pnl)) * 100

    return round(improvement, 2)


# ═══════════════════════════════════════════════════════
# OVERRIDE MANAGEMENT
# ═══════════════════════════════════════════════════════

def _apply_overrides(proposals):
    """Write overrides to config_overrides.json and apply immediately."""
    overrides = {}
    for p in proposals:
        param = p["param"]
        value = p["proposed"]

        # Double-check safety
        if param in NEVER_ADJUST or param not in PARAM_BOUNDS:
            continue

        overrides[param] = value
        # Apply immediately
        if hasattr(config, param):
            setattr(config, param, value)

    now = datetime.now(timezone.utc)
    data = {
        "overrides": overrides,
        "applied_at": now.isoformat(),
        "expires_at": (now + timedelta(days=7)).isoformat(),
        "proposals": proposals,
    }

    try:
        with open(config.CONFIG_OVERRIDES_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        print(f"  [SELF-IMPROVE] Error writing overrides: {e}")


# ═══════════════════════════════════════════════════════
# DATA LOADING
# ═══════════════════════════════════════════════════════

def _load_pnl_history():
    """Load pnl_history.json."""
    try:
        with open(config.PNL_HISTORY_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _load_settled_trades(days_back=7):
    """Load settled trades from trade_history.json within lookback window."""
    try:
        with open(config.TRADE_LOG_FILE) as f:
            all_trades = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

    cutoff = (datetime.now() - timedelta(days=days_back)).isoformat()

    settled = []
    for t in all_trades:
        # Filter by time
        ts = t.get("timestamp", "")
        if ts < cutoff:
            continue

        # Filter by status — must be settled
        status = t.get("status", "")
        order_status = t.get("order_status", "")
        if status in SKIP_STATUSES or order_status in SKIP_STATUSES:
            continue

        # Must have a profit_cents (settled)
        if t.get("profit_cents") is None and not t.get("settled"):
            continue

        settled.append(t)

    return settled


# ═══════════════════════════════════════════════════════
# LOGGING & DISPLAY
# ═══════════════════════════════════════════════════════

def _print_metrics(metrics):
    """Print formatted metrics summary."""
    print(f"\n  [SELF-IMPROVE] Weekly Metrics ({metrics['days_counted']}d, "
          f"{metrics['total_trades']} trades):")
    print(f"    Sharpe:           {metrics['sharpe']:>7.3f}  "
          f"(target: {config.SELF_IMPROVE_TARGET_SHARPE})")
    print(f"    Max Drawdown:     {metrics['max_dd_pct']:>7.1%}  "
          f"(target: <{config.SELF_IMPROVE_TARGET_MAX_DD_PCT:.0%})")
    print(f"    Win Rate:         {metrics['win_rate']:>7.1%}  "
          f"(target: {config.SELF_IMPROVE_TARGET_WIN_RATE:.0%})")
    print(f"    Edge Realization: {metrics['edge_realization']:>7.1%}  "
          f"(target: {config.SELF_IMPROVE_TARGET_EDGE_REALIZATION:.0%})")
    print(f"    Total P&L:        {metrics['total_pnl_cents']:>+7d}¢  "
          f"({metrics['total_wins']}W/{metrics['total_losses']}L)")


def _save_improvement_log(review):
    """Append review to improvement_log.json."""
    log = []
    try:
        with open(config.IMPROVEMENT_LOG_FILE) as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    log.append(review)

    # Keep last 52 reviews (1 year of weekly reviews)
    log = log[-52:]

    try:
        with open(config.IMPROVEMENT_LOG_FILE, "w") as f:
            json.dump(log, f, indent=2)
    except OSError as e:
        print(f"  [SELF-IMPROVE] Error saving log: {e}")
