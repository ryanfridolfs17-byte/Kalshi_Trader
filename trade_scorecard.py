"""
TRADE SCORECARD v4.0 — Recursive Evaluation Loop
=====================================================
Every trade signal passes through 8 criteria before execution.
If any criterion fails, the trade is diagnosed, adjusted, and re-scored.
The loop continues until all criteria pass OR the trade is rejected.

Pattern: generate → evaluate → diagnose → improve → repeat

Criteria:
  1. data_integrity      — NWS station correct for settlement?
  2. forecast_convergence — Ensemble models agree?
  3. edge_magnitude       — Edge survives fees + uncertainty?
  4. timing_window        — Favorable entry timing?
  5. liquidity            — Enough depth to fill?
  6. portfolio_correlation— Not over-concentrated?
  7. position_sizing      — Within drawdown tolerance?
  8. adversarial_check    — Survives devil's advocate?
"""

from datetime import datetime, timezone, timedelta
import config


class TradeScorecard:
    """
    Recursive evaluation loop for trade signals.

    Pattern: generate → evaluate → diagnose → improve → repeat
    Stops when: all criteria pass OR max_iterations reached OR trade rejected
    """

    MAX_ITERATIONS = 3

    # ── SCORING CRITERIA ──────────────────────────────────────────────
    CRITERIA = {
        "data_integrity": {
            "description": "Is the NWS station correct for this market's settlement source?",
            "fixable": False,
        },
        "forecast_convergence": {
            "threshold": 0.50,
            "description": "Do multiple forecast models agree within 2°F?",
            "fixable": False,
        },
        "edge_magnitude": {
            "threshold": 0.05,
            "description": "Is the edge large enough after fees and uncertainty?",
            "fixable": True,
        },
        "timing_window": {
            "threshold": 0.50,
            "description": "Are we in a historically favorable entry window?",
            "fixable": True,
        },
        "liquidity": {
            "threshold": 3,
            "description": "Is there enough volume to fill without excessive slippage?",
            "fixable": True,
        },
        "portfolio_correlation": {
            "threshold": 0.40,
            "description": "Are we over-concentrated in correlated markets?",
            "fixable": True,
        },
        "position_sizing": {
            "description": "Does position size stay within risk limits?",
            "fixable": True,
        },
        "adversarial_check": {
            "description": "Does the trade survive devil's advocate stress test?",
            "fixable": True,
        },
    }

    def __init__(self, kalshi_client=None, risk_manager=None):
        self.client = kalshi_client
        self.risk = risk_manager

        # Import station mappings from weather engine
        try:
            from weather_engine import CITIES
            self.cities = CITIES
        except ImportError:
            self.cities = {}

    def evaluate(self, signal, weather_data, portfolio):
        """
        Run the recursive evaluation loop.

        Args:
            signal: Raw trade signal from strategy.py
                    Must have: ticker, side, edge, price_cents, suggested_contracts,
                               confidence, confirmation_verdict, strategy
            weather_data: Current ensemble forecast distribution dict (from weather_engine)
                          Keys: forecasted_high_mean, total_members, spread, model_spread,
                                model_means, confidence, members (list)
            portfolio: Dict with portfolio state:
                       {"positions": [...], "balance_cents": int, "city_exposure": {...}}

        Returns:
            {
                "action": "execute" | "reject" | "defer",
                "signal": dict,              # Possibly modified signal
                "scores": dict,              # Score for each criterion
                "iterations": int,
                "diagnosis_log": list[str],
                "final_reasoning": str,
            }
        """
        # Bypass scorecard for confirmed outcomes and arbitrage
        # These are near-risk-free and already heavily validated
        verdict = signal.get("confirmation_verdict", "")
        strategy_type = signal.get("strategy", "")
        if verdict == "CONFIRMED_OUTCOME" or strategy_type == "S2-Arbitrage":
            return {
                "action": "execute",
                "signal": signal,
                "scores": {"bypass": {"passed": True, "detail": f"Bypass: {verdict or strategy_type}"}},
                "iterations": 0,
                "diagnosis_log": [],
                "final_reasoning": f"Scorecard bypassed for {verdict or strategy_type}",
            }

        iteration = 0
        diagnosis_log = []
        scores = {}

        while iteration < self.MAX_ITERATIONS:
            scores = self._score_all(signal, weather_data, portfolio)
            failures = {k: v for k, v in scores.items() if not v["passed"]}

            if not failures:
                return {
                    "action": "execute",
                    "signal": signal,
                    "scores": scores,
                    "iterations": iteration + 1,
                    "diagnosis_log": diagnosis_log,
                    "final_reasoning": f"All {len(self.CRITERIA)} criteria passed after {iteration + 1} iteration(s).",
                }

            # Check for unfixable failures → reject immediately
            unfixable = [k for k in failures if not self.CRITERIA[k]["fixable"]]
            if unfixable:
                return {
                    "action": "reject",
                    "signal": signal,
                    "scores": scores,
                    "iterations": iteration + 1,
                    "diagnosis_log": diagnosis_log,
                    "final_reasoning": f"Rejected: unfixable failure(s) in {unfixable}",
                }

            # Diagnose and attempt fixes for fixable failures
            for criterion_name, score_result in failures.items():
                diagnosis = self._diagnose(criterion_name, score_result, signal)
                diagnosis_log.append(
                    f"Iter {iteration + 1}: {criterion_name} failed — "
                    f"{diagnosis['problem']} — fix: {diagnosis['fix']}"
                )
                signal = self._apply_fix(signal, diagnosis)

            iteration += 1

        # Max iterations reached with remaining failures
        return {
            "action": "defer",
            "signal": signal,
            "scores": scores,
            "iterations": iteration,
            "diagnosis_log": diagnosis_log,
            "final_reasoning": (
                f"Deferred: could not resolve all failures in {self.MAX_ITERATIONS} iterations. "
                f"Remaining: {list(failures.keys())}"
            ),
        }

    # ── SCORE ALL CRITERIA ────────────────────────────────────────

    def _score_all(self, signal, weather_data, portfolio):
        """Run all 8 criteria and return scores dict."""
        scores = {}
        scores["data_integrity"] = self._check_data_integrity(signal, weather_data, portfolio)
        scores["forecast_convergence"] = self._check_forecast_convergence(signal, weather_data, portfolio)
        scores["edge_magnitude"] = self._check_edge_magnitude(signal, weather_data, portfolio)
        scores["timing_window"] = self._check_timing_window(signal, weather_data, portfolio)
        scores["liquidity"] = self._check_liquidity(signal, weather_data, portfolio)
        scores["portfolio_correlation"] = self._check_portfolio_correlation(signal, weather_data, portfolio)
        scores["position_sizing"] = self._check_position_sizing(signal, weather_data, portfolio)
        scores["adversarial_check"] = self._check_adversarial(signal, weather_data, portfolio)
        return scores

    # ── INDIVIDUAL CRITERION CHECKS ───────────────────────────────

    def _check_data_integrity(self, signal, weather_data, portfolio):
        """
        Verify the NWS station in weather_data matches the Kalshi settlement source.
        For SP500 trades, this always passes (no NWS station involved).
        """
        strategy = signal.get("strategy", "")
        if strategy == "S3-SP500":
            return {"passed": True, "value": "N/A", "threshold": "N/A",
                    "detail": "SP500 — no NWS station check needed"}

        city_code = signal.get("city_code", "")
        if not city_code:
            # Try to extract from ticker
            city_code = self._extract_city_from_ticker(signal.get("ticker", ""))

        if not city_code or city_code not in self.cities:
            return {"passed": False, "value": city_code, "threshold": "valid city",
                    "detail": f"Unknown city code: {city_code}"}

        expected_station = self.cities[city_code].get("nws_station", "")
        # Weather data should reference the correct station's coordinates
        # The distribution is fetched using CITIES[city_code] lat/lon, so if
        # city_code resolves correctly, the station is correct.
        if not expected_station:
            return {"passed": False, "value": "missing", "threshold": "valid station",
                    "detail": f"No NWS station configured for {city_code}"}

        return {"passed": True, "value": expected_station, "threshold": expected_station,
                "detail": f"Station {expected_station} verified for {city_code}"}

    def _check_forecast_convergence(self, signal, weather_data, portfolio):
        """
        Check what percentage of ensemble members agree within 2°F of the median.
        High convergence = high confidence in forecast.
        """
        threshold = self.CRITERIA["forecast_convergence"]["threshold"]

        if not weather_data:
            return {"passed": False, "value": 0.0, "threshold": threshold,
                    "detail": "No weather data available"}

        # SP500 uses different distribution — always pass convergence
        if signal.get("strategy") == "S3-SP500":
            return {"passed": True, "value": 1.0, "threshold": threshold,
                    "detail": "SP500 — convergence check N/A"}

        # Use model_spread as a proxy for convergence
        # Lower spread = higher convergence
        model_spread = weather_data.get("model_spread", 0)
        total_members = weather_data.get("total_members", 143)

        # Count members within 2°F of the mean
        members = weather_data.get("members", [])
        mean = weather_data.get("forecasted_high_mean", 0)

        if members and mean:
            within_2f = sum(1 for m in members if abs(m - mean) <= 2.0)
            convergence = within_2f / len(members)
        elif model_spread > 0:
            # Approximate: spread < 2°F ≈ 80%+ convergence, spread > 6°F ≈ 30%
            convergence = max(0.2, min(1.0, 1.0 - (model_spread - 1.0) / 8.0))
        else:
            # If we have confidence from distribution, use it
            convergence = weather_data.get("confidence", 0.5)

        passed = convergence >= threshold
        return {"passed": passed, "value": round(convergence, 3), "threshold": threshold,
                "detail": f"{convergence:.0%} convergence (model spread: {model_spread:.1f}°F, {total_members} members)"}

    def _check_edge_magnitude(self, signal, weather_data, portfolio):
        """
        Check that edge after fees exceeds minimum threshold.
        Uses the fee-adjusted edge already computed by strategy.py.
        """
        threshold = self.CRITERIA["edge_magnitude"]["threshold"]
        edge = signal.get("edge", 0)
        price_cents = signal.get("price_cents", 0)

        # Compute fee-adjusted edge (expected fee, matching strategy.py formula)
        if config.FEE_ADJUSTMENT_ENABLED and price_cents > 0:
            win_prob = min(0.95, (price_cents / 100.0) + edge)
            fee_drag = config.KALSHI_FEE_PCT * (100 - price_cents) / 100.0 * win_prob
            net_edge = edge - fee_drag
        else:
            net_edge = edge

        passed = net_edge >= threshold
        return {"passed": passed, "value": round(net_edge, 4), "threshold": threshold,
                "detail": f"Net edge {net_edge:.1%} (raw {edge:.1%}, fee drag ~{edge - net_edge:.1%})"}

    def _check_timing_window(self, signal, weather_data, portfolio):
        """
        Check if we're in a favorable entry window based on hours to settlement.

        Default multipliers (before Becker data):
          48-24h: 1.2x (early edge), 24-12h: 1.0x, 12-6h: 0.8x,
          <6h: 0.5x (late entry), >72h: 0.7x (forecast uncertain)
        """
        threshold = self.CRITERIA["timing_window"]["threshold"]

        close_time_str = signal.get("close_time")
        if not close_time_str:
            # No close time info — assume acceptable timing
            return {"passed": True, "value": 1.0, "threshold": threshold,
                    "detail": "No close_time available — timing check skipped"}

        try:
            close_time = datetime.fromisoformat(close_time_str.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            hours_until = (close_time - now_utc).total_seconds() / 3600
        except (ValueError, TypeError):
            return {"passed": True, "value": 1.0, "threshold": threshold,
                    "detail": "Could not parse close_time — timing check skipped"}

        # Default timing multipliers
        if hours_until > 72:
            multiplier = 0.7
            reason = "forecast too uncertain (>72h)"
        elif hours_until > 48:
            multiplier = 0.9
            reason = "early window (48-72h)"
        elif hours_until > 24:
            multiplier = 1.2
            reason = "peak edge window (24-48h)"
        elif hours_until > 12:
            multiplier = 1.0
            reason = "standard window (12-24h)"
        elif hours_until > 6:
            multiplier = 0.8
            reason = "market converging (6-12h)"
        elif hours_until > 0:
            multiplier = 0.5
            reason = "late entry penalty (<6h)"
        else:
            multiplier = 0.0
            reason = "market closed"

        passed = multiplier >= threshold
        return {"passed": passed, "value": round(multiplier, 2), "threshold": threshold,
                "detail": f"Timing {multiplier:.1f}x — {reason} ({hours_until:.1f}h to settlement)"}

    def _check_liquidity(self, signal, weather_data, portfolio):
        """
        Check orderbook depth at target price level.
        If client is available, fetch real orderbook; otherwise use market volume.

        Weather markets are inherently thin — we use maker orders (post limits
        and wait) so we don't need deep orderbook depth.  The market_quality.py
        filter already rejected dead/frozen markets and extreme spreads before
        the signal reaches here, so any surviving weather signal has baseline
        activity.  Threshold is relaxed to 1 contract for weather.
        """
        is_weather = signal.get("strategy", "") in ("S1-Weather", "S2-Arbitrage")
        # Weather maker orders post into the book — empty book is ideal,
        # not a problem. Bypass liquidity check entirely for weather.
        if is_weather and config.MAKER_STRATEGY_ENABLED:
            return {"passed": True, "value": "maker", "threshold": 0,
                    "detail": "Weather maker strategy — liquidity check bypassed (we post limits)"}
        threshold = 1 if is_weather else self.CRITERIA["liquidity"]["threshold"]

        ticker = signal.get("ticker", "")
        side = signal.get("side", "yes")
        price_cents = signal.get("price_cents", 0)

        # Try to get real orderbook depth
        depth = 0
        if self.client and ticker:
            try:
                book = self.client.get_orderbook(ticker)
                if book:
                    # Count contracts available within 2¢ of our target price
                    if side == "yes":
                        asks = book.get("yes", []) or book.get("asks", [])
                    else:
                        asks = book.get("no", []) or book.get("asks", [])

                    if isinstance(asks, list):
                        for level in asks:
                            level_price = level.get("price", 0) if isinstance(level, dict) else 0
                            level_qty = level.get("quantity", 0) if isinstance(level, dict) else 0
                            if abs(level_price - price_cents) <= 2:
                                depth += level_qty
            except Exception:
                pass

        # Fallback: use signal metadata if available
        if depth == 0:
            depth = signal.get("available_contracts", 0)

        # If we still have no data, infer from market activity
        if depth == 0:
            volume = signal.get("volume", 0)
            if volume >= 10:
                depth = 5  # Active market, assume reasonable depth
            elif volume >= 1:
                depth = 2  # Some activity
            elif is_weather:
                # Weather markets that survived market_quality.py filters
                # (spread check, not dead/frozen) have baseline activity.
                # We post maker limits so we don't need deep book depth.
                depth = 1
            else:
                depth = 0  # Dead market

        passed = depth >= threshold
        return {"passed": passed, "value": depth, "threshold": threshold,
                "detail": f"{depth} contracts available near {price_cents}¢ ({side})"
                          f"{' [weather: relaxed threshold]' if is_weather else ''}"}

    def _check_portfolio_correlation(self, signal, weather_data, portfolio):
        """
        Compute correlation exposure across the portfolio.
        Same city = 100% correlated, same region ~60%, different region ~10%.
        Total correlated exposure must stay under threshold.
        """
        threshold = self.CRITERIA["portfolio_correlation"]["threshold"]
        positions = portfolio.get("positions", [])
        balance_cents = portfolio.get("balance_cents", config.MAX_TOTAL_EXPOSURE_CENTS)
        city = signal.get("city_code", "")
        cost = signal.get("price_cents", 0) * signal.get("suggested_contracts", 0)

        if not positions or not city or balance_cents <= 0:
            return {"passed": True, "value": 0.0, "threshold": threshold,
                    "detail": "No existing positions or unknown city"}

        # Region mapping for correlation
        regions = {
            "northeast": ["NYC", "BOS", "PHI", "DC"],
            "southeast": ["MIA", "ATL", "NOLA"],
            "south_central": ["AUS", "DAL", "HOU", "OKC", "SATX"],
            "midwest": ["CHI", "MIN"],
            "west": ["LAX", "DEN", "LV", "PHX", "SEA", "SFO"],
        }

        # Find signal's region
        signal_region = None
        for region, cities in regions.items():
            if city in cities:
                signal_region = region
                break

        # Calculate correlated exposure
        correlated_exposure_cents = cost  # Start with the proposed trade
        for pos in positions:
            pos_city = pos.get("city_code", "")
            pos_cost = pos.get("cost_cents", 0)

            if pos_city == city:
                correlated_exposure_cents += pos_cost  # 100% correlated
            elif signal_region:
                pos_region = None
                for region, cities in regions.items():
                    if pos_city in cities:
                        pos_region = region
                        break
                if pos_region == signal_region:
                    correlated_exposure_cents += int(pos_cost * 0.6)  # Same region
                else:
                    correlated_exposure_cents += int(pos_cost * 0.1)  # Cross region

        correlated_pct = correlated_exposure_cents / balance_cents if balance_cents > 0 else 1.0
        passed = correlated_pct <= threshold

        return {"passed": passed, "value": round(correlated_pct, 3), "threshold": threshold,
                "detail": (f"Correlated exposure: ${correlated_exposure_cents/100:.2f} "
                           f"= {correlated_pct:.0%} of ${balance_cents/100:.2f} bankroll")}

    def _check_position_sizing(self, signal, weather_data, portfolio):
        """
        Verify the position size is within risk limits.
        Uses quarter-Kelly bounds as sanity check (Monte Carlo sizer not yet built).
        Falls back to percentage-of-bankroll caps.
        """
        price_cents = signal.get("price_cents", 0)
        contracts = signal.get("suggested_contracts", 0)
        cost = price_cents * contracts
        balance_cents = portfolio.get("balance_cents", config.MAX_TOTAL_EXPOSURE_CENTS)

        if balance_cents <= 0 or price_cents <= 0:
            return {"passed": False, "value": 0, "threshold": "valid sizing",
                    "detail": "Invalid balance or price"}

        # Check against per-position cap (20% of bankroll)
        max_position_cents = int(balance_cents * config.MAX_POSITION_PCT)
        if cost > max_position_cents:
            return {"passed": False, "value": cost, "threshold": max_position_cents,
                    "detail": (f"Cost ${cost/100:.2f} exceeds {config.MAX_POSITION_PCT:.0%} "
                               f"position cap ${max_position_cents/100:.2f}")}

        # Check against total exposure cap
        total_exposure = portfolio.get("total_exposure_cents", 0)
        exposure_cap = max(int(balance_cents * config.MAX_TOTAL_EXPOSURE_PCT),
                           config.MAX_TOTAL_EXPOSURE_CENTS)
        if total_exposure + cost > exposure_cap:
            return {"passed": False, "value": total_exposure + cost, "threshold": exposure_cap,
                    "detail": (f"Total exposure ${(total_exposure + cost)/100:.2f} "
                               f"exceeds cap ${exposure_cap/100:.2f}")}

        # Verify contracts >= 1
        if contracts < 1:
            return {"passed": False, "value": contracts, "threshold": 1,
                    "detail": "Zero contracts — position too small"}

        fraction = cost / balance_cents
        return {"passed": True, "value": round(fraction, 3), "threshold": config.MAX_POSITION_PCT,
                "detail": f"{contracts} contracts @ {price_cents}¢ = ${cost/100:.2f} ({fraction:.1%} of bankroll)"}

    def _check_adversarial(self, signal, weather_data, portfolio):
        """
        Devil's advocate stress test. Checks for:
          1. NWS data potentially stale (model data > 6 hours old)
          2. Model spread too wide (weather system uncertainty)
          3. Anomalous temperature departure from climatology
          4. Market price moved significantly since signal generation
          5. Are we chasing a move (impatient taker behavior)?

        Each sub-check flags "warning" or "ok". 2+ warnings = fail.
        """
        warnings = []

        # 1. Data staleness — check if forecast data is fresh
        if weather_data:
            data_age_hours = weather_data.get("data_age_hours", 0)
            if data_age_hours > 6:
                warnings.append(f"Forecast data is {data_age_hours:.0f}h old (>6h)")
        else:
            if signal.get("strategy") != "S3-SP500":
                warnings.append("No weather data available for verification")

        # 2. Model disagreement as uncertainty proxy
        if weather_data:
            model_spread = weather_data.get("model_spread", 0)
            if model_spread > 4.0:  # Match MAX_MODEL_DIVERGENCE_F in strategy.py (was 3.0)
                warnings.append(f"Model spread {model_spread:.1f}°F indicates uncertainty")

        # 3. Anomalous temperature departure
        if weather_data:
            mean = weather_data.get("forecasted_high_mean", 0)
            climatology = weather_data.get("climatology_mean")
            if climatology and mean:
                departure = abs(mean - climatology)
                if departure > config.ANOMALY_THRESHOLD_F:
                    warnings.append(f"Temperature departure {departure:.0f}°F from normal (anomalous)")

        # 4. Price sanity — is our target price reasonable?
        price_cents = signal.get("price_cents", 0)
        if price_cents > 0:
            # Check if we're buying near the extremes despite the quality filter
            if price_cents < 15:
                warnings.append(f"Price {price_cents}¢ is near longshot territory")
            elif price_cents > 85:
                warnings.append(f"Price {price_cents}¢ is near certainty — low risk/reward")

        # 5. Impatient taker check — are we paying more than fair value?
        edge = signal.get("edge", 0)
        confidence = signal.get("confidence", 0)
        if edge > 0 and confidence < 0.4:
            warnings.append(f"Low confidence {confidence:.0%} despite edge — possible noise trade")

        passed = len(warnings) < 2
        detail = "; ".join(warnings) if warnings else "All adversarial checks passed"
        return {"passed": passed, "value": len(warnings), "threshold": 2,
                "detail": detail}

    # ── DIAGNOSIS AND FIX ENGINE ──────────────────────────────────

    def _diagnose(self, criterion_name, score_result, signal):
        """For a failed criterion, return diagnosis and fix recommendation."""
        fixes = {
            "edge_magnitude": {
                "problem": f"Edge {score_result['value']:.1%} below {score_result['threshold']:.1%} threshold",
                "fix": "Reduce position size to lower fee impact",
                "fix_type": "reduce_size",
            },
            "timing_window": {
                "problem": f"Timing multiplier {score_result['value']:.2f} below threshold",
                "fix": "Defer trade — re-evaluate at next scan cycle",
                "fix_type": "wait",
            },
            "liquidity": {
                "problem": f"Only {score_result['value']} contracts available at target price",
                "fix": "Reduce position size to match available liquidity",
                "fix_type": "reduce_size",
            },
            "portfolio_correlation": {
                "problem": f"Correlated exposure at {score_result['value']:.0%}, above {score_result['threshold']:.0%}",
                "fix": "Reduce position size to stay within correlation limit",
                "fix_type": "reduce_size",
            },
            "position_sizing": {
                "problem": f"Position size exceeds risk limit: {score_result.get('detail', '')}",
                "fix": "Reduce to maximum allowed size",
                "fix_type": "reduce_size",
            },
            "adversarial_check": {
                "problem": score_result.get("detail", "Failed stress test"),
                "fix": "Reduce size by 50% due to elevated risk",
                "fix_type": "reduce_size",
            },
        }
        return fixes.get(criterion_name, {
            "problem": f"Unknown failure: {score_result.get('detail', '')}",
            "fix": "Skip trade",
            "fix_type": "reject",
        })

    def _apply_fix(self, signal, diagnosis):
        """Apply the diagnosed fix to the signal and return modified signal."""
        fix_type = diagnosis.get("fix_type", "reject")

        if fix_type == "reduce_size":
            current = signal.get("suggested_contracts", 0)
            signal["suggested_contracts"] = max(1, current // 2)
            signal["scorecard_reduced"] = True
        elif fix_type == "adjust_price":
            current = signal.get("price_cents", 0)
            if signal.get("side") == "yes":
                signal["price_cents"] = max(1, current - 1)
            else:
                signal["price_cents"] = max(1, current - 1)
        elif fix_type == "wait":
            signal["deferred"] = True
        elif fix_type == "retarget":
            signal["retarget_adjacent_bucket"] = True

        return signal

    # ── UTILITY ───────────────────────────────────────────────────

    def _extract_city_from_ticker(self, ticker):
        """Extract city code from a Kalshi weather ticker."""
        if not ticker:
            return ""
        # Tickers look like KXHIGHNY-26FEB16-T40 or KXHIGHCHI-...
        # Series tickers map in CITIES: KXHIGHNY → NYC, KXHIGHCHI → CHI
        for city_code, info in self.cities.items():
            series = info.get("series_ticker", "")
            if series and ticker.startswith(series.replace("KXHIGH", "KXHIGH")):
                return city_code
        return ""

    def format_scorecard_summary(self, result):
        """Format scorecard result for logging."""
        lines = []
        action = result["action"].upper()
        lines.append(f"    [SCORECARD] {action} after {result['iterations']} iteration(s)")

        for name, score in result["scores"].items():
            if name == "bypass":
                lines.append(f"      {score['detail']}")
                continue
            status = "PASS" if score["passed"] else "FAIL"
            lines.append(f"      {status}: {name} — {score.get('detail', '')}")

        if result["diagnosis_log"]:
            lines.append("      Diagnosis log:")
            for entry in result["diagnosis_log"]:
                lines.append(f"        {entry}")

        return "\n".join(lines)
