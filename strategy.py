"""
STRATEGY ENGINE v4.0 -- Weather Edge Detection
================================================
Two strategies: Weather ensemble (S1) and Arbitrage (S2).
No SP500. No scorecard. No convergence boost. No seasonal sizing.

Pipeline:
  1. Fast-reject dead markets
  2. Parse weather bucket (city, date, temp range)
  3. Check confirmed outcome (CASE 1 only, CASE 3 -> STRONG)
  4. Fetch 143-member ensemble distribution
  5. Calculate bucket probability vs market price
  6. Fee-adjusted edge check (7% drag)
  7. Signal confirmation (5-source voting)
  8. NO-side guards (separation, price cap, divergence)
  9. Quarter-Kelly sizing with confirmation multiplier
  10. Reductions (next-day, rounding buffer, NO-expensive)
"""

import math
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from weather_engine import WeatherEngine, CITIES
from signal_confirmer import SignalConfirmer
import config


class Strategy:
    """Edge detection engine. Weather ensemble (S1) + Arbitrage (S2)."""

    def __init__(self, kalshi_client=None, reviewer=None):
        self.client = kalshi_client
        self.weather = WeatherEngine()
        self.confirmer = SignalConfirmer()
        self.reviewer = reviewer
        self.balance_cents = config.BALANCE_FALLBACK_CENTS

    # ===========================================================
    # MAIN ENTRY
    # ===========================================================

    def evaluate_market(self, market, todays_high=None):
        """Evaluate a market for trading signals.

        Args:
            market: Kalshi market dict
            todays_high: Optional observed high so far (float, degF).
                          If provided, enables confirmed outcome checks.

        Returns: signal dict with keys per spec.
        """
        ticker = market.get("ticker", "")
        yes_ask = market.get("yes_ask", 0) or 0
        no_ask = market.get("no_ask", 0) or 0
        last_price = market.get("last_price", 0) or 0
        volume = market.get("volume", 0) or 0
        open_interest = market.get("open_interest", 0) or 0

        # --- FAST REJECT: dead markets ---
        if yes_ask == 0 and no_ask == 0 and last_price == 0:
            return self._skip("Dead market", ticker)
        if volume == 0 and open_interest == 0 and last_price == 0:
            return self._skip("Dead market", ticker)
        ref_price = yes_ask if yes_ask > 0 else last_price
        if ref_price <= 1 or ref_price >= 99:
            return self._skip("Dead market", ticker)
        if yes_ask >= 99 and no_ask >= 99:
            return self._skip("Dead market", ticker)

        # --- Try weather strategy ---
        signal = self._strategy_weather(market, ref_price, todays_high)
        if signal and signal["signal"] == "buy":
            _t = signal["ticker"]
            _s = signal["side"]
            _e = signal["edge"]
            _v = signal["confirmation_verdict"]
            print(f"  [SIGNAL] {_t} {_s.upper()} edge={_e:.1%} verdict={_v}")
            return signal

        # --- Try arbitrage ---
        if getattr(config, "ENABLE_ARBITRAGE_STRATEGY", False):
            arb = self._strategy_arbitrage(market, yes_ask, no_ask)
            if arb and arb["signal"] == "buy":
                _t = arb["ticker"]
                _e = arb["edge"]
                print(f"  [SIGNAL] {_t} ARB edge={_e:.1%}")
                return arb

        # Propagate the rich skip from weather strategy (has forecast data)
        if signal and signal.get("signal") == "skip":
            return signal

        return self._skip(f"No signal for {ticker}", ticker)

    # ===========================================================
    # S1: WEATHER ENSEMBLE EDGE
    # ===========================================================

    def _strategy_weather(self, market, ref_price, todays_high=None):
        """Core weather edge detection."""
        ticker = market.get("ticker", "")

        # Step 1: Parse bucket
        parsed = self.weather.parse_market_bucket(market)
        if not parsed:
            return None

        city_code = parsed["city_code"]
        temp_low = parsed["temp_low"]
        temp_high = parsed["temp_high"]
        target_date = parsed.get("target_date")
        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Step 2: Check confirmed outcome (needs todays_high)
        if todays_high is not None:
            confirmed = self._check_confirmed_outcome(
                market, city_code, target_date, temp_low, temp_high,
                ref_price, todays_high
            )
            if confirmed:
                return confirmed

        # Dead market check
        vol24 = market.get("volume_24h", 0) or 0
        vol = market.get("volume", 0) or 0
        if vol == 0 and vol24 == 0:
            return self._skip("no_volume", ticker,
                              city_code=city_code, target_date=target_date)

        # Step 3: Fetch ensemble distribution
        model_weights, city_bias_correction = self._get_learning_adjustments(city_code)
        distribution = self.weather.get_temperature_distribution(
            city_code,
            target_date,
            model_weights=model_weights,
            city_bias_f=city_bias_correction,
        )
        if not distribution:
            from weather_engine import _in_fetch_window
            reason = "outside_fetch_window" if not _in_fetch_window() else "ensemble_fetch_failed"
            return self._skip(reason, ticker,
                              city_code=city_code, target_date=target_date)

        # Step 4: Calculate bucket probability
        our_prob = self.weather.calculate_bucket_probability(
            distribution, temp_low, temp_high
        )
        if our_prob is None:
            return self._skip("bucket_prob_failed", ticker,
                              city_code=city_code, target_date=target_date,
                              predicted_high=distribution.get("forecasted_high_mean"),
                              model_spread=distribution.get("model_spread"),
                              model_means=distribution.get("model_means", {}))

        yes_price = market.get("yes_ask", 0) or ref_price
        no_price = market.get("no_ask", 0) or max(1, 100 - yes_price)
        yes_market_prob = yes_price / 100.0
        no_market_prob = no_price / 100.0

        yes_edge = our_prob - yes_market_prob
        no_prob = 1.0 - our_prob
        no_edge = no_prob - no_market_prob

        # Common fields for rich skip signals (post-distribution)
        forecast_mean = distribution.get("forecasted_high_mean")
        if forecast_mean is None:
            return self._skip("no_forecast_mean", ticker,
                              city_code=city_code, target_date=target_date)
        model_spread = distribution.get("model_spread", 0)
        model_means = distribution.get("model_means", {})
        std_dev_val = distribution.get("std_dev")
        model_stds = distribution.get("model_stds", {})
        _dist_fields = dict(
            city_code=city_code, target_date=target_date,
            predicted_high=forecast_mean, model_spread=model_spread,
            model_means=model_means, model_stds=model_stds,
            std_dev=std_dev_val,
            our_prob=round(our_prob, 4),
            market_prob=round(yes_market_prob, 4),
            strategy="S1-Weather",
        )

        if yes_edge <= 0 and no_edge <= 0:
            return self._skip("no_edge", ticker, **_dist_fields)

        if yes_edge >= no_edge:
            side = "yes"
            edge = yes_edge
            price_cents = yes_price
            market_prob = yes_market_prob
            win_prob = our_prob
        else:
            side = "no"
            edge = no_edge
            price_cents = no_price
            market_prob = no_market_prob
            win_prob = no_prob

        # Step 6: Fee-adjusted edge
        fee_adjusted_edge = self._calculate_fee_adjusted_edge(
            win_prob, market_prob
        )

        # Determine if next-day
        city_tz = CITIES.get(city_code, {}).get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(city_tz))
        local_date = local_now.strftime("%Y-%m-%d")
        local_hour = local_now.hour
        is_next_day = target_date > local_date

        # Helper: side-aware skip fields (available after side selection)
        def _side_skip(reason, **extra):
            fields = dict(_dist_fields, side=side, edge=round(edge, 4),
                          fee_adjusted_edge=round(fee_adjusted_edge, 4),
                          price_cents=price_cents, market_prob=round(market_prob, 4))
            fields.update(extra)
            return self._skip(reason, ticker, **fields)

        # Block same-day trades before 6 AM local -- overnight forecasts are stale
        if not is_next_day and local_hour < 6:
            return _side_skip("before_6am_local")

        # Convergence score (same-day afternoon only)
        convergence_score = 0.0
        if not is_next_day and local_hour >= 14 and todays_high is not None:
            convergence_score = self._compute_convergence_score(
                city_code, todays_high, distribution, local_hour)

        # Step 7: Edge threshold check
        min_edge = self._get_edge_threshold(
            is_next_day, is_confirmed=False,
            local_hour=local_hour if not is_next_day else None,
            convergence_score=convergence_score)
        if edge < min_edge:
            return _side_skip("edge_below_threshold",
                              min_edge_required=round(min_edge, 4))
        if fee_adjusted_edge < config.FEE_ADJUSTED_MIN_EDGE:
            return _side_skip("fee_adj_edge_below_threshold")

        # Price guardrails
        if price_cents < config.LONGSHOT_FLOOR_CENTS:
            return _side_skip("longshot_floor")
        if price_cents > config.NEAR_CERTAINTY_CAP_CENTS:
            return _side_skip("near_certainty_cap")

        # Model divergence check (side-aware)
        if side == "yes" and model_spread > config.MAX_MODEL_DIVERGENCE_YES_F:
            return _side_skip("model_divergence_yes")
        if side == "no" and model_spread > config.MAX_MODEL_DIVERGENCE_NO_F:
            return _side_skip("model_divergence_no")

        # Step 8: Confirm signal
        city_info = CITIES.get(city_code, {})
        confirmation = self.confirmer.confirm_signal(
            city_info=city_info,
            target_date=target_date,
            temp_low=temp_low,
            temp_high=temp_high,
            ensemble_prob=our_prob,
            market_price_cents=ref_price,
        )
        verdict = confirmation["verdict"]
        conf_mult = confirmation["size_multiplier"]

        if verdict == "REJECT":
            # High-edge override: if ensemble strongly disagrees with market AND
            # it's not early morning, allow with minimal sizing
            if (edge >= 0.25 and not is_next_day
                    and local_hour is not None and local_hour >= 9):
                conf_mult = 0.4  # Heavy penalty but not zero
            else:
                return _side_skip("confirmation_reject",
                                  confirmation_verdict="REJECT")

        # Step 9: NO-side guards
        if side == "no":
            passed, reason = self._apply_no_side_guards(
                distribution, temp_low, temp_high, price_cents, verdict
            )
            if not passed:
                return _side_skip("no_side_guard",
                                  confirmation_verdict=verdict)

        # Rounding buffer (YES and NO)
        rounding_mult = self._rounding_buffer_multiplier(
            forecast_mean, temp_low, temp_high, side
        )
        if rounding_mult == 0.0:
            return _side_skip("rounding_buffer",
                              confirmation_verdict=verdict)

        # Step 10: Kelly sizing (convergence boost applied pre-caps via multiplier)
        # Don't boost convergence on REJECT-overridden trades (contradictory signals)
        conv_mult = 1.0
        if (verdict != "REJECT"
                and convergence_score > config.CONVERGENCE_SCORE_THRESHOLD):
            conv_mult = 1.0 + config.CONVERGENCE_SIZING_BOOST * convergence_score
        contracts = self._kelly_size(
            edge, win_prob, price_cents, self.balance_cents,
            conf_mult * rounding_mult * conv_mult, model_spread=model_spread
        )
        if contracts <= 0:
            return _side_skip("kelly_undersized",
                              confirmation_verdict=verdict)

        # Step 11: Reductions
        if is_next_day:
            contracts = max(1, int(contracts * config.NEXT_DAY_SIZING_MULTIPLIER))
        if side == "no" and price_cents >= 50:
            contracts = max(1, int(contracts * config.NO_SIDE_SIZING_MULTIPLIER))
        # Early morning NO penalty: stale forecasts + NO-side = compounding risk
        if side == "no" and not is_next_day and local_hour is not None and local_hour < 9:
            contracts = max(1, int(contracts * 0.5))

        # Minimum payout filter
        payout_per = 100 - price_cents
        total_payout = (contracts * payout_per) / 100.0
        if total_payout < config.MIN_PAYOUT_DOLLARS:
            return _side_skip("min_payout",
                              confirmation_verdict=verdict,
                              suggested_contracts=contracts)

        # Model convergence boost (sizing only, not edge)
        if model_spread < config.MODEL_CONVERGENCE_BOOST_F:
            contracts = max(1, int(contracts * 1.2))

        total_members = distribution.get("total_members", "?")

        return {
            "signal": "buy",
            "ticker": ticker,
            "side": side,
            "edge": round(edge, 4),
            "fee_adjusted_edge": round(fee_adjusted_edge, 4),
            "our_prob": round(our_prob, 4),
            "market_prob": round(market_prob, 4),
            "price_cents": price_cents,
            "suggested_contracts": contracts,
            "reasoning": (
                f"[S1] {city_code} {target_date}: "
                f"ensemble={win_prob:.0%} vs market={market_prob:.0%}, "
                f"edge={edge:.1%} (fee-adj={fee_adjusted_edge:.1%}). "
                f"Verdict={verdict}. "
                f"Mean={forecast_mean:.1f}F, spread={model_spread:.1f}F. "
                f"{total_members} members."
            ),
            "strategy": "S1-Weather",
            "confirmation_verdict": verdict,
            "confirmation_multiplier": conf_mult,
            "city_code": city_code,
            "target_date": target_date,
            "close_time": market.get("close_time"),
            "predicted_high": forecast_mean,
            "model_spread": model_spread,
            "std_dev": std_dev_val,
            "model_means": distribution.get("model_means", {}),
            "model_stds": distribution.get("model_stds", {}),
        }

    # ===========================================================
    # CONFIRMED OUTCOME DETECTION
    # ===========================================================

    def _check_confirmed_outcome(self, market, city_code, target_date,
                                  temp_low, temp_high, ref_price,
                                  todays_high):
        """CASE 1: observed high already exceeded bucket ceiling + 1F rounding.
        CASE 3: gap too large for bucket -> returns STRONG (not confirmed).
        CASE 2: DELETED.
        """
        ticker = market.get("ticker", "")

        # Must have a parsed target_date
        if not target_date:
            return None

        # Only today's markets
        city_info = CITIES.get(city_code, {})
        tz_name = city_info.get("timezone", "America/New_York")
        local_now = datetime.now(ZoneInfo(tz_name))
        local_hour = local_now.hour
        local_date = local_now.strftime("%Y-%m-%d")

        if target_date != local_date:
            return None
        if local_hour < config.CASE1_MIN_LOCAL_HOUR:
            return None

        # ---- CASE 1: High already ABOVE bucket upper bound ----
        # If observed high > temp_high + 1F rounding, the daily high is above
        # this bucket. Buy NO on this bucket (temp was above, not in it).
        if todays_high > temp_high + config.ROUNDING_BUFFER_HARD_F:
            no_price = market.get("no_ask", 0) or (100 - ref_price)
            if no_price <= 0 or no_price >= 98:
                return None
            # CASE1 bypasses NO_SIDE_MAX_PRICE_CENTS (near-guaranteed outcome)

            edge = 0.99 - (no_price / 100.0)
            if edge < config.CASE1_MIN_EDGE:
                return None

            # Confirmed outcome sizing via Kelly with CONFIRMED_POSITION_PCT cap
            contracts = self._kelly_size(
                edge, 0.99, no_price, self.balance_cents, 1.0,
                is_confirmed=True
            )
            if contracts <= 0:
                contracts = 1
            # Apply NO sizing multiplier for expensive NO contracts
            if no_price >= 50:
                contracts = max(1, int(contracts * config.NO_SIDE_SIZING_MULTIPLIER))

            print(f"  [CASE1] {city_code} high={todays_high}F > "
                  f"bucket {temp_low}-{temp_high}F + 1F rounding")
            print(f"  [CASE1] NO @ {no_price}c -> {contracts} contracts")

            return {
                "signal": "buy",
                "ticker": ticker,
                "side": "no",
                "edge": round(edge, 4),
                "fee_adjusted_edge": round(self._calculate_fee_adjusted_edge(0.99, no_price / 100.0), 4),
                "our_prob": 0.99,
                "market_prob": round(no_price / 100.0, 4),
                "price_cents": no_price,
                "suggested_contracts": contracts,
                "reasoning": (
                    f"[CASE1] {city_code}: observed high {todays_high}F exceeds "
                    f"bucket {temp_low}-{temp_high}F. NO @ {no_price}c "
                    f"near-guaranteed. {contracts} contracts."
                ),
                "strategy": "S1-Weather",
                "confirmation_verdict": "CONFIRMED_OUTCOME",
                "confirmation_multiplier": 1.0,
                "city_code": city_code,
                "target_date": target_date,
                "close_time": market.get("close_time"),
                "predicted_high": todays_high,
                "model_spread": None,
                "std_dev": None,
            }

        # ---- CASE 3: Gap too large for bucket -> STRONG ----
        # NWS rounding: reduce gap by 1F (real temp could be higher)
        temp_gap = temp_low - todays_high - config.ROUNDING_BUFFER_HARD_F

        # Cooling gate: before 2 PM, require evidence peak has passed.
        # Without trade_intelligence dependency, skip CASE 3 pre-afternoon.
        case3_ok = True
        if local_hour < config.CASE3_COOLING_REQUIRED_BEFORE_HOUR:
            case3_ok = False

        if case3_ok:
            case3_triggered = False
            for min_hour, min_gap in sorted(
                config.CASE3_GAP_THRESHOLDS.items(), reverse=True
            ):
                if local_hour >= min_hour and temp_gap > min_gap:
                    case3_triggered = True
                    break

            if case3_triggered:
                # Ensemble veto: check models don't predict reaching bucket
                model_weights, city_bias_correction = self._get_learning_adjustments(city_code)
                dist = self.weather.get_temperature_distribution(
                    city_code,
                    target_date,
                    model_weights=model_weights,
                    city_bias_f=city_bias_correction,
                )
                if dist:
                    f_mean = dist.get("forecasted_high_mean", 0)
                    veto_gap = (config.CASE3_ENSEMBLE_VETO_GAP_LATE
                                if local_hour >= 15
                                else config.CASE3_ENSEMBLE_VETO_GAP_DEFAULT)
                    if f_mean >= temp_low - veto_gap:
                        case3_triggered = False
                else:
                    case3_triggered = False  # No ensemble = blocked

            if case3_triggered:
                no_price = market.get("no_ask", 0) or (100 - ref_price)
                if no_price <= 0 or no_price >= 95:
                    return None
                if no_price > config.NO_SIDE_MAX_PRICE_CENTS:
                    return None

                # Dynamic probability based on gap size and hour:
                # Larger gap + later hour = higher confidence bucket won't be reached
                # Base: 0.85 at minimum gap. Scales up to 0.95 for large gaps late in day.
                gap_factor = min(1.0, temp_gap / 15.0)  # 15F gap = max confidence
                hour_factor = min(1.0, max(0.0, (local_hour - 13) / 5.0))
                case3_prob = 0.85 + 0.10 * (0.6 * gap_factor + 0.4 * hour_factor)

                edge = case3_prob - (no_price / 100.0)
                if edge < config.CONFIRMED_MIN_EDGE:
                    return None

                case3_spread = dist.get("model_spread") if dist else None
                case3_contracts = self._kelly_size(
                    edge, case3_prob, no_price, self.balance_cents, 1.0,
                    model_spread=case3_spread
                )

                print(f"  [CASE3] {city_code} high={todays_high}F, "
                      f"bucket {temp_low}-{temp_high}F, "
                      f"gap={temp_gap:.0f}F -> STRONG (p={case3_prob:.2f})")

                return {
                    "signal": "buy",
                    "ticker": ticker,
                    "side": "no",
                    "edge": round(edge, 4),
                    "fee_adjusted_edge": round(self._calculate_fee_adjusted_edge(case3_prob, no_price / 100.0), 4),
                    "our_prob": round(case3_prob, 4),
                    "market_prob": round(no_price / 100.0, 4),
                    "price_cents": no_price,
                    "suggested_contracts": case3_contracts,
                    "reasoning": (
                        f"[CASE3] {city_code}: high only {todays_high}F "
                        f"at {local_hour}:00, bucket {temp_low}-{temp_high}F "
                        f"unreachable (gap={temp_gap:.0f}F). "
                        f"NO @ {no_price}c. STRONG (not confirmed)."
                    ),
                    "strategy": "S1-Weather",
                    "confirmation_verdict": "STRONG",
                    "confirmation_multiplier": 1.0,
                    "city_code": city_code,
                    "target_date": target_date,
                    "close_time": market.get("close_time"),
                    "predicted_high": todays_high,
                    "model_spread": None,
                    "std_dev": None,
                }

        return None

    # ===========================================================
    # S2: SPREAD ARBITRAGE
    # ===========================================================

    def _strategy_arbitrage(self, market, yes_ask, no_ask):
        """Arbitrage is disabled until both legs are executed and tracked as a pair."""
        return None

    # ===========================================================
    # NO-SIDE GUARDS
    # ===========================================================

    def _apply_no_side_guards(self, distribution, temp_low, temp_high,
                                price_cents, confirmation_verdict):
        """Consolidated NO-side guards. Returns (passed, reason).

        1. Dynamic separation: max(3.0F, std_dev * 0.8)
           - NO-side expands bucket by +/-1F for NWS rounding
           - CONFIRM gets 1.5x penalty
        2. Price cap: NO > 50c = reject
        3. Model divergence already checked in caller
        """
        # Price cap
        if price_cents > config.NO_SIDE_MAX_PRICE_CENTS:
            return False, (f"NO price {price_cents}c > "
                           f"{config.NO_SIDE_MAX_PRICE_CENTS}c cap")

        # Dynamic separation from expanded bucket
        std_dev = distribution.get("std_dev", 5.0)
        dynamic_sep = max(
            config.NO_SEPARATION_FLOOR_F,
            std_dev * config.NO_SEPARATION_STD_DEV_MULT
        )

        # Expand bucket boundaries by rounding buffer for NO
        eff_low = temp_low - config.ROUNDING_BUFFER_HARD_F
        eff_high = temp_high + config.ROUNDING_BUFFER_HARD_F

        # Use raw (pre-bias) forecast mean for separation
        raw_mean = distribution.get("raw_forecast_mean",
                                     distribution["forecasted_high_mean"])

        if eff_low <= raw_mean <= eff_high:
            separation = 0.0
        elif raw_mean < eff_low:
            separation = eff_low - raw_mean
        else:
            separation = raw_mean - eff_high

        if separation < dynamic_sep:
            return False, (
                f"separation {separation:.1f}F < {dynamic_sep:.1f}F "
                f"(std={std_dev:.1f}F)"
            )

        # CONFIRM-level penalty
        if confirmation_verdict == "CONFIRM":
            confirm_sep = dynamic_sep * config.CONFIRM_NO_SEPARATION_PENALTY
            if separation < confirm_sep:
                return False, (
                    f"CONFIRM separation {separation:.1f}F < "
                    f"{confirm_sep:.1f}F (1.5x penalty)"
                )

        return True, ""

    # ===========================================================
    # ROUNDING BUFFER
    # ===========================================================

    def _rounding_buffer_multiplier(self, forecast_mean, temp_low, temp_high,
                                     side):
        """NWS rounding buffer. Returns sizing multiplier (0.0 = no trade)."""
        if side == "no":
            eff_low = temp_low - config.ROUNDING_BUFFER_HARD_F
            eff_high = temp_high + config.ROUNDING_BUFFER_HARD_F
        else:
            eff_low = temp_low
            eff_high = temp_high

        # Find distance to nearest strike
        if temp_high >= 200:
            nearest = abs(forecast_mean - eff_low)
        elif temp_low <= -100:
            nearest = abs(forecast_mean - eff_high)
        else:
            nearest = min(abs(forecast_mean - eff_low),
                          abs(forecast_mean - eff_high))

        if nearest <= config.ROUNDING_BUFFER_HARD_F:
            return 0.0
        elif nearest <= config.ROUNDING_BUFFER_SOFT_F:
            return 0.5
        return 1.0

    # ===========================================================
    # EDGE THRESHOLD
    # ===========================================================

    def _get_edge_threshold(self, is_next_day, is_confirmed, local_hour=None,
                            convergence_score=0.0):
        """Minimum raw edge threshold.

        Confirmed: 5%
        Convergence (score > 0.7, afternoon, same-day): 5%
        Same-day before 9 AM local: 15% (stale forecast penalty)
        Morning (before noon ET): 12%
        Afternoon: 10% (MIN_EDGE)
        Next-day: 15% (1.5x)
        """
        if is_confirmed:
            return config.CONFIRMED_MIN_EDGE

        # Convergence confidence: high score = sources agree, lower threshold
        if (convergence_score > config.CONVERGENCE_SCORE_THRESHOLD
                and not is_next_day
                and local_hour is not None
                and local_hour >= 14):
            return config.CONFIRMED_MIN_EDGE

        if is_next_day:
            return config.MIN_EDGE * config.NEXT_DAY_EDGE_MULTIPLIER

        # Same-day early morning: stale forecast penalty (stricter than next-day)
        if local_hour is not None and local_hour < 9:
            return config.MIN_EDGE * getattr(config, 'EARLY_MORNING_EDGE_MULTIPLIER', 2.0)

        # Morning premium: use local hour (not ET) so West Coast gets correct threshold
        if local_hour is not None and local_hour < 12:
            return config.MIN_EDGE * 1.2  # Morning: 20% premium over base
        return config.MIN_EDGE

    # ===========================================================
    # FEE-ADJUSTED EDGE
    # ===========================================================

    def _calculate_fee_adjusted_edge(self, win_prob, market_prob):
        """Edge after a simple fee drag approximation on the actual contract price."""
        raw_edge = max(0.0, win_prob - market_prob)
        fee_drag = config.KALSHI_FEE_PCT * (1.0 - market_prob) * win_prob
        return max(0.0, raw_edge - fee_drag)

    # ===========================================================
    # QUARTER-KELLY SIZING
    # ===========================================================

    def _kelly_size(self, edge, win_prob, price_cents, balance_cents,
                     confirmation_multiplier, is_confirmed=False,
                     is_arb=False, model_spread=None):
        """Graduated Kelly position sizing for binary markets.

        Graduated divisor based on edge magnitude:
          Edge 5-10%:  Kelly/6 (conservative on thin edges)
          Edge 10-20%: Kelly/4 (standard)
          Edge 20%+:   Kelly/3 (aggressive on fat edges)

        Dispersion multiplier: scales down when models disagree.
          dispersion_mult = 1.0 / (1.0 + model_spread / 5.0)

        Caps: MAX_POSITION_PCT (5%), CONFIRMED (10%), ARB (15%).
        """
        if edge <= 0 or price_cents <= 0 or balance_cents <= 0:
            return 0
        if balance_cents < 500:  # Below  -- not enough to trade safely
            return 0

        prob_win = min(0.99, max(0.01, win_prob))
        gross_payout = 100 - price_cents
        net_payout = gross_payout * (1.0 - config.KALSHI_FEE_PCT)
        cost = price_cents

        if net_payout <= 0:
            return 0

        kelly = ((prob_win * net_payout) - ((1.0 - prob_win) * cost)) / net_payout
        if kelly <= 0:
            return 0

        # Graduated Kelly divisor based on edge magnitude
        if edge >= 0.20:
            divisor = 3.0
        elif edge >= 0.10:
            divisor = 4.0
        else:
            divisor = 6.0

        fraction = kelly / divisor * confirmation_multiplier

        # Dispersion multiplier: high model disagreement = lower sizing
        if model_spread is not None and model_spread > 0:
            dispersion_mult = 1.0 / (1.0 + max(0.0, model_spread) / 5.0)
            fraction *= dispersion_mult

        bet_cents = fraction * balance_cents

        if is_arb:
            max_pct = config.ARB_POSITION_PCT
        elif is_confirmed:
            max_pct = config.CONFIRMED_POSITION_PCT
        else:
            max_pct = config.MAX_POSITION_PCT

        max_bet = balance_cents * max_pct
        bet_cents = min(bet_cents, max_bet)
        bet_cents = min(bet_cents, config.MAX_PER_TICKER_CENTS)

        contracts = int(bet_cents / price_cents)
        if contracts <= 0:
            return 0
        contracts = min(contracts, config.MAX_CONTRACTS_PER_TICKER)

        return contracts

    def _get_learning_adjustments(self, city_code):
        """Return reviewer-driven model weights and city bias correction."""
        if not self.reviewer:
            return None, 0.0

        model_data = self.reviewer.get_model_weights().get(city_code, {})
        model_key_map = {
            "GFS": "gfs_ensemble",
            "ECMWF": "ecmwf_ifs",
            "ICON": "icon_eps",
            "GEM": "gem_ensemble",
        }

        weights = {}
        for model_name, payload in model_data.items():
            quant_key = model_key_map.get(model_name)
            if not quant_key:
                continue
            weight = payload.get("weight") if isinstance(payload, dict) else None
            if weight is not None:
                weights[quant_key] = weight

        bias_payload = self.reviewer.get_city_biases().get(city_code, {})
        city_bias = bias_payload.get("bias", 0.0) if isinstance(bias_payload, dict) else 0.0
        city_bias_correction = -float(city_bias or 0.0)

        return weights or None, city_bias_correction

    # ===========================================================
    # UTILITY
    # ===========================================================

    def _compute_convergence_score(self, city_code, obs_high, distribution, local_hour):
        """Compute convergence score from observations vs forecast.

        Returns 0.0-1.0. Higher = obs tracking forecast closely + models agree.
        Used for late-day confidence trades when everything converges.
        """
        if obs_high is None or not distribution or local_hour < 14:
            return 0.0
        forecast_mean = distribution.get("forecasted_high_mean", 0)
        model_spread = distribution.get("model_spread", 5.0)
        tracking_error = abs(obs_high - forecast_mean)
        hour_factor = min(1.0, max(0.0, (local_hour - 13) / 5.0))
        score = (max(0.0, 1.0 - (tracking_error / 5.0))
                 * max(0.0, 1.0 - (model_spread / 8.0))
                 * hour_factor)
        if score > 0.5:
            print("  [CONVERGENCE] %s score=%.2f" % (city_code, score))
        return score

    # ===========================================================
    # BUCKET INCONSISTENCY DETECTION
    # ===========================================================

    def detect_bucket_inconsistencies(self, markets):
        """Detect pricing inconsistencies within a city's temperature bucket set.

        All exclusive buckets for one event (city+date) should sum to ~100c.
        If total deviates significantly, some bucket is mispriced.

        Args:
            markets: list of market dicts from scanner

        Returns:
            list of inconsistency dicts, sorted by abs(deviation)
        """
        # Group by event_ticker
        events = {}
        for m in markets:
            event = m.get("event_ticker", "")
            if not event:
                continue
            if event not in events:
                events[event] = []
            events[event].append(m)

        inconsistencies = []

        for event_ticker, event_markets in events.items():
            # Skip small events (need enough buckets for meaningful sum)
            if len(event_markets) < config.BUCKET_SUM_MIN_MARKETS:
                continue

            # Sum all yes_ask prices (only count markets with active asks)
            active_markets = []
            total_yes = 0
            for m in event_markets:
                yes_ask = m.get("yes_ask") or 0
                if yes_ask > 0:
                    active_markets.append(m)
                    total_yes += yes_ask

            if len(active_markets) < config.BUCKET_SUM_MIN_MARKETS:
                continue

            deviation = total_yes - 100
            if abs(deviation) < config.BUCKET_SUM_DEVIATION_CENTS:
                continue

            # Find city_code from first market
            city_code = ""
            for m in active_markets:
                city_code = m.get("_city_code", "")
                if city_code:
                    break

            inconsistencies.append({
                "event_ticker": event_ticker,
                "city_code": city_code,
                "total_yes_cents": total_yes,
                "deviation_cents": deviation,
                "num_buckets": len(active_markets),
                "num_total_buckets": len(event_markets),
            })

        inconsistencies.sort(key=lambda x: abs(x["deviation_cents"]), reverse=True)
        return inconsistencies

    def _skip(self, reason, ticker="", **kwargs):
        """Return a skip signal. Extra kwargs override defaults for learning."""
        base = {
            "signal": "skip",
            "ticker": ticker,
            "side": "",
            "edge": 0.0,
            "fee_adjusted_edge": 0.0,
            "our_prob": 0.0,
            "market_prob": 0.0,
            "price_cents": 0,
            "suggested_contracts": 0,
            "reasoning": reason,
            "strategy": "none",
            "confirmation_verdict": "",
            "confirmation_multiplier": 1.0,
            "city_code": "",
            "target_date": "",
            "close_time": None,
            "predicted_high": None,
            "model_spread": None,
            "std_dev": None,
            "skip_reason": reason,
        }
        base.update(kwargs)
        return base
