"""
TRADE ANALYZER v1.0 — End-of-Day Post-Mortem
================================================
Analyzes settled trades to understand WHY they won or lost:
  - Entry analysis: edge, confidence, confirmation at time of trade
  - Outcome analysis: actual vs predicted, forecast error, bucket distance
  - Model scorecard: which deterministic models were right/wrong
  - Guard analysis: would today's guards have caught losers?
  - Exit timing: could we have exited earlier based on observations?
  - Sizing review: was Kelly optimal vs actual size?
  - Lessons: template-based natural language takeaways

Called on day change by kalshi_bot.py. Writes to trade_analysis.json.
"""

import json
import os
import math
import requests
from datetime import datetime, timezone, timedelta
from weather_engine import CITIES
import config


# Open-Meteo deterministic forecast endpoints
_DETERMINISTIC_APIS = {
    "gfs": "https://api.open-meteo.com/v1/gfs",
    "ecmwf": "https://api.open-meteo.com/v1/ecmwf",
    "icon": "https://api.open-meteo.com/v1/dwd-icon",
    "gem": "https://api.open-meteo.com/v1/gem",
}

# Ticker prefix → city code
_TICKER_TO_CITY = {
    "KXHIGHNY": "NYC",
    "KXHIGHCHI": "CHI",
    "KXHIGHMIA": "MIA",
    "KXHIGHAUS": "AUS",
}


