"""
TRADE REVIEWER & LEARNING SYSTEM v1.0
=======================================
Learns from trade history to improve future decisions.
Runs once daily after 11 PM ET. Idempotent.

What it learns:
  1. Per-city forecast bias (EMA, rolling 30-point window)
  2. Per-model accuracy weights (MAE-based)
  3. Win/loss patterns by time, side, city, edge bucket
  4. Daily report generation

Single writer for learning_state.json.
"""

import json
import math
import os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import requests
import config
from weather_engine import CITIES


class TradeReviewer:
    """Learns from trade history to improve future decisions."""

    def __init__(self):
        self.state = self._load_state()
        self._ensure_defaults()

    def _ensure_defaults(self):
        defaults = {
            "city_biases": {},
            "model_accuracy": {},
            "forecast_snapshots": [],
            "daily_reports": [],
            "patterns": {},
            "last_review_date": "",
            "actual_temps": {},
            "scan_snapshots": {},
            "scan_reconciliation": [],
            "guard_stats": {},
            "calibration": {},
            "profitability": {},
            "information_decay": {},
        }
        for k, v in defaults.items():
            if k not in self.state:
                self.state[k] = v

        # Prune actual_temps older than 30 days
        if "actual_temps" in self.state and len(self.state["actual_temps"]) > 100:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
            self.state["actual_temps"] = {
                k: v for k, v in self.state["actual_temps"].items()
                if k.split("_")[-1] >= cutoff  # key format: {city}_{date}
            }

    # ===========================================================
    # PUBLIC API
    # ===========================================================

    def check_and_run(self):
        """Called every cycle. Runs review once per day after 11 PM ET."""
        et_now = datetime.now(ZoneInfo("America/New_York"))
        today = et_now.strftime("%Y-%m-%d")

        if et_now.hour < 23:
            return
        if self.state["last_review_date"] == today:
            return

        print("  [REVIEW] Running daily trade review...")
        trade_log = self._load_trade_log()
        if not trade_log:
            print("  [REVIEW] No trade history to review")
            self.state["last_review_date"] = today
            self._save_state()
            return

        self._learn_forecast_bias(trade_log)
        self._learn_model_accuracy(trade_log)
        self._analyze_patterns(trade_log)
        self._compute_profitability_metrics(trade_log)
        self._reconcile_scans(today)
        self._analyze_guard_effectiveness()
        self._analyze_calibration()
        self._analyze_information_decay()
        self._generate_daily_report(trade_log, today)

        # --- Learning pipeline: cache actuals + compress history ---
        try:
            yesterday = (et_now - timedelta(days=1)).strftime("%Y-%m-%d")
            self._cache_actual_temps([yesterday, today])
            self._compress_daily_record(yesterday)
            # Retry any recent dates with missing actuals
            self._retry_missing_actuals()
        except Exception as e:
            print("  [REVIEW] Learning pipeline error: %s" % e)

        self.state["last_review_date"] = today
        self._save_state()
        print("  [REVIEW] Daily review complete")

    def capture_forecast_snapshot(self, signal):
        """Called when a trade is placed. Saves forecast data for later learning."""
        if not signal or signal.get("signal") != "buy":
            return

        snapshot = {
            "ticker": signal.get("ticker", ""),
            "city_code": signal.get("city_code", ""),
            "forecast_mean": signal.get("predicted_high"),
            "model_means": signal.get("model_means", {}),
            "model_stds": signal.get("model_stds", {}),
            "market_price_cents": signal.get("price_cents", 0),
            "our_prob": signal.get("our_prob", 0),
            "side": signal.get("side", ""),
            "edge": signal.get("edge", 0),
            "strategy": signal.get("strategy", ""),
            "confirmation_verdict": signal.get("confirmation_verdict", ""),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "target_date": signal.get("target_date", ""),
        }

        self.state["forecast_snapshots"].append(snapshot)
        # Keep last 500 snapshots
        if len(self.state["forecast_snapshots"]) > 500:
            self.state["forecast_snapshots"] = self.state["forecast_snapshots"][-500:]
        self._save_state()

    def get_city_biases(self):
        """Returns learned per-city forecast biases."""
        return dict(self.state.get("city_biases", {}))

    def get_model_weights(self):
        """Returns learned per-model accuracy weights."""
        return dict(self.state.get("model_accuracy", {}))

    def seed_backtest_weights(self, backtest_file=None):
        """Seed per-city per-model weights from backtest_results.json.

        Only seeds cities that don't already have sufficient live learned data.
        Live learning (when enough data accumulates) will override these.
        """
        if backtest_file is None:
            backtest_file = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "backtest_results.json"
            )
        if not os.path.exists(backtest_file):
            print("  [REVIEW] Backtest file not found: %s" % backtest_file)
            return

        with open(backtest_file, "r") as f:
            backtest = json.load(f)

        # Compute per-city per-model MAE from daily records
        city_model_errors = {}  # city -> model -> [errors]
        for rec in backtest.get("daily_records", []):
            for city, cdata in rec.get("cities", {}).items():
                if city not in city_model_errors:
                    city_model_errors[city] = {}
                for model, mdata in cdata.get("models", {}).items():
                    err = mdata.get("error")
                    if err is not None:
                        if model not in city_model_errors[city]:
                            city_model_errors[city][model] = []
                        city_model_errors[city][model].append(abs(err))

        # Compute normalized 1/MAE weights per city
        if "model_accuracy" not in self.state:
            self.state["model_accuracy"] = {}

        seeded = 0
        for city, models in city_model_errors.items():
            # Skip if city already has live learned weights with sufficient data
            existing = self.state["model_accuracy"].get(city, {})
            has_live = any(
                isinstance(v, dict) and len(v.get("errors", [])) >= 5
                for v in existing.values()
            )
            if has_live:
                continue

            # Compute MAE and inverse-MAE weights
            maes = {}
            for model, errors in models.items():
                if errors:
                    maes[model] = sum(errors) / len(errors)

            if not maes:
                continue

            raw_weights = {m: 1.0 / (mae + 0.5) for m, mae in maes.items()}
            total = sum(raw_weights.values())
            norm_weights = {m: round(w / total, 3) for m, w in raw_weights.items()}

            self.state["model_accuracy"][city] = {}
            for model in maes:
                self.state["model_accuracy"][city][model] = {
                    "errors": [],
                    "mae": round(maes[model], 2),
                    "weight": norm_weights[model],
                    "crps_errors": [],
                    "crps": None,
                    "source": "backtest",
                }
            seeded += 1

        self._save_state()
        print("  [REVIEW] Seeded backtest weights for %d cities" % seeded)

    def get_losing_patterns(self):
        """Returns per-city combos with ≥5 trades and <20% win rate.

        Used by strategy to auto-block trades that are statistically losing.
        Only blocks per-city patterns (NOT per-side — blocking an entire side
        would shut down the bot during losing streaks).

        Requires 5+ settled trades for a specific city before blocking.
        """
        patterns = self.state.get("patterns", {})
        result = {}

        # Per-city blocking only (not per-side — both sides can lose during drawdowns)
        by_city = patterns.get("by_city", {})
        for city, stats in by_city.items():
            total = stats.get("wins", 0) + stats.get("losses", 0)
            if total >= 5:
                win_rate = stats.get("wins", 0) / total
                if win_rate < 0.20:
                    result[f"city:{city}"] = {
                        "win_rate": round(win_rate, 3),
                        "total": total,
                        "wins": stats.get("wins", 0),
                        "losses": stats.get("losses", 0),
                    }

        return result

    def capture_scan_snapshot(self, all_signals):
        """Called once per cycle with ALL evaluated signals (buy + skip).

        Stores deduplicated snapshots per ticker per day. Keeps 30 days.
        Only stores signals that have forecast data (city_code + predicted_high).
        """
        if not all_signals:
            return

        et_now = datetime.now(ZoneInfo("America/New_York"))
        today = et_now.strftime("%Y-%m-%d")

        if "scan_snapshots" not in self.state:
            self.state["scan_snapshots"] = {}

        day_snaps = self.state["scan_snapshots"].get(today, {})

        for sig in all_signals:
            ticker = sig.get("ticker", "")
            if not ticker:
                continue
            # Only store signals with meaningful forecast data
            if not sig.get("city_code") or sig.get("predicted_high") is None:
                continue

            day_snaps[ticker] = {
                "ticker": ticker,
                "city_code": sig.get("city_code", ""),
                "target_date": sig.get("target_date", ""),
                "signal": sig.get("signal", "skip"),
                "side": sig.get("side", ""),
                "edge": sig.get("edge", 0),
                "our_prob": sig.get("our_prob", 0),
                "market_prob": sig.get("market_prob", 0),
                "price_cents": sig.get("price_cents", 0),
                "predicted_high": sig.get("predicted_high"),
                "model_means": sig.get("model_means", {}),
                "model_spread": sig.get("model_spread"),
                "temp_low": sig.get("temp_low"),
                "temp_high": sig.get("temp_high"),
                "yes_price_cents": sig.get("yes_price_cents"),
                "no_price_cents": sig.get("no_price_cents"),
                "todays_high_snapshot": sig.get("todays_high_snapshot"),
                "market_title": sig.get("market_title", ""),
                "market_subtitle": sig.get("market_subtitle", ""),
                "event_ticker": sig.get("event_ticker", ""),
                "strike_type": sig.get("strike_type", ""),
                "floor_strike": sig.get("floor_strike"),
                "cap_strike": sig.get("cap_strike"),
                "skip_reason": sig.get("skip_reason"),
                "confirmation_verdict": sig.get("confirmation_verdict", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        self.state["scan_snapshots"][today] = day_snaps

        # Prune to 30 days (was 7 — need more data for learning)
        all_dates = sorted(self.state["scan_snapshots"].keys())
        while len(all_dates) > 30:
            del self.state["scan_snapshots"][all_dates.pop(0)]

        # Save periodically (not every cycle -- every 10th call)
        cycle_count = getattr(self, "_scan_snap_counter", 0) + 1
        self._scan_snap_counter = cycle_count
        if cycle_count % 10 == 0:
            self._save_state()

    def get_learning_summary(self):
        """Summary for dashboard /api/learning endpoint."""
        reports = self.state.get("daily_reports", [])
        latest_report = reports[-1] if reports else None
        reconciliations = self.state.get("scan_reconciliation", [])
        latest_recon = reconciliations[-1] if reconciliations else None
        return {
            "city_biases": self.state.get("city_biases", {}),
            "model_accuracy": self.state.get("model_accuracy", {}),
            "patterns": self.state.get("patterns", {}),
            "latest_report": latest_report,
            "total_snapshots": len(self.state.get("forecast_snapshots", [])),
            "last_review_date": self.state.get("last_review_date", ""),
            "scan_reconciliation": latest_recon,
            "guard_stats": self.state.get("guard_stats", {}),
            "calibration": self.state.get("calibration", {}),
            "profitability": self.state.get("profitability", {}),
            "information_decay": self.state.get("information_decay", {}),
        }

    # ===========================================================
    # LEARNING: FORECAST BIAS
    # ===========================================================

    def _learn_forecast_bias(self, trade_log):
        """Learn per-city forecast bias from snapshots vs actual outcomes.

        Compares forecast_mean from snapshots against actual NWS settlement.
        Uses exponential moving average (alpha=0.15), rolling 30-point window.
        Positive bias = model runs hot (forecast > actual).
        """
        # Match snapshots to settled trades (only real buy_fill entries)
        settled = {}
        for t in trade_log:
            if t.get("entry_type") != "buy_fill":
                continue
            ticker = t.get("ticker", "")
            if t.get("settled") or t.get("result") in ("win", "loss"):
                settled[ticker] = t

        alpha = 0.15
        max_points = 30
        biases = {}  # Rebuild from scratch each night to avoid duplicate errors

        for snap in self.state.get("forecast_snapshots", []):
            ticker = snap.get("ticker", "")
            city = snap.get("city_code", "")
            forecast = snap.get("forecast_mean")
            if not city or forecast is None:
                continue

            # Check if this trade settled
            trade = settled.get(ticker)
            if not trade:
                continue

            # We need the actual temperature to compute bias.
            # Use the trade result + bucket parsing to infer actual.
            # If the trade has actual_temp stored, use it directly.
            actual = trade.get("actual_temp")
            if actual is None:
                target_date = trade.get("target_date") or snap.get("target_date", "")
                if target_date:
                    actual = self._get_actual_temp(city, target_date)
            if actual is None:
                continue

            error = forecast - actual  # positive = model ran hot

            if city not in biases:
                biases[city] = {"bias": 0.0, "count": 0, "errors": []}

            entry = biases[city]
            entry["errors"].append(round(error, 2))
            if len(entry["errors"]) > max_points:
                entry["errors"] = entry["errors"][-max_points:]

            # Only compute bias with 3+ data points
            entry["count"] = len(entry["errors"])
            if entry["count"] >= 3:
                # EMA over errors
                ema = entry["errors"][0]
                for e in entry["errors"][1:]:
                    ema = alpha * e + (1 - alpha) * ema
                # Small sample discount: 3pts = 60%, 4pts = 80%, 5+ = 100%
                sample_factor = min(1.0, 0.4 + entry["count"] * 0.2)
                entry["bias"] = round(max(-8.0, min(8.0, ema * sample_factor)), 2)

        self.state["city_biases"] = biases

    # ===========================================================
    # LEARNING: MODEL ACCURACY
    # ===========================================================

    @staticmethod
    def _crps_gaussian(mean, std, actual):
        """CRPS for a Gaussian forecast distribution. Lower = better.

        Exact formula: CRPS = std * [z*(2*Phi(z) - 1) + 2*phi(z) - 1/sqrt(pi)]
        where z = (actual - mean) / std, Phi=CDF, phi=PDF of standard normal.
        """
        if std <= 0:
            return abs(mean - actual)  # Degenerate: CRPS = MAE
        try:
            from scipy.stats import norm
            z = (actual - mean) / std
            crps = std * (z * (2.0 * norm.cdf(z) - 1.0)
                          + 2.0 * norm.pdf(z) - 1.0 / math.sqrt(math.pi))
            return max(0.0, crps)
        except Exception:
            return abs(mean - actual)

    def _learn_model_accuracy(self, trade_log):
        """Learn per-model accuracy weights from forecast snapshots.

        Tracks MAE and CRPS per model per city. Prefers CRPS-based weights
        when per-model std devs are available; falls back to MAE.
        CRPS rewards sharp, well-calibrated distributions.
        """
        settled = {}
        for t in trade_log:
            if t.get("entry_type") != "buy_fill":
                continue
            ticker = t.get("ticker", "")
            if t.get("settled") or t.get("result") in ("win", "loss"):
                settled[ticker] = t

        accuracy = {}  # Rebuild from scratch each night to avoid duplicate errors

        for snap in self.state.get("forecast_snapshots", []):
            ticker = snap.get("ticker", "")
            city = snap.get("city_code", "")
            model_means = snap.get("model_means", {})
            if not city or not model_means:
                continue

            trade = settled.get(ticker)
            if not trade:
                continue

            actual = trade.get("actual_temp")
            if actual is None:
                target_date = trade.get("target_date") or snap.get("target_date", "")
                if target_date:
                    actual = self._get_actual_temp(city, target_date)
            if actual is None:
                continue

            if city not in accuracy:
                accuracy[city] = {}

            model_stds = snap.get("model_stds", {})

            for model_name, model_mean in model_means.items():
                if model_mean is None:
                    continue
                abs_error = abs(model_mean - actual)

                if model_name not in accuracy[city]:
                    accuracy[city][model_name] = {
                        "errors": [], "mae": None, "weight": None,
                        "crps_errors": [], "crps": None,
                    }

                entry = accuracy[city][model_name]
                entry["errors"].append(round(abs_error, 2))
                if len(entry["errors"]) > 30:
                    entry["errors"] = entry["errors"][-30:]

                if len(entry["errors"]) >= 5:
                    mae = sum(entry["errors"]) / len(entry["errors"])
                    entry["mae"] = round(mae, 2)

                # CRPS computation (when per-model std available)
                model_std = model_stds.get(model_name)
                if model_std is not None and model_std > 0:
                    crps_val = self._crps_gaussian(model_mean, model_std, actual)
                    if "crps_errors" not in entry:
                        entry["crps_errors"] = []
                    entry["crps_errors"].append(round(crps_val, 3))
                    if len(entry["crps_errors"]) > 30:
                        entry["crps_errors"] = entry["crps_errors"][-30:]
                    if len(entry["crps_errors"]) >= 5:
                        entry["crps"] = round(
                            sum(entry["crps_errors"]) / len(entry["crps_errors"]), 3)

            # Normalize weights for this city -- prefer CRPS over MAE
            # Require 3+ data points per model before computing weights (avoid noise)
            min_pts = getattr(config, 'LEARNING_MIN_DATA_POINTS', 3)
            scorable = {m: d for m, d in accuracy[city].items()
                        if (d.get("crps") is not None or d.get("mae") is not None)
                        and len(d.get("errors", [])) >= min_pts}
            if scorable:
                raw_weights = {}
                for m, d in scorable.items():
                    if d.get("crps") is not None:
                        raw_weights[m] = 1.0 / (d["crps"] + 0.5)
                    elif d.get("mae") is not None:
                        raw_weights[m] = 1.0 / (d["mae"] + 1.0)
                total_weight = sum(raw_weights.values())
                for m, w in raw_weights.items():
                    accuracy[city][m]["weight"] = round(w / total_weight, 3)

        self.state["model_accuracy"] = accuracy

    # ===========================================================
    # PROFITABILITY METRICS
    # ===========================================================

    def _compute_profitability_metrics(self, trade_log):
        """Compute profit factor and per-trade expectancy from settled trades.

        Profit factor = gross_wins / gross_losses (>1.5 good, >2.0 excellent).
        Expectancy = (win_rate * avg_win) - (loss_rate * avg_loss) in cents.
        """
        gross_wins_cents = 0
        gross_losses_cents = 0
        num_wins = 0
        num_losses = 0

        for t in trade_log:
            if t.get("entry_type") != "buy_fill":
                continue
            result = t.get("result")
            pnl = t.get("profit_cents", 0)
            if result == "win" and pnl > 0:
                gross_wins_cents += pnl
                num_wins += 1
            elif result == "loss" and pnl < 0:
                gross_losses_cents += abs(pnl)
                num_losses += 1

        total = num_wins + num_losses
        if total == 0:
            return

        win_rate = num_wins / total
        loss_rate = 1.0 - win_rate
        avg_win = gross_wins_cents / num_wins if num_wins > 0 else 0
        avg_loss = gross_losses_cents / num_losses if num_losses > 0 else 0

        profit_factor = None
        if gross_losses_cents > 0:
            profit_factor = round(gross_wins_cents / gross_losses_cents, 2)

        expectancy_cents = round((win_rate * avg_win) - (loss_rate * avg_loss), 1)

        self.state["profitability"] = {
            "profit_factor": profit_factor,
            "expectancy_cents": expectancy_cents,
            "gross_wins_cents": gross_wins_cents,
            "gross_losses_cents": gross_losses_cents,
            "win_rate": round(win_rate, 3),
            "total_settled_trades": total,
            "avg_win_cents": round(avg_win, 1),
            "avg_loss_cents": round(avg_loss, 1),
        }

        pf_str = "%.2f" % profit_factor if profit_factor else "N/A"
        pf_quality = ""
        if profit_factor is not None:
            pf_quality = " (excellent)" if profit_factor >= 2.0 else \
                         " (good)" if profit_factor >= 1.5 else \
                         " (needs work)" if profit_factor >= 1.0 else " (losing)"
        print("  [REVIEW] Profitability: PF=%s%s, Expectancy=%+.1fc/trade, "
              "WinRate=%.0f%% (%d trades)" % (
                  pf_str, pf_quality, expectancy_cents, win_rate * 100, total))

    # ===========================================================
    # PATTERN ANALYSIS
    # ===========================================================

    def _analyze_patterns(self, trade_log):
        """Analyze win rates by time, side, city, edge bucket, verdict."""
        patterns = {
            "by_side": {"yes": {"wins": 0, "losses": 0}, "no": {"wins": 0, "losses": 0}},
            "by_city": {},
            "by_edge_bucket": {
                "5-10%": {"wins": 0, "losses": 0},
                "10-15%": {"wins": 0, "losses": 0},
                "15-20%": {"wins": 0, "losses": 0},
                "20%+": {"wins": 0, "losses": 0},
            },
            "by_verdict": {},
            "by_time": {
                "morning": {"wins": 0, "losses": 0},
                "afternoon": {"wins": 0, "losses": 0},
                "evening": {"wins": 0, "losses": 0},
            },
            "losing_patterns": [],
        }

        for t in trade_log:
            if t.get("entry_type") != "buy_fill":
                continue
            result = t.get("result")
            if result not in ("win", "loss"):
                continue

            is_win = result == "win"
            side = t.get("side", "")
            city = t.get("city_code", "")
            edge = t.get("edge", 0)
            verdict = t.get("confirmation_verdict", "")
            ts = t.get("timestamp", "")

            # By side
            if side in patterns["by_side"]:
                if is_win:
                    patterns["by_side"][side]["wins"] += 1
                else:
                    patterns["by_side"][side]["losses"] += 1

            # By city
            if city:
                if city not in patterns["by_city"]:
                    patterns["by_city"][city] = {"wins": 0, "losses": 0}
                if is_win:
                    patterns["by_city"][city]["wins"] += 1
                else:
                    patterns["by_city"][city]["losses"] += 1

            # By edge bucket
            if edge > 0:
                if edge < 0.10:
                    bucket = "5-10%"
                elif edge < 0.15:
                    bucket = "10-15%"
                elif edge < 0.20:
                    bucket = "15-20%"
                else:
                    bucket = "20%+"
                if is_win:
                    patterns["by_edge_bucket"][bucket]["wins"] += 1
                else:
                    patterns["by_edge_bucket"][bucket]["losses"] += 1

            # By verdict
            if verdict:
                if verdict not in patterns["by_verdict"]:
                    patterns["by_verdict"][verdict] = {"wins": 0, "losses": 0}
                if is_win:
                    patterns["by_verdict"][verdict]["wins"] += 1
                else:
                    patterns["by_verdict"][verdict]["losses"] += 1

            # By time of day
            if ts:
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    et = dt.astimezone(ZoneInfo("America/New_York"))
                    hour = et.hour
                    if hour < 12:
                        period = "morning"
                    elif hour < 17:
                        period = "afternoon"
                    else:
                        period = "evening"
                    if is_win:
                        patterns["by_time"][period]["wins"] += 1
                    else:
                        patterns["by_time"][period]["losses"] += 1
                except Exception:
                    pass

        # Identify losing patterns (< 30% win rate with 5+ trades)
        losing = []
        for category_name, category_data in patterns.items():
            if category_name == "losing_patterns":
                continue
            if isinstance(category_data, dict):
                for key, stats in category_data.items():
                    if isinstance(stats, dict) and "wins" in stats and "losses" in stats:
                        total = stats["wins"] + stats["losses"]
                        if total >= 5:
                            win_rate = stats["wins"] / total
                            stats["win_rate"] = round(win_rate, 3)
                            stats["total"] = total
                            if win_rate < 0.30:
                                losing.append({
                                    "category": category_name,
                                    "key": key,
                                    "win_rate": round(win_rate, 3),
                                    "total_trades": total,
                                })

        patterns["losing_patterns"] = losing
        self.state["patterns"] = patterns

    # ===========================================================
    # SCAN RECONCILIATION
    # ===========================================================

    def _reconcile_scans(self, today):
        """Reconcile today's scan snapshots against NWS actual temperatures.

        For every market we evaluated today, check what actually happened.
        Classifies each signal as: correct_skip, missed_opportunity,
        correct_trade, bad_trade, or unknown.
        """
        day_snaps = self.state.get("scan_snapshots", {}).get(today, {})
        if not day_snaps:
            print("  [REVIEW] No scan snapshots for %s" % today)
            return

        # Fetch actuals for all cities evaluated today
        city_dates = set()
        for snap in day_snaps.values():
            city = snap.get("city_code", "")
            tdate = snap.get("target_date", "")
            if city and tdate:
                city_dates.add((city, tdate))

        actuals = {}
        for city, tdate in city_dates:
            actual = self._get_actual_temp(city, tdate)
            if actual is not None:
                actuals["%s_%s" % (city, tdate)] = actual

        # Classify each signal
        results = {
            "date": today,
            "total_evaluated": len(day_snaps),
            "correct_skips": 0,
            "missed_opportunities": 0,
            "correct_trades": 0,
            "bad_trades": 0,
            "unknown": 0,
            "missed_by_guard": {},
            "missed_profit_potential_cents": 0,
            "forecast_accuracy": {},
            "missed_details": [],
        }

        for snap in day_snaps.values():
            city = snap.get("city_code", "")
            tdate = snap.get("target_date", "")
            actual = actuals.get("%s_%s" % (city, tdate))

            if actual is None:
                results["unknown"] += 1
                continue

            # Record forecast accuracy per city (deduped, keep last)
            predicted = snap.get("predicted_high")
            if predicted is not None and city:
                results["forecast_accuracy"][city] = {
                    "predicted": round(predicted, 1),
                    "actual": actual,
                    "error": round(predicted - actual, 1),
                }

            # Determine actual outcome for this ticker's bucket
            ticker = snap.get("ticker", "")
            would_yes_win = self._would_yes_win(ticker, actual)
            if would_yes_win is None:
                results["unknown"] += 1
                continue

            sig_type = snap.get("signal", "skip")
            side = snap.get("side", "")
            price = snap.get("price_cents", 0)

            if sig_type == "buy":
                # We traded this
                if side == "yes":
                    traded_wins = would_yes_win
                else:
                    traded_wins = not would_yes_win
                if traded_wins:
                    results["correct_trades"] += 1
                else:
                    results["bad_trades"] += 1
            else:
                # We skipped this -- would we have won?
                if side == "yes":
                    skip_would_win = would_yes_win
                elif side == "no":
                    skip_would_win = not would_yes_win
                else:
                    # No side selected (no edge either direction)
                    results["correct_skips"] += 1
                    continue

                if skip_would_win and price > 0:
                    results["missed_opportunities"] += 1
                    payout = 100 - price
                    results["missed_profit_potential_cents"] += payout
                    guard = snap.get("skip_reason", "unknown")
                    results["missed_by_guard"][guard] = \
                        results["missed_by_guard"].get(guard, 0) + 1
                    # Keep top 10 missed opportunities
                    if len(results["missed_details"]) < 10:
                        results["missed_details"].append({
                            "ticker": ticker,
                            "city": city,
                            "side": side,
                            "edge": snap.get("edge", 0),
                            "price": price,
                            "payout": payout,
                            "guard": guard,
                            "predicted": predicted,
                            "actual": actual,
                        })
                else:
                    results["correct_skips"] += 1

        # Print reconciliation summary
        print()
        print("  +======================================+")
        print("  |       SCAN RECONCILIATION: %s   |" % today)
        print("  +======================================+")
        print("  |  Evaluated: %-3d markets w/ forecasts |" % results["total_evaluated"])
        print("  |  Correct skips: %-3d                  |" % results["correct_skips"])
        print("  |  Missed opportunities: %-3d           |" % results["missed_opportunities"])
        print("  |  Missed profit: $%.2f               |" % (results["missed_profit_potential_cents"] / 100.0))
        print("  |  Correct trades: %-3d                 |" % results["correct_trades"])
        print("  |  Bad trades: %-3d                     |" % results["bad_trades"])
        print("  +======================================+")

        if results["missed_by_guard"]:
            print("  [RECON] Missed by guard:")
            for guard, count in sorted(results["missed_by_guard"].items(),
                                       key=lambda x: -x[1]):
                print("    %s: %d" % (guard, count))

        # Store (keep last 30 days)
        self.state["scan_reconciliation"].append(results)
        if len(self.state["scan_reconciliation"]) > 30:
            self.state["scan_reconciliation"] = \
                self.state["scan_reconciliation"][-30:]

    def _would_yes_win(self, ticker, actual_temp):
        """Given a ticker and actual temp, determine if YES wins.

        Parses bucket boundaries from ticker name.
        Returns True (YES wins), False (NO wins), or None (can't determine).
        """
        if not ticker or actual_temp is None:
            return None
        try:
            # Ticker format: KXHIGH{CITY}-{DATE}-T{temp} or -B{temp}
            parts = ticker.split("-")
            if len(parts) < 3:
                return None
            bucket_part = parts[-1]  # e.g., T54, B63.5, T78

            if bucket_part.startswith("T"):
                # "T54" means ">54F" -- YES wins if actual > threshold
                threshold = float(bucket_part[1:])
                return actual_temp > threshold
            elif bucket_part.startswith("B"):
                # "B63.5" means "63-64F" bucket -- YES wins if in range
                mid = float(bucket_part[1:])
                temp_low = int(mid)
                temp_high = temp_low + 1
                return temp_low <= actual_temp <= temp_high
        except (ValueError, IndexError):
            pass
        return None

    def _analyze_guard_effectiveness(self):
        """Analyze how effective each guard is at blocking losing trades.

        Uses reconciliation data to compute per-guard accuracy.
        """
        guard_stats = {}
        for recon in self.state.get("scan_reconciliation", []):
            # Count guard blocks from missed_by_guard
            for guard, count in recon.get("missed_by_guard", {}).items():
                if guard not in guard_stats:
                    guard_stats[guard] = {
                        "total_blocks": 0,
                        "would_have_won": 0,
                        "would_have_lost": 0,
                    }
                guard_stats[guard]["would_have_won"] += count

        # We need correct_skips per guard too -- but we only track
        # missed_by_guard (winners blocked). For correct skips we need
        # to aggregate from scan_snapshots vs actuals more carefully.
        # For now, estimate from the reconciliation totals.
        for recon in self.state.get("scan_reconciliation", []):
            day_snaps = self.state.get("scan_snapshots", {}).get(
                recon.get("date", ""), {})
            for snap in day_snaps.values():
                if snap.get("signal") != "skip":
                    continue
                guard = snap.get("skip_reason", "unknown")
                if guard not in guard_stats:
                    guard_stats[guard] = {
                        "total_blocks": 0,
                        "would_have_won": 0,
                        "would_have_lost": 0,
                    }
                guard_stats[guard]["total_blocks"] += 1

        # The would_have_won is already counted above from missed_by_guard.
        # would_have_lost = total_blocks - would_have_won (for known outcomes).
        for guard, stats in guard_stats.items():
            stats["would_have_lost"] = max(
                0, stats["total_blocks"] - stats["would_have_won"])
            known = stats["would_have_won"] + stats["would_have_lost"]
            if known > 0:
                stats["block_accuracy"] = round(
                    stats["would_have_lost"] / known, 3)
            else:
                stats["block_accuracy"] = None

        self.state["guard_stats"] = guard_stats

        # Print warnings for low-accuracy guards
        for guard, stats in guard_stats.items():
            acc = stats.get("block_accuracy")
            total = stats.get("total_blocks", 0)
            if acc is not None and total >= 30 and acc < 0.60:
                print("  [REVIEW] [!] GUARD WARNING: %s accuracy=%.0f%% "
                      "(%d blocks, %d were winners)" % (
                          guard, acc * 100, total,
                          stats["would_have_won"]))

    def _analyze_calibration(self):
        """Analyze probability calibration using ALL scan data.

        Groups predictions into 10% buckets and compares predicted
        probability vs actual outcome rate. Computes Brier score.
        """
        buckets = {}
        for label in ["0-10%", "10-20%", "20-30%", "30-40%", "40-50%",
                       "50-60%", "60-70%", "70-80%", "80-90%", "90-100%"]:
            buckets[label] = {"predictions": 0, "actual_wins": 0}

        brier_sum = 0.0
        brier_count = 0

        actuals_cache = self.state.get("actual_temps", {})

        for day_date, day_snaps in self.state.get("scan_snapshots", {}).items():
            for snap in day_snaps.values():
                city = snap.get("city_code", "")
                tdate = snap.get("target_date", "")
                our_prob = snap.get("our_prob", 0)
                ticker = snap.get("ticker", "")

                if not city or not tdate or not ticker:
                    continue

                actual = actuals_cache.get("%s_%s" % (city, tdate))
                if actual is None:
                    actual = self._get_actual_temp(city, tdate)
                if actual is None:
                    continue

                would_yes = self._would_yes_win(ticker, actual)
                if would_yes is None:
                    continue

                outcome = 1.0 if would_yes else 0.0

                # Brier score component
                brier_sum += (our_prob - outcome) ** 2
                brier_count += 1

                # Bucket
                pct = int(our_prob * 100)
                if pct >= 100:
                    label = "90-100%"
                elif pct < 0:
                    label = "0-10%"
                else:
                    low = (pct // 10) * 10
                    label = "%d-%d%%" % (low, low + 10)

                if label in buckets:
                    buckets[label]["predictions"] += 1
                    if would_yes:
                        buckets[label]["actual_wins"] += 1

        # Compute actual rates
        for label, data in buckets.items():
            if data["predictions"] > 0:
                data["actual_rate"] = round(
                    data["actual_wins"] / data["predictions"], 3)
            else:
                data["actual_rate"] = None

        brier_score = round(brier_sum / brier_count, 4) if brier_count > 0 else None

        # Brier decomposition: Brier = Reliability - Resolution + Uncertainty
        reliability = None
        resolution = None
        uncertainty = None
        if brier_count > 0:
            # Overall base rate
            total_wins = sum(b["actual_wins"] for b in buckets.values())
            base_rate = total_wins / brier_count if brier_count > 0 else 0.5
            uncertainty = round(base_rate * (1.0 - base_rate), 4)

            # Bucket midpoints: "10-20%" -> 0.15
            bucket_midpoints = {
                "0-10%": 0.05, "10-20%": 0.15, "20-30%": 0.25,
                "30-40%": 0.35, "40-50%": 0.45, "50-60%": 0.55,
                "60-70%": 0.65, "70-80%": 0.75, "80-90%": 0.85,
                "90-100%": 0.95,
            }

            rel_sum = 0.0
            res_sum = 0.0
            for label, data in buckets.items():
                n_k = data["predictions"]
                if n_k == 0:
                    continue
                f_k = bucket_midpoints.get(label, 0.5)
                o_k = data["actual_wins"] / n_k
                rel_sum += n_k * (f_k - o_k) ** 2
                res_sum += n_k * (o_k - base_rate) ** 2

            reliability = round(rel_sum / brier_count, 4)
            resolution = round(res_sum / brier_count, 4)

        self.state["calibration"] = {
            "buckets": buckets,
            "brier_score": brier_score,
            "total_predictions": brier_count,
            "reliability": reliability,
            "resolution": resolution,
            "uncertainty": uncertainty,
        }

        if brier_score is not None:
            quality = "excellent" if brier_score < 0.15 else \
                      "good" if brier_score < 0.20 else \
                      "fair" if brier_score < 0.25 else "poor"
            print("  [REVIEW] Calibration: Brier=%.4f (%s), %d predictions" % (
                brier_score, quality, brier_count))
            if reliability is not None:
                print("  [REVIEW]   Reliability=%.4f (lower=better), "
                      "Resolution=%.4f (higher=better), Uncertainty=%.4f" % (
                          reliability, resolution, uncertainty))

    # ===========================================================
    # INFORMATION DECAY CURVES
    # ===========================================================

    def _analyze_information_decay(self):
        """Analyze forecast accuracy by local hour of evaluation.

        Groups scan signals by local hour bucket, computes per-bucket:
        accuracy, avg_edge, edge_realized_pct, brier, count.

        Answers: "Are morning predictions more/less accurate than afternoon?"
        """
        hour_buckets = {
            "6-9": {"correct": 0, "total": 0, "edges": [], "edge_wins": 0,
                    "edge_total": 0, "brier_sum": 0.0, "brier_n": 0},
            "9-12": {"correct": 0, "total": 0, "edges": [], "edge_wins": 0,
                     "edge_total": 0, "brier_sum": 0.0, "brier_n": 0},
            "12-15": {"correct": 0, "total": 0, "edges": [], "edge_wins": 0,
                      "edge_total": 0, "brier_sum": 0.0, "brier_n": 0},
            "15-18": {"correct": 0, "total": 0, "edges": [], "edge_wins": 0,
                      "edge_total": 0, "brier_sum": 0.0, "brier_n": 0},
            "18+": {"correct": 0, "total": 0, "edges": [], "edge_wins": 0,
                    "edge_total": 0, "brier_sum": 0.0, "brier_n": 0},
        }

        actuals_cache = self.state.get("actual_temps", {})

        for day_date, day_snaps in self.state.get("scan_snapshots", {}).items():
            for snap in day_snaps.values():
                city = snap.get("city_code", "")
                tdate = snap.get("target_date", "")
                our_prob = snap.get("our_prob", 0)
                ticker = snap.get("ticker", "")
                edge = snap.get("edge", 0)
                ts_str = snap.get("timestamp", "")

                if not city or not tdate or not ticker or not ts_str:
                    continue

                actual = actuals_cache.get("%s_%s" % (city, tdate))
                if actual is None:
                    actual = self._get_actual_temp(city, tdate)
                if actual is None:
                    continue

                would_yes = self._would_yes_win(ticker, actual)
                if would_yes is None:
                    continue

                # Determine local hour from timestamp + city timezone
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    city_info = CITIES.get(city, {})
                    tz_name = city_info.get("timezone", "America/New_York")
                    local_hour = ts.astimezone(ZoneInfo(tz_name)).hour
                except Exception:
                    continue

                # Map to bucket
                if local_hour < 6:
                    continue  # Pre-6 AM blocked by strategy anyway
                elif local_hour < 9:
                    bucket_key = "6-9"
                elif local_hour < 12:
                    bucket_key = "9-12"
                elif local_hour < 15:
                    bucket_key = "12-15"
                elif local_hour < 18:
                    bucket_key = "15-18"
                else:
                    bucket_key = "18+"

                bucket = hour_buckets[bucket_key]
                outcome = 1.0 if would_yes else 0.0

                # Prediction direction correct?
                pred_yes = our_prob > 0.5
                correct = (pred_yes and would_yes) or (not pred_yes and not would_yes)
                bucket["total"] += 1
                if correct:
                    bucket["correct"] += 1

                # Edge realized?
                if edge > 0:
                    bucket["edges"].append(edge)
                    bucket["edge_total"] += 1
                    if correct:
                        bucket["edge_wins"] += 1

                # Brier per bucket
                bucket["brier_sum"] += (our_prob - outcome) ** 2
                bucket["brier_n"] += 1

        # Compile results
        decay = {}
        for key, b in hour_buckets.items():
            if b["total"] == 0:
                continue
            decay[key] = {
                "accuracy": round(b["correct"] / b["total"], 3),
                "avg_edge": round(sum(b["edges"]) / len(b["edges"]), 3) if b["edges"] else 0,
                "edge_realized_pct": round(b["edge_wins"] / b["edge_total"], 3) if b["edge_total"] > 0 else 0,
                "brier": round(b["brier_sum"] / b["brier_n"], 4) if b["brier_n"] > 0 else None,
                "count": b["total"],
            }

        if decay:
            self.state["information_decay"] = decay
            print("  [REVIEW] Information decay by local hour:")
            for key in ["6-9", "9-12", "12-15", "15-18", "18+"]:
                if key in decay:
                    d = decay[key]
                    print("    %s: acc=%.0f%% edge_real=%.0f%% brier=%.4f (n=%d)" % (
                        key, d["accuracy"] * 100, d["edge_realized_pct"] * 100,
                        d["brier"] or 0, d["count"]))

    # ===========================================================
    # DAILY REPORT
    # ===========================================================

    def _generate_daily_report(self, trade_log, today):
        """Generate daily summary report. Prints to console + stores in state."""
        # Filter today's settled trades
        todays_trades = []
        for t in trade_log:
            if t.get("entry_type") != "buy_fill":
                continue
            ts = t.get("timestamp", "")
            if today in ts and t.get("result") in ("win", "loss"):
                todays_trades.append(t)

        if not todays_trades:
            print("  [REVIEW] No settled trades today")
            return

        wins = sum(1 for t in todays_trades if t["result"] == "win")
        losses = sum(1 for t in todays_trades if t["result"] == "loss")
        total_pnl = sum(t.get("profit_cents", 0) for t in todays_trades)

        # Edge analysis
        win_edges = [t.get("edge", 0) for t in todays_trades if t["result"] == "win"]
        loss_edges = [t.get("edge", 0) for t in todays_trades if t["result"] == "loss"]
        avg_win_edge = sum(win_edges) / len(win_edges) if win_edges else 0
        avg_loss_edge = sum(loss_edges) / len(loss_edges) if loss_edges else 0

        # Per-city breakdown
        city_results = {}
        for t in todays_trades:
            city = t.get("city_code", "unknown")
            if city not in city_results:
                city_results[city] = {"wins": 0, "losses": 0, "pnl_cents": 0}
            if t["result"] == "win":
                city_results[city]["wins"] += 1
            else:
                city_results[city]["losses"] += 1
            city_results[city]["pnl_cents"] += t.get("profit_cents", 0)

        # Best/worst trade
        best = max(todays_trades, key=lambda t: t.get("profit_cents", 0))
        worst = min(todays_trades, key=lambda t: t.get("profit_cents", 0))

        report = {
            "date": today,
            "wins": wins,
            "losses": losses,
            "total_pnl_cents": total_pnl,
            "avg_win_edge": round(avg_win_edge, 4),
            "avg_loss_edge": round(avg_loss_edge, 4),
            "city_results": city_results,
            "best_trade": {
                "ticker": best.get("ticker", ""),
                "profit_cents": best.get("profit_cents", 0),
            },
            "worst_trade": {
                "ticker": worst.get("ticker", ""),
                "profit_cents": worst.get("profit_cents", 0),
            },
            "bias_alerts": self._check_bias_drift(),
        }

        # Print report to console
        print()
        print("  +======================================+")
        print("  |       DAILY TRADE REVIEW: %s      |" % today)
        print("  +======================================+")
        print("  |  Record: %dW / %dL                    |" % (wins, losses))
        print("  |  P&L: %+dc ($%+.2f)                |" % (total_pnl, total_pnl / 100.0))
        print("  |  Avg Edge: Win=%.1f%% Loss=%.1f%%     |" % (avg_win_edge * 100, avg_loss_edge * 100))
        prof = self.state.get("profitability", {})
        if prof.get("profit_factor") is not None:
            print("  |  Profit Factor: %-20s  |" % ("%.2f" % prof["profit_factor"]))
            print("  |  Expectancy: %+.1fc/trade           |" % prof.get("expectancy_cents", 0))
        print("  +======================================+")

        for city, cr in sorted(city_results.items()):
            print("  [REVIEW] %s: %dW/%dL, %+dc" % (
                city, cr["wins"], cr["losses"], cr["pnl_cents"]))

        if report["bias_alerts"]:
            for alert in report["bias_alerts"]:
                print("  [REVIEW] BIAS ALERT: %s" % alert)

        # Store report (keep last 30)
        self.state["daily_reports"].append(report)
        if len(self.state["daily_reports"]) > 30:
            self.state["daily_reports"] = self.state["daily_reports"][-30:]

    def _get_actual_temp(self, city_code, target_date):
        """Fetch actual max temp from NWS for a city+date. Caches results."""
        cache_key = "%s_%s" % (city_code, target_date)
        cached = self.state.get("actual_temps", {})
        if cache_key in cached:
            return cached[cache_key]

        # Only look up dates within last 7 days
        try:
            from datetime import timedelta
            target = datetime.strptime(target_date, "%Y-%m-%d")
            if (datetime.now(timezone.utc) - target).days > 7:
                return None
        except Exception:
            return None

        city_info = CITIES.get(city_code)
        if not city_info:
            return None
        station = city_info.get("nws_station", "")
        if not station:
            return None

        try:
            # Use a wide UTC window to capture all local-day observations.
            # West Coast 4 PM PT = midnight UTC next day, so extend +8h past midnight.
            from datetime import timedelta as _td
            end_date = (datetime.strptime(target_date, "%Y-%m-%d") + _td(days=1, hours=8))
            url = "https://api.weather.gov/stations/%s/observations" % station
            params = {
                "start": "%sT06:00:00Z" % target_date,
                "end": end_date.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            headers = {"User-Agent": "KalshiBot/4.0", "Accept": "application/geo+json"}
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                print("  [REVIEWER] NWS API error for %s/%s: HTTP %d" % (city_code, target_date, resp.status_code))
                return None

            features = resp.json().get("features", [])
            max_temp = None
            for obs in features:
                temp_c = obs.get("properties", {}).get("temperature", {}).get("value")
                if temp_c is not None:
                    import math
                    temp_f = math.floor(temp_c * 9 / 5 + 32)
                    if max_temp is None or temp_f > max_temp:
                        max_temp = temp_f

            if max_temp is not None:
                if "actual_temps" not in self.state:
                    self.state["actual_temps"] = {}
                self.state["actual_temps"][cache_key] = max_temp

            return max_temp
        except Exception as e:
            print("  [REVIEWER] NWS fetch error for %s/%s: %s" % (city_code, target_date, e))
            return None

    def _cache_actual_temps(self, dates):
        """Pre-fetch and permanently cache NWS actual temps for all cities on given dates.

        NWS API only keeps ~7 days of observations, so we must cache while fresh.
        Called nightly + morning retry to ensure all actuals are captured.
        """
        if "actual_temps" not in self.state:
            self.state["actual_temps"] = {}

        fetched = 0
        for date_str in dates:
            for city_code in CITIES:
                cache_key = "%s_%s" % (city_code, date_str)
                if cache_key in self.state["actual_temps"]:
                    continue  # Already cached
                actual = self._get_actual_temp(city_code, date_str)
                if actual is not None:
                    fetched += 1

        if fetched > 0:
            print("  [REVIEW] Cached %d new actual temps" % fetched)
            self._save_state()

    def _compress_daily_record(self, date_str):
        """Compress a day's scan snapshots into compact learning_history.json.

        Called after actuals are cached. Produces one permanent record per day
        with per-city forecast errors, per-model errors, and per-prediction outcomes.
        """
        history_file = getattr(config, 'LEARNING_HISTORY_FILE',
                               os.path.join(config.STATE_DIR, "learning_history.json"))

        # Load existing history
        history = {"daily_records": [], "cumulative": {}}
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r') as f:
                    history = json.load(f)
            except Exception:
                pass

        # Don't re-compress if already done
        existing_dates = {r["date"] for r in history.get("daily_records", [])}
        if date_str in existing_dates:
            return

        day_snaps = self.state.get("scan_snapshots", {}).get(date_str, {})
        if not day_snaps:
            return

        actuals_cache = self.state.get("actual_temps", {})

        # Build city-level aggregates
        cities = {}
        predictions = []
        summary = {"total_predictions": 0, "scored": 0,
                   "correct_skips": 0, "missed_opportunities": 0,
                   "correct_trades": 0, "bad_trades": 0}

        for snap in day_snaps.values():
            city = snap.get("city_code", "")
            tdate = snap.get("target_date", "")
            ticker = snap.get("ticker", "")
            if not city or not tdate:
                continue

            actual = actuals_cache.get("%s_%s" % (city, tdate))
            predicted = snap.get("predicted_high")
            model_means = snap.get("model_means", {})

            # City-level aggregate (deduped by city+date)
            city_key = city
            if city_key not in cities and predicted is not None:
                city_entry = {
                    "predicted_high": round(predicted, 1),
                    "actual_high": actual,
                    "error": round(predicted - actual, 1) if actual is not None and predicted is not None else None,
                    "n_predictions": 0,
                    "n_correct": 0,
                    "models": {}
                }
                for model_name, model_mean in model_means.items():
                    if model_mean is not None:
                        city_entry["models"][model_name] = {
                            "mean": round(model_mean, 1),
                            "error": round(model_mean - actual, 1) if actual is not None else None,
                        }
                cities[city_key] = city_entry

            if city_key in cities:
                cities[city_key]["n_predictions"] += 1

            summary["total_predictions"] += 1

            # Per-prediction outcome
            would_yes_win = self._would_yes_win(ticker, actual) if actual is not None else None
            side = snap.get("side", "")
            signal = snap.get("signal", "skip")

            # Determine outcome for this side
            outcome = None
            if would_yes_win is not None:
                summary["scored"] += 1
                if side == "yes":
                    outcome = 1 if would_yes_win else 0
                elif side == "no":
                    outcome = 1 if not would_yes_win else 0

                # Classify
                if signal == "buy":
                    if outcome == 1:
                        summary["correct_trades"] += 1
                    else:
                        summary["bad_trades"] += 1
                else:
                    if outcome == 1:
                        summary["missed_opportunities"] += 1
                    else:
                        summary["correct_skips"] += 1

                if outcome == 1 and city_key in cities:
                    cities[city_key]["n_correct"] += 1

            # Parse bucket from ticker for compact record
            bucket = self._parse_bucket_from_ticker(ticker)

            predictions.append({
                "city": city,
                "bucket": bucket,
                "side": side,
                "our_prob": round(snap.get("our_prob", 0), 3),
                "market_prob": round(snap.get("market_prob", 0), 3),
                "edge": round(snap.get("edge", 0), 3),
                "signal": signal,
                "verdict": snap.get("confirmation_verdict", ""),
                "outcome": outcome,
                "guard": snap.get("skip_reason") if signal == "skip" else None,
            })

        record = {
            "date": date_str,
            "cities": cities,
            "predictions": predictions,
            "summary": summary,
        }

        history["daily_records"].append(record)

        # Update cumulative stats
        self._update_cumulative_stats(history)

        # Save
        config.atomic_json_save(history_file, history)
        print("  [REVIEW] Compressed %s: %d predictions (%d scored)" % (
            date_str, summary["total_predictions"], summary["scored"]))

    def _parse_bucket_from_ticker(self, ticker):
        """Extract temperature bucket string from ticker name.

        Ticker format: KXHIGHNY-26MAR16-T64 (temp >= 64F)
        or KXHIGHNY-26MAR16-B55-59 (temp between 55-59F)
        Returns bucket string like '55-59' or '>=64' or ticker if unparsable.
        """
        if not ticker:
            return ""
        parts = ticker.upper().split("-")
        for part in parts:
            if part.startswith("T") and part[1:].isdigit():
                return ">=%s" % part[1:]
            if part.startswith("B") and part[1:].replace("-", "").isdigit():
                return part[1:]  # e.g. "55-59"
        # Fallback: return last meaningful part
        return parts[-1] if parts else ""

    def _update_cumulative_stats(self, history):
        """Recompute cumulative stats from all daily records."""
        records = history.get("daily_records", [])
        if not records:
            return

        total_predictions = 0
        total_scored = 0
        city_errors = {}  # city -> [errors]
        model_errors = {}  # model -> [errors]
        prob_outcomes = []  # (our_prob, outcome) for Brier

        for rec in records:
            summary = rec.get("summary", {})
            total_predictions += summary.get("total_predictions", 0)
            total_scored += summary.get("scored", 0)

            # City errors
            for city, cdata in rec.get("cities", {}).items():
                error = cdata.get("error")
                if error is not None:
                    if city not in city_errors:
                        city_errors[city] = []
                    city_errors[city].append(error)

                # Model errors
                for model, mdata in cdata.get("models", {}).items():
                    merr = mdata.get("error")
                    if merr is not None:
                        if model not in model_errors:
                            model_errors[model] = []
                        model_errors[model].append(merr)

            # Prediction outcomes for Brier
            for pred in rec.get("predictions", []):
                if pred.get("outcome") is not None and pred.get("our_prob") is not None:
                    prob_outcomes.append((pred["our_prob"], pred["outcome"]))

        # Compute aggregates
        by_city = {}
        for city, errors in city_errors.items():
            by_city[city] = {
                "mae": round(sum(abs(e) for e in errors) / len(errors), 2),
                "bias": round(sum(errors) / len(errors), 2),
                "n": len(errors),
            }

        by_model = {}
        for model, errors in model_errors.items():
            by_model[model] = {
                "mae": round(sum(abs(e) for e in errors) / len(errors), 2),
                "n": len(errors),
            }

        # Brier score
        brier = None
        if prob_outcomes:
            brier = round(sum((p - o) ** 2 for p, o in prob_outcomes) / len(prob_outcomes), 4)

        history["cumulative"] = {
            "total_days": len(records),
            "total_predictions": total_predictions,
            "total_scored": total_scored,
            "brier_score": brier,
            "by_city_error": by_city,
            "by_model_error": by_model,
        }

    def _retry_missing_actuals(self):
        """Retry fetching actuals for recent dates that had unknowns."""
        recon = self.state.get("scan_reconciliation", [])
        for rec in recon[-5:]:  # Check last 5 reconciliation records
            date_str = rec.get("date", "")
            unknowns = rec.get("unknown", 0)
            if unknowns > 0 and date_str:
                print("  [REVIEW] Retrying actuals for %s (%d unknowns)" % (date_str, unknowns))
                self._cache_actual_temps([date_str])
                # Re-run reconciliation for this date
                self._reconcile_scans(date_str)
                # Re-compress if we got new actuals
                self._compress_daily_record(date_str)

    def _morning_retry(self):
        """Called at 6 AM ET to catch overnight NWS updates.

        West Coast highs happen late (4-5 PM PT = midnight UTC),
        so NWS observations may not be available at 11 PM ET review.
        """
        et_now = datetime.now(ZoneInfo("America/New_York"))
        yesterday = (et_now - timedelta(days=1)).strftime("%Y-%m-%d")
        two_days_ago = (et_now - timedelta(days=2)).strftime("%Y-%m-%d")

        print("  [REVIEW] Morning retry: fetching actuals for %s, %s" % (yesterday, two_days_ago))
        self._cache_actual_temps([yesterday, two_days_ago])
        self._retry_missing_actuals()
        self._save_state()

    def _check_bias_drift(self):
        """Check for cities with large learned biases that may need attention."""
        alerts = []
        for city, data in self.state.get("city_biases", {}).items():
            bias = data.get("bias", 0)
            count = data.get("count", 0)
            if count >= 5 and abs(bias) >= 3.0:
                direction = "hot" if bias > 0 else "cold"
                alerts.append(
                    "%s: models run %.1fF %s (based on %d trades)" % (
                        city, abs(bias), direction, count
                    )
                )
        return alerts

    # ===========================================================
    # STATE PERSISTENCE
    # ===========================================================

    def _load_trade_log(self):
        """Load trade history."""
        try:
            if os.path.exists(config.TRADE_LOG_FILE):
                with open(config.TRADE_LOG_FILE, "r") as f:
                    return json.load(f)
        except Exception:
            pass
        return []

    def _load_state(self):
        try:
            if os.path.exists(config.LEARNING_STATE_FILE):
                with open(config.LEARNING_STATE_FILE, "r") as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        return loaded
        except Exception as e:
            print("  [REVIEW] Warning: corrupt learning state, starting fresh: %s" % e)
        return {}

    def _save_state(self):
        try:
            config.atomic_json_save(config.LEARNING_STATE_FILE, self.state)
        except Exception as e:
            print("  [REVIEW] Error saving state: %s" % e)
