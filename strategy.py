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

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.weather = WeatherEngine()
        self.confirmer = SignalConfirmer()
        self.balance_cents = 4000  # Default $40, updated by caller

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
            return self._skip(None, ticker)
        if volume == 0 and open_interest == 0 and last_price == 0:
            return self._skip(None, ticker)
        ref_price = yes_ask if yes_ask > 0 else last_price
        if ref_price <= 1 or ref_price >= 99:
            return self._skip(None, ticker)
        if yes_ask >= 99 and no_ask >= 99:
            return self._skip(None, ticker)

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
        arb = self._strategy_arbitrage(market, yes_ask, no_ask)
        if arb and arb["signal"] == "buy":
            _t = arb["ticker"]
            _e = arb["edge"]
            print(f"  [SIGNAL] {_t} ARB edge={_e:.1%}")
            return arb

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
            return None

        # Step 3: Fetch ensemble distribution
        distribution = self.weather.get_temperature_distribution(
            city_code, target_date
        )
        if not distribution:
            return None

        # Step 4: Calculate bucket probability
        our_prob = self.weather.calculate_bucket_probability(
            distribution, temp_low, temp_high
        )
        if our_prob is None:
            return None

        market_prob = ref_price / 100.0
        raw_edge = our_prob - market_prob  # Positive = underpriced YES

        # Step 5: Determine side and edge
        if raw_edge > 0:
            side = "yes"
            edge = raw_edge
            ya = market.get("yes_ask", 0) or 0
            price_cents = ya if ya > 0 else ref_price
        else:
            side = "no"
            edge = abs(raw_edge)
            price_cents = market.get("no_ask", 0) or (100 - ref_price)

        # Step 6: Fee-adjusted edge
        fee_adjusted_edge = self._calculate_fee_adjusted_edge(
            our_prob, market_prob, side
        )

        # Determine if next-day
        city_tz = CITIES.get(city_code, {}).get("timezone", "America/New_York")
        local_date = datetime.now(ZoneInfo(city_tz)).strftime("%Y-%m-%d")
        is_next_day = target_date > local_date

        # Step 7: Edge threshold check
        min_edge = self._get_edge_threshold(is_next_day, is_confirmed=False)
        if edge < min_edge:
            return None
        if fee_adjusted_edge < config.FEE_ADJUSTED_MIN_EDGE:
            return None

        # Price guardrails
        if price_cents < config.LONGSHOT_FLOOR_CENTS:
            return None
        if price_cents > config.NEAR_CERTAINTY_CAP_CENTS:
            return None

        # Model divergence check (side-aware)
        model_spread = distribution.get("model_spread", 0)
        if side == "yes" and model_spread > config.MAX_MODEL_DIVERGENCE_YES_F:
            return None
        if side == "no" and model_spread > config.MAX_MODEL_DIVERGENCE_NO_F:
            return None

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
            return None

        # Step 9: NO-side guards
        if side == "no":
            passed, reason = self._apply_no_side_guards(
                distribution, temp_low, temp_high, price_cents, verdict
            )
            if not passed:
                return None

        # Rounding buffer (YES and NO)
        forecast_mean = distribution["forecasted_high_mean"]
        rounding_mult = self._rounding_buffer_multiplier(
            forecast_mean, temp_low, temp_high, side
        )
        if rounding_mult == 0.0:
            return None

        # Step 10: Kelly sizing
        contracts = self._kelly_size(
            edge, our_prob, price_cents, self.balance_cents,
            conf_mult * rounding_mult
        )

        # Step 11: Reductions
        if is_next_day:
            contracts = max(1, int(contracts * config.NEXT_DAY_SIZING_MULTIPLIER))
        if side == "no" and price_cents >= 50:
            contracts = max(1, int(contracts * config.NO_SIDE_SIZING_MULTIPLIER))

        # Minimum payout filter
        payout_per = 100 - price_cents
        total_payout = (contracts * payout_per) / 100.0
        if total_payout < config.MIN_PAYOUT_DOLLARS:
            return None

        # Model convergence boost (sizing only, not edge)
        if model_spread < config.MODEL_CONVERGENCE_BOOST_F:
            contracts = max(1, int(contracts * 1.2))

        total_members = distribution.get("total_members", "?")
        std_dev_val = distribution.get("std_dev")

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
                f"ensemble={our_prob:.0%} vs market={market_prob:.0%}, "
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
            if no_price <= 0 or no_price >= 95:
                return None
            if no_price > config.NO_SIDE_MAX_PRICE_CENTS:
                return None

            edge = (100 - no_price) / 100.0
            if edge < config.CONFIRMED_MIN_EDGE:
                return None

            # Confirmed outcome sizing: CONFIRMED_POSITION_PCT of bankroll
            max_bet = int(self.balance_cents * config.CONFIRMED_POSITION_PCT)
            max_bet = min(max_bet, config.DAILY_LOSS_LIMIT_CENTS)
            contracts = min(
                max(1, int(max_bet / no_price)),
                config.MAX_CONTRACTS_PER_TICKER
            )

            print(f"  [CASE1] {city_code} high={todays_high}F > "
                  f"bucket {temp_low}-{temp_high}F + 1F rounding")
            print(f"  [CASE1] NO @ {no_price}c -> {contracts} contracts")

            return {
                "signal": "buy",
                "ticker": ticker,
                "side": "no",
                "edge": round(edge, 4),
                "fee_adjusted_edge": round(edge * 0.93, 4),
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
                dist = self.weather.get_temperature_distribution(
                    city_code, target_date
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

                edge = (100 - no_price) / 100.0
                if edge < config.CONFIRMED_MIN_EDGE:
                    return None

                case3_contracts = self._kelly_size(
                    edge, 0.90, no_price, self.balance_cents, 1.0
                )

                print(f"  [CASE3] {city_code} high={todays_high}F, "
                      f"bucket {temp_low}-{temp_high}F, "
                      f"gap={temp_gap:.0f}F -> STRONG")

                return {
                    "signal": "buy",
                    "ticker": ticker,
                    "side": "no",
                    "edge": round(edge, 4),
                    "fee_adjusted_edge": round(edge * 0.93, 4),
                    "our_prob": round(1.0 - (no_price / 100.0), 4),
                    "market_prob": round(ref_price / 100.0, 4),
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
                    "confirmation_multiplier": 1.5,
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
        """YES_ask + NO_ask < 98c = guaranteed profit."""
        if yes_ask <= 0 or no_ask <= 0:
            return None

        total = yes_ask + no_ask
        if total >= 98:
            return None

        gap = 100 - total
        if gap < config.ARB_MIN_SPREAD_CENTS:
            return None

        # Liquidity sanity: reject phantom quotes
        vol24 = market.get("volume_24h", 0) or 0
        vol = market.get("volume", 0) or 0
        if vol == 0 and vol24 == 0:
            return None

        yes_bid = market.get("yes_bid", 0) or 0
        no_bid = market.get("no_bid", 0) or 0
        if (yes_ask - yes_bid) > 20 or (no_ask - no_bid) > 20:
            return None
        if gap > 15 and vol24 < 10:
            return None

        edge = gap / 100.0
        if yes_ask <= no_ask:
            side, price = "yes", yes_ask
        else:
            side, price = "no", no_ask

        ticker = market.get("ticker", "")
        contracts = self._kelly_size(
            edge, 0.95, price, self.balance_cents, 1.0, is_arb=True
        )

        return {
            "signal": "buy",
            "ticker": ticker,
            "side": side,
            "edge": round(edge, 4),
            "fee_adjusted_edge": round(edge, 4),
            "our_prob": 0.95,
            "market_prob": round(price / 100.0, 4),
            "price_cents": price,
            "suggested_contracts": contracts,
            "reasoning": (
                f"[ARB] YES({yes_ask}c) + NO({no_ask}c) = {total}c. "
                f"Guaranteed {gap}c profit per pair."
            ),
            "strategy": "S2-Arbitrage",
            "confirmation_verdict": "STRONG",
            "confirmation_multiplier": 1.5,
            "city_code": "",
            "target_date": "",
            "close_time": market.get("close_time"),
            "predicted_high": None,
            "model_spread": None,
            "std_dev": None,
        }

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

    def _get_edge_threshold(self, is_next_day, is_confirmed):
        """Minimum raw edge threshold.

        Confirmed: 5%
        Morning (before noon ET): 12%
        Afternoon: 10% (MIN_EDGE)
        Next-day: 15% (1.5x)
        """
        if is_confirmed:
            return config.CONFIRMED_MIN_EDGE

        et_hour = datetime.now(ZoneInfo("America/New_York")).hour

        if is_next_day:
            return config.MIN_EDGE * config.NEXT_DAY_EDGE_MULTIPLIER

        if et_hour < 12:
            return 0.12
        return config.MIN_EDGE

    # ===========================================================
    # FEE-ADJUSTED EDGE
    # ===========================================================

    def _calculate_fee_adjusted_edge(self, our_prob, market_prob, side):
        """Edge after Kalshi 7% fee drag. Side-aware.

        YES profit = (100 - price) per contract.
        NO profit = price per contract (yes_price cents).
        """
        raw_edge = abs(our_prob - market_prob)
        price = market_prob  # market_prob = price / 100

        if side == "yes":
            fee_drag = config.KALSHI_FEE_PCT * (1.0 - price) * our_prob
        else:
            fee_drag = config.KALSHI_FEE_PCT * price * (1.0 - our_prob)

        return max(0.0, raw_edge - fee_drag)

    # ===========================================================
    # QUARTER-KELLY SIZING
    # ===========================================================

    def _kelly_size(self, edge, our_prob, price_cents, balance_cents,
                     confirmation_multiplier, is_confirmed=False,
                     is_arb=False):
        """Quarter-Kelly position sizing for binary markets.

        kelly = (win_prob * payout - loss_prob * cost) / payout
        fraction = kelly / 4 * confirmation_multiplier

        Caps: MAX_POSITION_PCT (5%), CONFIRMED (10%), ARB (15%).
        Minimum: 1 contract.
        """
        if edge <= 0 or price_cents <= 0 or balance_cents <= 0:
            return 1

        prob_win = min(0.95, our_prob)
        payout = 100 - price_cents
        cost = price_cents

        if payout <= 0:
            return 1

        kelly = ((prob_win * payout) - ((1.0 - prob_win) * cost)) / payout
        if kelly <= 0:
            return 1

        fraction = kelly / 4.0 * confirmation_multiplier
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

        contracts = max(1, int(bet_cents / price_cents))
        contracts = min(contracts, config.MAX_CONTRACTS_PER_TICKER)

        return contracts

    # ===========================================================
    # UTILITY
    # ===========================================================

    def _skip(self, reason, ticker=""):
        """Return a skip signal."""
        return {
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
        }
