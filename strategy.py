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

    @staticmethod
    def _describe_bet(side, temp_low, temp_high):
        """Human-readable description of what a bet means."""
        if temp_low == -100:
            return f"{side.upper()}: temp {'<=' if side == 'yes' else '>'} {temp_high}F"
        elif temp_high == 200:
            return f"{side.upper()}: temp {'>=' if side == 'yes' else '<'} {temp_low}F"
        else:
            return f"{side.upper()}: temp {'in' if side == 'yes' else 'NOT in'} [{temp_low},{temp_high}]F"

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
            _bet = signal.get("bet_description", "")
            print(f"  [SIGNAL] {_t} {_s.upper()} edge={_e:.1%} verdict={_v} | {_bet}")
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
        model_weights, city_bias_correction, bias_blocked = self._get_learning_adjustments(city_code)
        if bias_blocked:
            return self._skip("city_bias_blocked", ticker,
                              city_code=city_code, target_date=target_date)
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

        # Block ALL YES-side trades. Data: YES 1W/17L (-$23), NO 2W/1L (+$4).
        # Favourite-longshot bias (Whelan 2025): cheap YES contracts lose more than
        # price implies. Only profitable path is NO-side + CASE 1 confirmed.
        if side == "yes" and not getattr(config, "ALLOW_YES_SIDE_TRADES", False):
            return _side_skip("yes_side_blocked")

        # Direction sanity check: forecast mean must not strongly contradict bet.
        # Catches tail-probability edge where ensemble mean says the opposite.
        _DIRECTION_SANITY_MARGIN_F = 3.0
        if side == "yes":
            # YES on "or below" market: mean should be near/below ceiling
            if temp_low == -100 and forecast_mean > temp_high + _DIRECTION_SANITY_MARGIN_F:
                return _side_skip("direction_sanity_check",
                                  detail=f"mean {forecast_mean:.1f}F >> ceiling {temp_high}F")
            # YES on "or above" market: mean should be near/above floor
            if temp_high == 200 and forecast_mean < temp_low - _DIRECTION_SANITY_MARGIN_F:
                return _side_skip("direction_sanity_check",
                                  detail=f"mean {forecast_mean:.1f}F << floor {temp_low}F")
        elif side == "no":
            # NO on "or below" market (= betting temp ABOVE ceiling): mean should be near/above ceiling
            if temp_low == -100 and forecast_mean < temp_high - _DIRECTION_SANITY_MARGIN_F:
                return _side_skip("direction_sanity_check",
                                  detail=f"NO on below-{temp_high}F but mean {forecast_mean:.1f}F")
            # NO on "or above" market (= betting temp BELOW floor): mean should be near/below floor
            if temp_high == 200 and forecast_mean > temp_low + _DIRECTION_SANITY_MARGIN_F:
                return _side_skip("direction_sanity_check",
                                  detail=f"NO on above-{temp_low}F but mean {forecast_mean:.1f}F")

        # Block same-day directional trades before noon local.
        # Data: morning 19W/24L (-$17), afternoon 18W/0L (+$10.71).
        # CASE 1 confirmed outcomes bypass (checked earlier at _check_confirmed_outcome).
        if not is_next_day and local_hour is not None and local_hour < 12:
            return _side_skip("before_noon_directional")

        # Convergence score (same-day afternoon only)
        convergence_score = 0.0
        if not is_next_day and local_hour >= config.CONVERGENCE_MIN_LOCAL_HOUR and todays_high is not None:
            convergence_score = self._compute_convergence_score(
                city_code, todays_high, distribution, local_hour)

        # Step 7: Edge threshold check
        min_edge = self._get_edge_threshold(
            is_next_day, is_confirmed=False,
            local_hour=local_hour if not is_next_day else None,
            convergence_score=convergence_score,
            city_code=city_code, target_date=target_date)
        # Deferred edge check: afternoon CONFIRM trades get a lower 4% threshold.
        # We don't know the verdict yet (confirmation runs at Step 8), so if edge
        # is between AFTERNOON_CONFIRM_MIN_EDGE and min_edge, defer the rejection.
        _edge_deferred = False
        afternoon_confirm_min = getattr(config, 'AFTERNOON_CONFIRM_MIN_EDGE', None)
        if edge < min_edge:
            if (afternoon_confirm_min is not None
                    and not is_next_day
                    and local_hour is not None and local_hour >= 14
                    and edge >= afternoon_confirm_min):
                _edge_deferred = True  # Will re-check after confirmation
            else:
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
            return _side_skip("confirmation_reject",
                              confirmation_verdict="REJECT")

        # Block STRONG verdict: 2W/13L (-$3.42). Ensemble overconfidence + NWS
        # agreement doesn't overcome the favourite-longshot bias.
        if verdict == "STRONG" and not getattr(config, "ALLOW_STRONG_VERDICTS", False):
            return _side_skip("strong_verdict_blocked",
                              confirmation_verdict="STRONG")

        # Post-confirmation edge gate: if we deferred the edge check for afternoon
        # CONFIRM, verify the verdict is actually CONFIRM (not STRONG/REJECT)
        if _edge_deferred:
            if verdict != "CONFIRM":
                return _side_skip("edge_below_threshold_no_confirm",
                                  min_edge_required=round(min_edge, 4),
                                  confirmation_verdict=verdict)
            print(f"    [SNIPER] {ticker}: afternoon CONFIRM edge={edge:.3f} "
                  f"(normal_min={min_edge:.3f}, confirm_min={afternoon_confirm_min})")

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
            forecast_mean, temp_low, temp_high, side, price_cents=price_cents
        )
        if rounding_mult == 0.0:
            return _side_skip("rounding_buffer",
                              confirmation_verdict=verdict)

        # Step 10: Kelly sizing — ALL multipliers inside 2.0x cap
        conv_mult = 1.0
        if (verdict != "REJECT"
                and convergence_score > config.CONVERGENCE_SCORE_THRESHOLD):
            conv_mult = 1.0 + config.CONVERGENCE_SIZING_BOOST * convergence_score
        # Model convergence boost (moved here so it's inside the cap, not post-Kelly)
        model_conv_mult = 1.0
        total_members = distribution.get("total_members", 0)
        num_models = len(distribution.get("model_means", {}))
        if (model_spread < config.MODEL_CONVERGENCE_BOOST_F
                and num_models >= 2 and total_members >= 80):
            model_conv_mult = 1.2
        total_mult = min(2.0, conf_mult * rounding_mult * conv_mult * model_conv_mult)
        contracts = self._kelly_size(
            fee_adjusted_edge, win_prob, price_cents, self.balance_cents,
            total_mult, model_spread=model_spread, raw_edge=edge
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

        total_members_str = distribution.get("total_members", "?")

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
                f"{total_members_str} members."
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
            "bet_description": self._describe_bet(side, temp_low, temp_high),
            "temp_low": temp_low,
            "temp_high": temp_high,
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
            print(f"  [CASE1-SKIP] {city_code} hour={local_hour} < min={config.CASE1_MIN_LOCAL_HOUR}")
            return None

        # ---- CASE 1: High already ABOVE bucket upper bound ----
        # If observed high > temp_high + 1F rounding, the daily high is above
        # this bucket. Buy NO on this bucket (temp was above, not in it).
        if todays_high > temp_high + config.ROUNDING_BUFFER_HARD_F:
            no_price = market.get("no_ask", 0) or (100 - ref_price)
            case1_cap = getattr(config, 'CASE1_NO_PRICE_CAP', 60)
            if no_price <= 0 or no_price > case1_cap:
                print(f"  [CASE1-SKIP] {city_code} NO price {no_price}c out of range (0,{case1_cap})")
                return None
            # CASE1 cap at 60c (was 98c). Even confirmed outcomes need sane risk/reward.
            # At 98c: pay 98c to win 2c (50:1 against). At 60c: pay 60c to win 40c (1.5:1).

            edge = 0.99 - (no_price / 100.0)
            fee_adj_edge = self._calculate_fee_adjusted_edge(0.99, no_price / 100.0)
            case1_fee_min = getattr(config, "CASE1_FEE_ADJUSTED_MIN_EDGE", 0.01)
            if edge < config.CASE1_MIN_EDGE or fee_adj_edge < case1_fee_min:
                print(f"  [CASE1-SKIP] {city_code} edge={edge:.3f} fee_adj={fee_adj_edge:.3f} "
                      f"below min (raw={config.CASE1_MIN_EDGE}, fee={case1_fee_min})")
                return None

            # Confirmed outcome sizing via Kelly with CONFIRMED_POSITION_PCT cap
            contracts = self._kelly_size(
                fee_adj_edge, 0.99, no_price, self.balance_cents, 1.0,
                is_confirmed=True, raw_edge=edge
            )
            if contracts <= 0:
                print(f"  [CASE1-SKIP] {city_code} Kelly=0 (edge too thin after fees or balance too low)")
                return None
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
                "our_prob": 0.01,  # YES probability (convention: our_prob = YES bucket prob)
                "market_prob": round((100 - no_price) / 100.0, 4),  # YES-side market prob (convention)
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
                "bet_description": self._describe_bet("no", temp_low, temp_high),
                "temp_low": temp_low,
                "temp_high": temp_high,
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
                model_weights, city_bias_correction, _ = self._get_learning_adjustments(city_code)
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
                        print(f"  [CASE3-SKIP] {city_code} ensemble veto: "
                              f"f_mean={f_mean:.1f}F >= {temp_low}-{veto_gap}F threshold")
                        case3_triggered = False
                else:
                    print(f"  [CASE3-SKIP] {city_code} no ensemble data available")
                    case3_triggered = False  # No ensemble = blocked

            if case3_triggered:
                # CASE 3 STRONG blocked: bypasses all safety guards (rounding buffer,
                # model divergence, longshot floor, pre-noon block). Data: STRONG
                # verdict 1W/11L (-$7.41). Only profitable STRONG was a non-CASE3 trade.
                # Re-enable when we have data showing CASE 3 specifically wins.
                print(f"  [CASE3-SKIP] {city_code} CASE 3 STRONG blocked (1W/11L track record)")
                return None

                no_price = market.get("no_ask", 0) or (100 - ref_price)
                if no_price <= 0 or no_price >= 95:
                    print(f"  [CASE3-SKIP] {city_code} NO price {no_price}c out of range (0,95)")
                    return None
                if no_price > config.NO_SIDE_MAX_PRICE_CENTS:
                    print(f"  [CASE3-SKIP] {city_code} NO price {no_price}c > cap {config.NO_SIDE_MAX_PRICE_CENTS}c")
                    return None

                # Dynamic probability based on gap size and hour:
                # Larger gap + later hour = higher confidence bucket won't be reached
                # Base: 0.85 at minimum gap. Scales up to 0.95 for large gaps late in day.
                gap_factor = min(1.0, temp_gap / 15.0)  # 15F gap = max confidence
                hour_factor = min(1.0, max(0.0, (local_hour - 13) / 5.0))
                case3_prob = min(0.90, 0.85 + 0.10 * (0.6 * gap_factor + 0.4 * hour_factor))

                edge = case3_prob - (no_price / 100.0)
                fee_adj_edge = self._calculate_fee_adjusted_edge(case3_prob, no_price / 100.0)
                if edge < config.CONFIRMED_MIN_EDGE or fee_adj_edge < config.FEE_ADJUSTED_MIN_EDGE:
                    print(f"  [CASE3-SKIP] {city_code} edge={edge:.3f} fee_adj={fee_adj_edge:.3f} "
                          f"below min (raw={config.CONFIRMED_MIN_EDGE}, fee={config.FEE_ADJUSTED_MIN_EDGE})")
                    return None

                # Use conservative default spread (8F) when ensemble unavailable
                # to ensure dispersion penalty is applied even without model data
                case3_spread = dist.get("model_spread", 8.0) if dist else 8.0
                case3_contracts = self._kelly_size(
                    fee_adj_edge, case3_prob, no_price, self.balance_cents, 1.0,
                    model_spread=case3_spread, raw_edge=edge
                )
                if case3_contracts <= 0:
                    print(f"  [CASE3-SKIP] {city_code} Kelly=0 (edge too thin after fees)")
                    return None

                print(f"  [CASE3] {city_code} high={todays_high}F, "
                      f"bucket {temp_low}-{temp_high}F, "
                      f"gap={temp_gap:.0f}F -> STRONG (p={case3_prob:.2f})")

                return {
                    "signal": "buy",
                    "ticker": ticker,
                    "side": "no",
                    "edge": round(edge, 4),
                    "fee_adjusted_edge": round(self._calculate_fee_adjusted_edge(case3_prob, no_price / 100.0), 4),
                    "our_prob": round(1.0 - case3_prob, 4),  # YES probability (convention)
                    "market_prob": round((100 - no_price) / 100.0, 4),  # YES-side market prob (convention)
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

        1. Dynamic separation: max(2.0F, std_dev * 0.6)
           - NO-side expands bucket by +/-1F for NWS rounding
           - CONFIRM gets 1.25x penalty
        2. Price cap: NO >= 50c = reject
        3. Model divergence already checked in caller
        """
        # Price cap (>= to match NO_SIDE_SIZING_MULTIPLIER check)
        if price_cents >= config.NO_SIDE_MAX_PRICE_CENTS:
            return False, (f"NO price {price_cents}c >= "
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
                    f"{confirm_sep:.1f}F ({config.CONFIRM_NO_SEPARATION_PENALTY}x penalty)"
                )

        return True, ""

    # ===========================================================
    # ROUNDING BUFFER
    # ===========================================================

    def _rounding_buffer_multiplier(self, forecast_mean, temp_low, temp_high,
                                     side, price_cents=0):
        """NWS rounding buffer. Returns sizing multiplier (0.0 = no trade).

        Blocks trades where the forecast mean is within the NWS rounding error
        margin of a bucket boundary. Hard buffer (1F) = no trade. Soft buffer
        (2F) = 50% sizing, UNLESS the contract is expensive (>50c) — then
        a 1F rounding error means total loss on an expensive bet.
        """
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
            # Expensive contracts in soft buffer = too much risk for rounding uncertainty.
            # A 1F NWS rounding error on an 80c contract = 80c loss.
            if price_cents > 50:
                return 0.0
            return 0.5
        return 1.0

    # ===========================================================
    # EDGE THRESHOLD
    # ===========================================================

    def _get_edge_threshold(self, is_next_day, is_confirmed, local_hour=None,
                            convergence_score=0.0, city_code=None, target_date=None):
        """Minimum raw edge threshold.

        Confirmed: 5% (CONFIRMED_MIN_EDGE)
        Convergence (score > 0.5, afternoon, same-day): 5%
        Next-day: 9% (MIN_EDGE * 1.5)
        Early morning (6-9 AM local): 12% (MIN_EDGE * 2.0)
        Morning (before noon local): 7.2% (MIN_EDGE * 1.2)
        Afternoon: 6% (MIN_EDGE)
        City multiplier: losing cities get higher thresholds (config.CITY_EDGE_MULTIPLIERS)
        """
        if is_confirmed:
            return config.CONFIRMED_MIN_EDGE

        # Convergence confidence: lower threshold to CONFIRMED_MIN_EDGE (5%)
        # when obs tracking tightly + models agree. Bypasses city/seasonal multipliers.
        if (convergence_score > config.CONVERGENCE_SCORE_THRESHOLD
                and local_hour is not None
                and local_hour >= config.CONVERGENCE_MIN_LOCAL_HOUR):
            return config.CONFIRMED_MIN_EDGE

        if is_next_day:
            base = config.MIN_EDGE * config.NEXT_DAY_EDGE_MULTIPLIER
        elif local_hour is not None and local_hour < 9:
            base = config.MIN_EDGE * getattr(config, 'EARLY_MORNING_EDGE_MULTIPLIER', 2.0)
        elif local_hour is not None and local_hour < 12:
            base = config.MIN_EDGE * 1.2
        else:
            base = config.MIN_EDGE

        # Seasonal multiplier (backtest: March 2.7F MAE vs Oct 2.0F)
        month = None
        if target_date:
            try:
                month = datetime.strptime(str(target_date), "%Y-%m-%d").month
            except Exception:
                pass
        seasonal_mult = getattr(config, 'SEASONAL_EDGE_MULTIPLIERS', {}).get(month, 1.0)

        # City-specific multiplier for historically losing cities
        city_mult = getattr(config, 'CITY_EDGE_MULTIPLIERS', {}).get(city_code, 1.0)
        # Cap combined multiplier to avoid impossible thresholds
        # (PHI 2.0x * March 1.2x * morning 1.2x = 2.88x → 17.3% threshold)
        combined_mult = min(city_mult * seasonal_mult, 2.5)
        return base * combined_mult

    # ===========================================================
    # FEE-ADJUSTED EDGE
    # ===========================================================

    def _calculate_fee_adjusted_edge(self, win_prob, market_prob):
        """Edge after exact Kalshi fee on the contract price.

        Kalshi fee per contract (in cents):
            fee = min(fee_rate * price, fee_rate * (100 - price))
        where price = market_prob * 100 cents.
        Convert to probability space by dividing by 100.
        """
        raw_edge = max(0.0, win_prob - market_prob)
        price_frac = market_prob  # 0-1
        fee_frac = config.KALSHI_FEE_PCT * min(price_frac, 1.0 - price_frac)
        return max(0.0, raw_edge - fee_frac)

    # ===========================================================
    # QUARTER-KELLY SIZING
    # ===========================================================

    def _kelly_size(self, edge, win_prob, price_cents, balance_cents,
                     confirmation_multiplier, is_confirmed=False,
                     is_arb=False, model_spread=None, raw_edge=None):
        """Graduated Kelly position sizing for binary markets.

        Graduated divisor based on raw edge magnitude (not fee-adjusted):
          Edge 5-10%:  Kelly/6 (conservative on thin edges)
          Edge 10-20%: Kelly/4 (standard)
          Edge 20%+:   Kelly/3 (aggressive on fat edges)

        Dispersion multiplier: scales down when models disagree.
          dispersion_mult = 1.0 / (1.0 + model_spread / 5.0)

        Caps: MAX_POSITION_PCT (5%), CONFIRMED (10%), ARB (15%).
        """
        if edge <= 0 or price_cents <= 0 or balance_cents <= 0:
            return 0
        if balance_cents < 500:  # Below $5 -- not enough to trade safely
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

        # Continuous Kelly divisor: raw edge 5% -> 6.0, raw edge 15%+ -> 4.0
        # Edge capped at 15% — data: 0-10% edge = 96% win rate, 20%+ = 21%.
        # High edge = ensemble overconfidence, NOT real edge. Treat as 15% max.
        divisor_edge = raw_edge if raw_edge is not None else edge
        divisor_edge = min(divisor_edge, 0.15)  # Was 0.40. Favourite-longshot: high edge = overconfident.
        divisor = max(3.0, min(6.0, 6.0 - (divisor_edge - 0.05) * 20.0))

        fraction = kelly / divisor * confirmation_multiplier

        # High-edge penalty: >20% raw edge gets 50% size reduction.
        # Data: 20%+ edge trades are 5W/19L (-$13.82). Anti-correlated with reality.
        if raw_edge is not None and raw_edge > 0.20:
            fraction *= 0.5

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
        """Return reviewer-driven model weights, city bias correction, and block flag.

        Returns (model_weights or None, city_bias_correction, blocked).
        blocked=True when |learned bias| exceeds threshold, or when pattern
        analysis shows a statistically losing city (<20% win rate, 5+ trades).
        """
        if not self.reviewer:
            return None, 0.0, False

        # Learning kill switch
        if not getattr(config, 'LEARNING_AUTO_APPLY', True):
            return None, 0.0, False

        min_pts = getattr(config, 'LEARNING_MIN_DATA_POINTS', 3)

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
        bias_count = bias_payload.get("count", 0) if isinstance(bias_payload, dict) else 0

        # Only apply bias correction if we have enough data points
        # AND cap at ±3F — backtest shows no city has >2.2F true bias.
        # Larger corrections from small samples are noise, not signal.
        max_bias_correction = getattr(config, 'MAX_BIAS_CORRECTION_F', 3.0)
        if bias_count >= min_pts:
            raw_correction = -float(city_bias or 0.0)
            city_bias_correction = max(-max_bias_correction, min(max_bias_correction, raw_correction))
            if abs(raw_correction) > max_bias_correction:
                print("  [BIAS-CAP] %s correction capped: %.1fF -> %.1fF (count=%d)" % (
                    city_code, raw_correction, city_bias_correction, bias_count))
        else:
            city_bias_correction = 0.0

        # Safety gate 1: block city if learned bias is extreme (model is broken)
        blocked = (abs(city_bias) > config.CITY_BIAS_BLOCK_THRESHOLD_F
                   and bias_count >= config.CITY_BIAS_BLOCK_MIN_COUNT)
        if blocked:
            print("  [BIAS-BLOCK] %s bias=%.1fF count=%d — blocking trades" % (
                city_code, city_bias, bias_count))
            return weights or None, city_bias_correction, True

        # Safety gate 2: block cities with <20% win rate (5+ trades)
        losing = self.reviewer.get_losing_patterns()
        if f"city:{city_code}" in losing:
            pat = losing[f"city:{city_code}"]
            print("  [PATTERN-BLOCK] %s city %dW/%dL (%.0f%%) — blocking" % (
                city_code, pat["wins"], pat["losses"], pat["win_rate"] * 100))
            return weights or None, city_bias_correction, True

        # Use learned weights if available, otherwise backtest-derived defaults
        final_weights = weights if weights else getattr(config, 'DEFAULT_MODEL_WEIGHTS', None)
        return final_weights, city_bias_correction, False

    # ===========================================================
    # UTILITY
    # ===========================================================

    def _compute_convergence_score(self, city_code, obs_high, distribution, local_hour):
        """Compute convergence score from observations vs forecast.

        Returns 0.0-1.0. Higher = obs tracking forecast closely + models agree.
        Used for late-day confidence trades when everything converges.
        """
        if obs_high is None or not distribution or local_hour < config.CONVERGENCE_MIN_LOCAL_HOUR:
            return 0.0
        forecast_mean = distribution.get("forecasted_high_mean", 0)
        model_spread = distribution.get("model_spread", 5.0)
        tracking_error = abs(obs_high - forecast_mean)
        hour_factor = min(1.0, max(0.0, (local_hour - 11) / 6.0))  # noon=0.17, 2PM=0.50, 5PM=1.0
        # Tighter formula: tracking_error/3 (was /5) requires <1.2F error at 2PM
        score = (max(0.0, 1.0 - (tracking_error / 3.0))
                 * max(0.0, 1.0 - (model_spread / 8.0))
                 * hour_factor)
        print(f"    [CONV] {city_code}: score={score:.2f} "
              f"tracking_err={tracking_error:.1f}F spread={model_spread:.1f}F "
              f"hour={local_hour} {'-> TRIGGERED' if score > config.CONVERGENCE_SCORE_THRESHOLD else ''}")
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