class TradeAnalyzer:
    """Post-mortem analysis engine for settled trades."""

    def run_daily_analysis(self, trade_log, target_date=None):
        """
        Main entry point. Analyze all trades that settled on target_date.
        Returns the daily analysis dict and saves to JSON.
        """
        if target_date is None:
            target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        # Filter to settled weather trades for the target date
        settled_trades = []
        for t in trade_log:
            if not t.get("settled"):
                continue
            # Match by settlement date (timestamp day) or trade date
            ts = t.get("timestamp", "")
            if ts.startswith(target_date):
                settled_trades.append(t)

        if not settled_trades:
            return None

        # Analyze each trade
        trade_analyses = []
        for trade in settled_trades:
            analysis = self._analyze_single_trade(trade)
            if analysis:
                trade_analyses.append(analysis)

        if not trade_analyses:
            return None

        # Build daily summary
        summary = self._build_daily_summary(trade_analyses, target_date)

        # Save to file
        self._save_analysis(summary)

        return summary

    def _analyze_single_trade(self, trade):
        """Orchestrate all analysis for one trade."""
        ticker = trade.get("ticker", "")
        city_code = trade.get("city_code", "")

        # Only analyze weather trades (have city code and temp data)
        if not city_code or city_code not in CITIES:
            return None

        # Skip non-real results
        result = trade.get("result", "")
        if result not in ("win", "loss", "exit_win", "exit_loss"):
            return None

        # Fetch actual high
        date_str = self._extract_date_from_ticker(ticker)
        actual_high = self._fetch_actual_high(city_code, date_str) if date_str else None

        # Parse bucket from ticker
        bucket = self._parse_bucket_from_ticker(ticker)

        # Run each analysis component
        entry = self._analyze_entry(trade)
        outcome = self._analyze_outcome(trade, actual_high, bucket)
        models = self._analyze_models(city_code, date_str, actual_high)
        guards = self._analyze_guards(trade, models, bucket)
        exit_timing = self._analyze_exit_timing(trade, actual_high, bucket)
        sizing = self._analyze_sizing(trade)
        lessons = self._generate_lessons(trade, entry, outcome, models, guards, exit_timing, sizing)

        return {
            "ticker": ticker,
            "date": date_str,
            "result": result,
            "profit_cents": trade.get("profit_cents", 0),
            "side": trade.get("side", ""),
            "price_cents": trade.get("price_cents", 0),
            "contracts": trade.get("contracts", 0),
            "city_code": city_code,
            "entry_analysis": entry,
            "outcome_analysis": outcome,
            "model_analysis": models,
            "guard_analysis": guards,
            "exit_analysis": exit_timing,
            "sizing_analysis": sizing,
            "lessons": lessons,
        }

    # ═══════════════════════════════════════════════════════
    # ANALYSIS COMPONENTS
    # ═══════════════════════════════════════════════════════

    def _analyze_entry(self, trade):
        """Extract entry data from the trade record."""
        return {
            "edge": trade.get("edge", 0),
            "confidence": trade.get("confidence", 0),
            "confirmation": trade.get("confirmation", ""),
            "predicted_high": trade.get("predicted_high"),
            "reasoning": trade.get("reasoning", ""),
            "time_of_entry": trade.get("timestamp", ""),
            "spread_at_entry": trade.get("spread_at_entry_cents", 0),
            "strategy": trade.get("strategy", ""),
        }

    def _analyze_outcome(self, trade, actual_high, bucket):
        """Compare prediction vs actual outcome."""
        predicted = trade.get("predicted_high")
        result = {
            "actual_high": actual_high,
            "forecast_error": None,
            "bucket_range": None,
            "distance_from_bucket": None,
            "actual_in_bucket": None,
        }

        if predicted is not None and actual_high is not None:
            result["forecast_error"] = round(actual_high - predicted, 1)

        if bucket:
            result["bucket_range"] = f"{bucket['low']}-{bucket['high']}F"

            if actual_high is not None:
                if bucket["low"] <= actual_high <= bucket["high"]:
                    result["actual_in_bucket"] = True
                    result["distance_from_bucket"] = 0
                elif actual_high < bucket["low"]:
                    result["actual_in_bucket"] = False
                    result["distance_from_bucket"] = round(bucket["low"] - actual_high, 1)
                else:
                    result["actual_in_bucket"] = False
                    result["distance_from_bucket"] = round(actual_high - bucket["high"], 1)

        return result

    def _analyze_models(self, city_code, date_str, actual_high):
        """Fetch deterministic model predictions and score each model's error."""
        result = {
            "model_forecasts": {},
            "model_errors": {},
            "best_model": None,
            "worst_model": None,
        }

        if not city_code or city_code not in CITIES or not date_str:
            return result

        city = CITIES[city_code]

        for model_name, api_url in _DETERMINISTIC_APIS.items():
            try:
                params = {
                    "latitude": city["lat"],
                    "longitude": city["lon"],
                    "daily": "temperature_2m_max",
                    "temperature_unit": "fahrenheit",
                    "timezone": city.get("timezone", "auto"),
                    "start_date": date_str,
                    "end_date": date_str,
                }
                resp = requests.get(api_url, params=params, timeout=10)
                if resp.status_code == 200:
                    temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
                    if temps and temps[0] is not None:
                        forecast = round(temps[0], 1)
                        result["model_forecasts"][model_name] = forecast

                        if actual_high is not None:
                            error = round(forecast - actual_high, 1)
                            result["model_errors"][model_name] = error
            except Exception:
                continue

        # Determine best/worst models
        if result["model_errors"]:
            by_abs_error = sorted(result["model_errors"].items(), key=lambda x: abs(x[1]))
            result["best_model"] = by_abs_error[0][0]
            result["worst_model"] = by_abs_error[-1][0]

        return result

    def _analyze_guards(self, trade, models, bucket):
        """Check if today's 4 strategy guards would have caught this loser."""
        result = {
            "would_ban_1f": False,
            "would_model_spread_reject": False,
            "would_spread_ratio_reject": False,
            "would_narrow_edge_reject": False,
            "guards_would_catch": False,
        }

        side = trade.get("side", "")
        if side != "yes":
            return result  # Guards are primarily for YES trades

        if not bucket:
            return result

        bucket_width = bucket["high"] - bucket["low"]

        # Guard 1: Ban 1F buckets
        if bucket_width <= 1:
            result["would_ban_1f"] = True

        # Guard 2: Model spread > 5F
        forecasts = models.get("model_forecasts", {})
        if len(forecasts) >= 2:
            vals = list(forecasts.values())
            model_spread = max(vals) - min(vals)
            if model_spread > 5:
                result["would_model_spread_reject"] = True

        # Guard 3: Spread-to-bucket ratio > 6x
        # We don't have ensemble spread post-hoc, but we can use model spread as proxy
        if len(forecasts) >= 2 and bucket_width > 0:
            vals = list(forecasts.values())
            proxy_spread = max(vals) - min(vals)
            if proxy_spread / bucket_width > 6:
                result["would_spread_ratio_reject"] = True

        # Guard 4: Narrow edge on <=2F bucket
        edge = trade.get("edge", 0)
        if bucket_width <= 2:
            if edge < 0.35:
                result["would_narrow_edge_reject"] = True

        result["guards_would_catch"] = any([
            result["would_ban_1f"],
            result["would_model_spread_reject"],
            result["would_spread_ratio_reject"],
            result["would_narrow_edge_reject"],
        ])

        return result

    def _analyze_exit_timing(self, trade, actual_high, bucket):
        """Heuristic: could we have exited earlier based on how far actual was from bucket?"""
        result = {
            "could_have_exited_earlier": False,
            "explanation": "",
        }

        if not bucket or actual_high is None:
            result["explanation"] = "Insufficient data for exit timing analysis"
            return result

        side = trade.get("side", "")
        trade_result = trade.get("result", "")

        if trade_result in ("loss", "exit_loss"):
            if side == "yes":
                if actual_high > bucket["high"]:
                    gap = actual_high - bucket["high"]
                    if gap >= 3:
                        result["could_have_exited_earlier"] = True
                        result["explanation"] = (
                            f"Actual high {actual_high}F exceeded bucket ceiling "
                            f"{bucket['high']}F by {gap:.1f}F. Afternoon observations "
                            f"would have shown temp rising past the bucket."
                        )
                    else:
                        result["explanation"] = (
                            f"Actual high {actual_high}F barely exceeded bucket {bucket['high']}F "
                            f"by {gap:.1f}F — hard to exit in time."
                        )
                elif actual_high < bucket["low"]:
                    gap = bucket["low"] - actual_high
                    if gap >= 5:
                        result["could_have_exited_earlier"] = True
                        result["explanation"] = (
                            f"Actual high {actual_high}F was {gap:.1f}F below bucket "
                            f"{bucket['low']}F. By mid-afternoon the temp trend would "
                            f"have been clearly insufficient."
                        )
                    else:
                        result["explanation"] = (
                            f"Actual high {actual_high}F was only {gap:.1f}F below "
                            f"bucket {bucket['low']}F — close call."
                        )
            elif side == "no":
                if bucket["low"] <= actual_high <= bucket["high"]:
                    result["could_have_exited_earlier"] = True
                    result["explanation"] = (
                        f"Actual high {actual_high}F landed in the bucket "
                        f"{bucket['low']}-{bucket['high']}F. Observations would have "
                        f"shown temp approaching the bucket range."
                    )
        else:
            result["explanation"] = "Winner — no early exit needed"

        return result

    def _analyze_sizing(self, trade):
        """Review position sizing: was it optimal?"""
        edge = trade.get("edge", 0)
        price_cents = trade.get("price_cents", 0)
        contracts = trade.get("contracts", 0)
        cost_cents = trade.get("cost_cents", 0)
        trade_result = trade.get("result", "")

        result = {
            "actual_size_cents": cost_cents,
            "actual_contracts": contracts,
            "kelly_optimal_contracts": None,
            "additional_profit_if_optimal": None,
        }

        if edge <= 0 or price_cents <= 0:
            return result

        # Reconstruct Kelly optimal sizing
        prob_win = min(0.75, edge + (price_cents / 100.0))
        payout = 100 - price_cents
        loss = price_cents

        if payout <= 0 or loss <= 0:
            return result

        kelly = ((prob_win * payout) - ((1 - prob_win) * loss)) / payout
        quarter_kelly = kelly / 4.0

        if quarter_kelly <= 0:
            return result

        bankroll = config.MAX_TOTAL_EXPOSURE_CENTS
        optimal_bet = quarter_kelly * bankroll
        max_per_pos = int(bankroll * config.MAX_POSITION_PCT)
        optimal_bet = min(optimal_bet, max_per_pos, config.MAX_AUTO_TRADE_CENTS)
        optimal_contracts = max(1, int(optimal_bet / price_cents))

        result["kelly_optimal_contracts"] = optimal_contracts

        if trade_result in ("win", "exit_win") and optimal_contracts > contracts:
            additional_payout = (optimal_contracts - contracts) * (100 - price_cents)
            result["additional_profit_if_optimal"] = additional_payout

        return result

    def _generate_lessons(self, trade, entry, outcome, models, guards, exit_timing, sizing):
        """Generate template-based natural language lessons."""
        lessons = []
        trade_result = trade.get("result", "")
        side = trade.get("side", "")
        city = trade.get("city_code", "")
        is_loss = trade_result in ("loss", "exit_loss")
        is_win = trade_result in ("win", "exit_win")

        # Lesson 1: Forecast accuracy
        if outcome.get("forecast_error") is not None:
            err = outcome["forecast_error"]
            if abs(err) <= 2:
                lessons.append(f"Forecast was accurate: {err:+.1f}F error.")
            elif err > 0:
                lessons.append(
                    f"Forecast was cold-biased by {err:.1f}F — actual was warmer "
                    f"than predicted. Consider warm bias adjustment for {city}."
                )
            else:
                lessons.append(
                    f"Forecast was warm-biased by {abs(err):.1f}F — actual was cooler "
                    f"than predicted. Consider cool bias adjustment for {city}."
                )

        # Lesson 2: Model disagreement
        model_errors = models.get("model_errors", {})
        if len(model_errors) >= 2:
            abs_errors = {k: abs(v) for k, v in model_errors.items()}
            best = min(abs_errors, key=abs_errors.get)
            worst = max(abs_errors, key=abs_errors.get)
            if abs_errors[worst] - abs_errors[best] > 3:
                lessons.append(
                    f"Model disagreement: {best} was closest (error {model_errors[best]:+.1f}F), "
                    f"{worst} was worst (error {model_errors[worst]:+.1f}F). "
                    f"Spread of {abs_errors[worst] - abs_errors[best]:.1f}F should have "
                    f"flagged uncertainty."
                )

        # Lesson 3: Guard effectiveness
        if is_loss and guards.get("guards_would_catch"):
            caught_by = []
            if guards["would_ban_1f"]:
                caught_by.append("1F-bucket ban")
            if guards["would_model_spread_reject"]:
                caught_by.append("model-spread >5F")
            if guards["would_spread_ratio_reject"]:
                caught_by.append("spread/bucket >6x")
            if guards["would_narrow_edge_reject"]:
                caught_by.append("narrow-edge <35%")
            lessons.append(
                f"Guards would have prevented this loss: {', '.join(caught_by)}. "
                f"Saved: ${abs(trade.get('profit_cents', 0))/100:.2f}."
            )
        elif is_loss and not guards.get("guards_would_catch"):
            lessons.append(
                "No existing guard would have caught this loss. "
                "Consider whether a new heuristic is needed."
            )

        # Lesson 4: Exit timing
        if is_loss and exit_timing.get("could_have_exited_earlier"):
            lessons.append(f"Early exit possible: {exit_timing['explanation']}")

        # Lesson 5: Sizing for winners
        if is_win and sizing.get("additional_profit_if_optimal"):
            extra = sizing["additional_profit_if_optimal"]
            lessons.append(
                f"Undersized: Kelly optimal was {sizing['kelly_optimal_contracts']} contracts "
                f"vs actual {sizing['actual_contracts']}. "
                f"Additional profit if optimal: ${extra/100:.2f}."
            )

        return lessons

    # ═══════════════════════════════════════════════════════
    # SUMMARY BUILDER
    # ═══════════════════════════════════════════════════════

    def _build_daily_summary(self, trade_analyses, target_date):
        """Build aggregate summary from per-trade analyses."""
        wins = [t for t in trade_analyses if t["result"] in ("win", "exit_win")]
        losses = [t for t in trade_analyses if t["result"] in ("loss", "exit_loss")]
        total_pnl = sum(t["profit_cents"] for t in trade_analyses)

        # Average edge for winners vs losers
        winner_edges = [t["entry_analysis"]["edge"] for t in wins if t["entry_analysis"]["edge"]]
        loser_edges = [t["entry_analysis"]["edge"] for t in losses if t["entry_analysis"]["edge"]]

        # Count guards effectiveness
        guards_caught = sum(
            1 for t in losses if t["guard_analysis"].get("guards_would_catch")
        )
        savings = sum(
            abs(t["profit_cents"]) for t in losses
            if t["guard_analysis"].get("guards_would_catch")
        )

        # Best/worst model across all trades
        all_model_errors = {}
        for t in trade_analyses:
            for model, err in t["model_analysis"].get("model_errors", {}).items():
                if model not in all_model_errors:
                    all_model_errors[model] = []
                all_model_errors[model].append(abs(err))

        best_model = None
        worst_model = None
        if all_model_errors:
            avg_errors = {m: sum(e) / len(e) for m, e in all_model_errors.items()}
            best_model = min(avg_errors, key=avg_errors.get)
            worst_model = max(avg_errors, key=avg_errors.get)

        # City performance
        city_perf = {}
        for t in trade_analyses:
            city = t["city_code"]
            if city not in city_perf:
                city_perf[city] = {"trades": 0, "wins": 0, "pnl_cents": 0}
            city_perf[city]["trades"] += 1
            if t["result"] in ("win", "exit_win"):
                city_perf[city]["wins"] += 1
            city_perf[city]["pnl_cents"] += t["profit_cents"]

        return {
            "date": target_date,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_trades": len(trade_analyses),
                "wins": len(wins),
                "losses": len(losses),
                "total_pnl_cents": total_pnl,
                "win_rate": len(wins) / len(trade_analyses) if trade_analyses else 0,
                "avg_edge_winners": round(sum(winner_edges) / len(winner_edges), 4) if winner_edges else 0,
                "avg_edge_losers": round(sum(loser_edges) / len(loser_edges), 4) if loser_edges else 0,
            },
            "patterns": {
                "guards_would_have_caught": guards_caught,
                "savings_cents": savings,
                "best_model": best_model,
                "worst_model": worst_model,
                "city_performance": city_perf,
            },
            "trade_analyses": trade_analyses,
        }

    # ═══════════════════════════════════════════════════════
    # DATA FETCHING HELPERS
    # ═══════════════════════════════════════════════════════

    def _fetch_actual_high(self, city_code, date_str):
        """Fetch actual daily high from Open-Meteo archive API."""
        if not city_code or city_code not in CITIES or not date_str:
            return None

        city = CITIES[city_code]
        try:
            url = "https://archive-api.open-meteo.com/v1/archive"
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city.get("timezone", "auto"),
                "start_date": date_str,
                "end_date": date_str,
            }
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 200:
                temps = resp.json().get("daily", {}).get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    return round(temps[0], 1)
        except Exception:
            pass
        return None

    def _extract_date_from_ticker(self, ticker):
        """Extract YYYY-MM-DD from ticker like KXHIGHNY-26FEB12-B36.5."""
        try:
            parts = ticker.split("-")
            if len(parts) >= 2:
                date_part = parts[1]
                if len(date_part) >= 7:
                    year = int("20" + date_part[:2])
                    month_str = date_part[2:5].upper()
                    day = int(date_part[5:7])
                    months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
                              "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
                              "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
                    month = months.get(month_str)
                    if month:
                        return f"{year}-{month:02d}-{day:02d}"
        except Exception:
            pass
        return None

    def _parse_bucket_from_ticker(self, ticker):
        """Parse temperature bucket from ticker like KXHIGHNY-26FEB12-B36.5.

        Bucket formats:
          B36.5  → 36.5 to 37.5  (above threshold)
          T36.5  → 35.5 to 36.5  (below threshold)
        Kalshi weather markets use ~1-2F bucket widths.
        """
        try:
            parts = ticker.split("-")
            if len(parts) >= 3:
                bucket_str = parts[2]
                if bucket_str.startswith("B"):
                    threshold = float(bucket_str[1:])
                    return {"low": threshold, "high": threshold + 1.0}
                elif bucket_str.startswith("T"):
                    threshold = float(bucket_str[1:])
                    return {"low": threshold - 1.0, "high": threshold}
        except Exception:
            pass
        return None

    # ═══════════════════════════════════════════════════════
    # PERSISTENCE
    # ═══════════════════════════════════════════════════════

    def _save_analysis(self, analysis):
        """Save analysis to trade_analysis.json, appending to existing data."""
        filepath = config.TRADE_ANALYSIS_FILE
        existing = []
        try:
            if os.path.exists(filepath):
                with open(filepath) as f:
                    existing = json.load(f)
                    if not isinstance(existing, list):
                        existing = [existing]
        except Exception:
            existing = []

        # Don't duplicate analyses for the same date
        existing = [a for a in existing if a.get("date") != analysis["date"]]
        existing.append(analysis)

        # Keep last 30 days
        existing = existing[-30:]

        try:
            with open(filepath, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception:
            pass
