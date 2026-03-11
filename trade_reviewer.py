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
import os
from datetime import datetime, timezone
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
        }
        for k, v in defaults.items():
            if k not in self.state:
                self.state[k] = v

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
        self._reconcile_scans(today)
        self._analyze_guard_effectiveness()
        self._analyze_calibration()
        self._generate_daily_report(trade_log, today)

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

    def capture_scan_snapshot(self, all_signals):
        """Called once per cycle with ALL evaluated signals (buy + skip).

        Stores deduplicated snapshots per ticker per day. Keeps 7 days.
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
                "skip_reason": sig.get("skip_reason"),
                "confirmation_verdict": sig.get("confirmation_verdict", ""),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

        self.state["scan_snapshots"][today] = day_snaps

        # Prune to 7 days
        all_dates = sorted(self.state["scan_snapshots"].keys())
        while len(all_dates) > 7:
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
        # Match snapshots to settled trades
        settled = {}
        for t in trade_log:
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

            # Only compute bias with 5+ data points
            entry["count"] = len(entry["errors"])
            if entry["count"] >= 5:
                # EMA over errors
                ema = entry["errors"][0]
                for e in entry["errors"][1:]:
                    ema = alpha * e + (1 - alpha) * ema
                # Cap at +/- 8F
                entry["bias"] = round(max(-8.0, min(8.0, ema)), 2)

        self.state["city_biases"] = biases

    # ===========================================================
    # LEARNING: MODEL ACCURACY
    # ===========================================================

    def _learn_model_accuracy(self, trade_log):
        """Learn per-model accuracy weights from forecast snapshots.

        Tracks MAE per model per city. Converts to weights:
        weight = 1.0 / (mae + 1.0), normalized to sum to 1.0.
        """
        settled = {}
        for t in trade_log:
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

            for model_name, model_mean in model_means.items():
                if model_mean is None:
                    continue
                abs_error = abs(model_mean - actual)

                if model_name not in accuracy[city]:
                    accuracy[city][model_name] = {"errors": [], "mae": None, "weight": None}

                entry = accuracy[city][model_name]
                entry["errors"].append(round(abs_error, 2))
                if len(entry["errors"]) > 30:
                    entry["errors"] = entry["errors"][-30:]

                if len(entry["errors"]) >= 5:
                    mae = sum(entry["errors"]) / len(entry["errors"])
                    entry["mae"] = round(mae, 2)

            # Normalize weights for this city
            models_with_mae = {m: d for m, d in accuracy[city].items()
                              if d.get("mae") is not None}
            if models_with_mae:
                raw_weights = {m: 1.0 / (d["mae"] + 1.0)
                              for m, d in models_with_mae.items()}
                total_weight = sum(raw_weights.values())
                for m, w in raw_weights.items():
                    accuracy[city][m]["weight"] = round(w / total_weight, 3)

        self.state["model_accuracy"] = accuracy

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
        print("  ╔══════════════════════════════════════╗")
        print("  ║       SCAN RECONCILIATION: %s   ║" % today)
        print("  ╠══════════════════════════════════════╣")
        print("  ║  Evaluated: %-3d markets w/ forecasts ║" % results["total_evaluated"])
        print("  ║  Correct skips: %-3d                  ║" % results["correct_skips"])
        print("  ║  Missed opportunities: %-3d           ║" % results["missed_opportunities"])
        print("  ║  Missed profit: $%.2f               ║" % (results["missed_profit_potential_cents"] / 100.0))
        print("  ║  Correct trades: %-3d                 ║" % results["correct_trades"])
        print("  ║  Bad trades: %-3d                     ║" % results["bad_trades"])
        print("  ╚══════════════════════════════════════╝")

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
                print("  [REVIEW] ⚠ GUARD WARNING: %s accuracy=%.0f%% "
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

        self.state["calibration"] = {
            "buckets": buckets,
            "brier_score": brier_score,
            "total_predictions": brier_count,
        }

        if brier_score is not None:
            quality = "excellent" if brier_score < 0.15 else \
                      "good" if brier_score < 0.20 else \
                      "fair" if brier_score < 0.25 else "poor"
            print("  [REVIEW] Calibration: Brier=%.4f (%s), %d predictions" % (
                brier_score, quality, brier_count))

    # ===========================================================
    # DAILY REPORT
    # ===========================================================

    def _generate_daily_report(self, trade_log, today):
        """Generate daily summary report. Prints to console + stores in state."""
        # Filter today's settled trades
        todays_trades = []
        for t in trade_log:
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
        print("  ╔══════════════════════════════════════╗")
        print("  ║       DAILY TRADE REVIEW: %s      ║" % today)
        print("  ╠══════════════════════════════════════╣")
        print("  ║  Record: %dW / %dL                    ║" % (wins, losses))
        print("  ║  P&L: %+dc ($%+.2f)                ║" % (total_pnl, total_pnl / 100.0))
        print("  ║  Avg Edge: Win=%.1f%% Loss=%.1f%%     ║" % (avg_win_edge * 100, avg_loss_edge * 100))
        print("  ╚══════════════════════════════════════╝")

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
            if (datetime.now() - target).days > 7:
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
            url = "https://api.weather.gov/stations/%s/observations" % station
            params = {
                "start": "%sT00:00:00Z" % target_date,
                "end": "%sT23:59:59Z" % target_date,
            }
            headers = {"User-Agent": "KalshiBot/4.0", "Accept": "application/geo+json"}
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                return None

            features = resp.json().get("features", [])
            max_temp = None
            for obs in features:
                temp_c = obs.get("properties", {}).get("temperature", {}).get("value")
                if temp_c is not None:
                    temp_f = round(temp_c * 9 / 5 + 32)
                    if max_temp is None or temp_f > max_temp:
                        max_temp = temp_f

            if max_temp is not None:
                if "actual_temps" not in self.state:
                    self.state["actual_temps"] = {}
                self.state["actual_temps"][cache_key] = max_temp

            return max_temp
        except Exception:
            return None

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
