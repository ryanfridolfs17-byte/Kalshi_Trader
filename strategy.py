"""
STRATEGY ENGINE v3.1 — Multi-Market Trading
======================================================
Primary: Weather ensemble edge (S1)
Secondary: Spread arbitrage (S2)
Tertiary: S&P 500 VIX-implied brackets (S3) — toggleable

Decision flow:
  1. Scan Kalshi for weather/SP500 markets
  2. Fetch forecasts → build probability distribution
  3. Compare vs market prices → detect mispricing
  4. Get second opinions from independent sources
  5. Risk check (safety layers)
  6. Size with Quarter-Kelly × confirmation multiplier
  7. Execute as LIMIT order (maker strategy)
"""

import math
from datetime import datetime, timezone
from weather_engine import WeatherEngine, CITIES
from signal_confirmer import SignalConfirmer
from trade_intelligence import TradeIntelligence
from quant_analytics import QuantAnalytics
from market_quality import MarketQualityFilter
from seasonal_confidence import get_seasonal_multiplier, detect_regime as detect_seasonal_regime
import config


class Strategy:
    """
    Multi-strategy evaluation engine.
    Weather is primary, arbitrage is secondary, SP500 is tertiary.
    """

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.weather = WeatherEngine()
        self.confirmer = SignalConfirmer()
        self.intel = TradeIntelligence(kalshi_client, self.weather)
        self.quant = QuantAnalytics(self.weather)
        self.quality = MarketQualityFilter()

        # Conditionally init SP500 components (only if enabled)
        self.vol_engine = None
        self.spx_confirmer = None
        if config.MARKET_TYPES.get("sp500"):
            try:
                from volatility_engine import VolatilityEngine
                from spx_confirmer import SPXConfirmer
                self.vol_engine = VolatilityEngine()
                self.spx_confirmer = SPXConfirmer()
                print("  [STRATEGY] S&P 500 strategy (S3) enabled")
            except Exception as e:
                print(f"  [STRATEGY] WARN: Could not init SP500 strategy: {e}")

    # ═══════════════════════════════════════════════════════
    # MAIN: Evaluate a market
    # ═══════════════════════════════════════════════════════

    def evaluate_market(self, market):
        """
        Run weather strategy and arbitrage against a market.
        Returns the best signal found.
        """
        ticker = market.get("ticker", "")
        title = market.get("title", "Unknown")

        # Extract prices
        yes_bid = market.get("yes_bid", 0) or 0
        yes_ask = market.get("yes_ask", 0) or 0
        no_bid = market.get("no_bid", 0) or 0
        no_ask = market.get("no_ask", 0) or 0
        last_price = market.get("last_price", 0) or 0
        volume = market.get("volume", 0) or 0

        ref_price = yes_ask if yes_ask > 0 else last_price
        spread = (yes_ask - yes_bid) if (yes_ask > 0 and yes_bid > 0) else 99

        # ─── GATE -1: DEAD MARKET FAST REJECT ───
        # Skip markets with no real pricing activity
        # Pattern 1: Price at 99-100¢ or 0-1¢ (settled/certain)
        if ref_price >= 99 or ref_price <= 1:
            return self._skip(None)  # Silent skip
        # Pattern 2: No prices at all
        if yes_bid == 0 and yes_ask == 0 and last_price == 0:
            return self._skip(None)
        # Pattern 3: No bids on either side (nobody trading)
        if yes_bid == 0 and (market.get("no_bid", 0) or 0) == 0:
            if volume == 0:
                return self._skip(None)
        # Pattern 4: Only one side has quotes at extremes
        # (yes_ask=100, no_ask=100 = Kalshi default for untouched markets)
        if yes_ask >= 99 and (market.get("no_ask", 0) or 0) >= 99:
            return self._skip(None)
        # Pattern 5: Zero volume entirely
        if volume == 0 and (market.get("volume_24h", 0) or 0) == 0 and last_price == 0:
            return self._skip(None)

        # ─── GATE 0: MARKET QUALITY FILTER ───
        # Must pass BEFORE any strategy runs (catches illiquid markets, longshots)
        # Note: Kalshi orderbooks are one-sided — yes_bid=0 doesn't mean illiquid.
        # We need to check actual orderbook depth separately from bid/ask spread.
        passed, reason, quality_score = self.quality.check_market(market)
        if not passed:
            if config.LOG_LEVEL == "DEBUG":
                self.quality.print_filter_summary(market, passed, reason, quality_score)
            return self._skip(f"Quality: {reason}")

        signals = []

        # ─── STRATEGY 1: WEATHER ENSEMBLE EDGE (PRIMARY) ───
        weather_signal = self._strategy_weather(market, ref_price, spread, volume)
        if weather_signal:
            signals.append(weather_signal)

        # ─── STRATEGY 2: SPREAD ARBITRAGE (SECONDARY) ───
        arb_signal = self._strategy_arbitrage(market, yes_ask, no_ask)
        if arb_signal:
            signals.append(arb_signal)

        # ─── STRATEGY 3: S&P 500 VIX-IMPLIED BRACKETS ───
        if self.vol_engine and self.spx_confirmer:
            sp500_signal = self._strategy_sp500(market, ref_price, spread, volume)
            if sp500_signal:
                signals.append(sp500_signal)

        # Return best signal
        if not signals:
            return self._skip(f"No signals for {ticker}")

        best = max(signals, key=lambda s: s["edge"] * s["confidence"])

        # Time-based edge threshold: be selective early, save capital for
        # afternoon confirmed outcomes and arbitrage opportunities.
        # Confirmed outcomes and arbitrage bypass this entirely.
        min_edge = self._get_time_adjusted_edge_threshold(best)
        if best["edge"] < min_edge:
            return self._skip(f"Best edge {best['edge']:.1%} below {min_edge:.0%} threshold ({self._get_edge_period()})")

        # Confirmed outcomes already have max sizing — skip normal sizing pipeline
        if best.get("confirmation_verdict") == "CONFIRMED_OUTCOME":
            best["ticker"] = ticker
            return best

        # Set ticker before correlation check (needed for same-ticker detection)
        best["ticker"] = ticker

        # Size the position
        contracts = self._kelly_size(
            best["edge"], best["confidence"], best["price_cents"]
        )

        # Apply confirmation multiplier (weather trades only)
        if best.get("confirmation_multiplier", 1.0) != 1.0:
            contracts = max(1, int(contracts * best["confirmation_multiplier"]))

        # Apply correlation adjustment
        from risk_manager import RiskManager
        try:
            rm = RiskManager()
            corr_mult = self.quant.adjust_for_correlation(best, rm.state.get("positions", []))
            if corr_mult == 0.0:
                return self._skip("Already hold this exact position")
            if corr_mult < 1.0:
                contracts = max(1, int(contracts * corr_mult))
        except Exception as e:
            print(f"  [STRATEGY] Correlation check error (trade proceeds): {e}")

        best["suggested_contracts"] = contracts
        best["order_type"] = "limit"  # Always maker orders
        best["quality_score"] = quality_score

        # Adjust size for market quality
        if quality_score < 0.9:
            contracts = self.quality.adjust_contracts_for_quality(contracts, market, quality_score)
            best["suggested_contracts"] = contracts

        # Minimum payout filter — skip dust trades
        payout_per_contract_cents = 100 - best["price_cents"]
        total_payout_dollars = (contracts * payout_per_contract_cents) / 100.0
        if total_payout_dollars < config.MIN_PAYOUT_DOLLARS:
            print(f"    [STRATEGY] Skipped {ticker}: payout ${total_payout_dollars:.2f} < ${config.MIN_PAYOUT_DOLLARS} minimum (payout_too_small)")
            return self._skip(f"payout_too_small: ${total_payout_dollars:.2f} < ${config.MIN_PAYOUT_DOLLARS}")

        return best

    # ═══════════════════════════════════════════════════════
    # WEATHER STRATEGY
    # ═══════════════════════════════════════════════════════

    def _strategy_weather(self, market, ref_price, spread, volume):
        """
        The main money-maker. Steps:
        1. Parse market to identify city + temperature bucket
        2. Fetch 143-member ensemble forecast
        3. Calculate our probability vs market price
        4. If edge ≥ 8%, get confirmation from independent sources
        5. Return signal with confirmation multiplier
        """
        # Step 1: Parse market
        parsed = self.weather.parse_market_bucket(market)
        if not parsed:
            return None  # Not a weather market

        city_code = parsed["city_code"]
        temp_low = parsed["temp_low"]
        temp_high = parsed["temp_high"]
        target_date = parsed["target_date"]

        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # ─── FAST PATH: CONFIRMED OUTCOME DETECTION ───
        # If NWS observations already show the outcome is determined,
        # this is near-risk-free profit. Max position sizing.
        confirmed_signal = self._check_confirmed_outcome(
            market, city_code, temp_low, temp_high, target_date, ref_price
        )
        if confirmed_signal:
            return confirmed_signal

        # Need some activity (but Kalshi weather markets are low-volume)
        if volume == 0 and (market.get("volume_24h", 0) or 0) == 0:
            return None  # Zero activity = truly dead

        # Spread check: only reject if we can actually calculate a real spread
        # (yes_bid=0 is normal on Kalshi — doesn't mean illiquid)
        if spread < 99 and spread > 40:
            return None  # Genuinely wide spread with real quotes on both sides

        # Step 2: Fetch ensemble distribution
        distribution = self.weather.get_temperature_distribution(city_code, target_date)
        if not distribution:
            return None

        # Step 2b: Apply bias correction (learned from past accuracy)
        distribution = self.intel.apply_bias_to_distribution(distribution, city_code)
        bias_applied = distribution.get("bias_applied", 0)

        # Step 2c: Get time-of-day sizing multiplier
        time_mult, time_reason = self.intel.get_time_multiplier(city_code)

        # Step 3: Calculate our probability for this bucket
        our_prob = self.weather.calculate_bucket_probability(
            distribution, temp_low, temp_high
        )
        if our_prob is None:
            return None

        market_prob = ref_price / 100.0
        edge = our_prob - market_prob  # Positive = underpriced YES

        # Need at least 8% edge
        min_weather_edge = 0.08
        if abs(edge) < min_weather_edge:
            return None

        # Step 4: Get confirmation from independent sources
        city_info = CITIES[city_code]
        confirmation = self.confirmer.confirm_signal(
            city_info=city_info,
            target_date=target_date,
            temp_low=temp_low,
            temp_high=temp_high,
            ensemble_prob=our_prob,
            market_price_cents=ref_price,
        )

        # Print confirmation details
        self.confirmer.print_vote_details(confirmation)

        # Reject if sources disagree
        if confirmation["verdict"] == "REJECT":
            return None

        # Step 4b: Statistical significance check
        stat_test = self.quant.validate_edge_significance(
            our_prob, market_prob, distribution["total_members"]
        )
        if not stat_test["significant"]:
            print(f"    [QUANT] Edge not statistically significant: {stat_test['reason']}")
            return None

        # Step 4c: Regime detection — adjust sizing
        regime = self.quant.detect_regime(distribution)
        regime_mult = regime["size_multiplier"]
        print(f"    [QUANT] Regime: {regime['regime']} ({regime_mult}x) — {regime['reason']}")

        # Step 4d: Seasonal confidence — adjust sizing for city/month/regime
        forecast_high = distribution.get("forecasted_high_mean")
        current_month = datetime.now().month
        seasonal_regime = detect_seasonal_regime(city_code, current_month, forecast_high)
        seasonal_mult = get_seasonal_multiplier(city_code, current_month, seasonal_regime)
        print(f"    [SEASONAL] {city_code} month={current_month}: "
              f"regime={seasonal_regime}, multiplier={seasonal_mult:.2f}")

        # Step 4e: Smart order pricing
        smart_price = self.quant.calculate_optimal_price(market, "yes" if edge > 0 else "no", abs(edge))

        # Step 5: Build signal
        if edge > 0:
            # Narrow bucket guard: 2°F buckets need very high conviction
            bucket_width = temp_high - temp_low
            if bucket_width <= 2:
                if edge < 0.25 or confirmation["agree_count"] < 3:
                    print(f"    [STRATEGY] Skipped YES on narrow {bucket_width}°F bucket "
                          f"{temp_low}-{temp_high}°F: edge={edge:.0%} (need 25%), "
                          f"agree={confirmation['agree_count']}/4 (need 3)")
                    return None

            # Underpriced YES — buy YES
            price = smart_price if smart_price else (market.get("yes_ask", 0) or ref_price)
            signal = {
                "signal": "buy_yes",
                "side": "yes",
                "edge": edge,
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": price,
                "confirmation_multiplier": confirmation["size_multiplier"] * time_mult * regime_mult * seasonal_mult,
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["forecasted_high_mean"],
                "seasonal_regime": seasonal_regime,
                "seasonal_multiplier": seasonal_mult,
                "reasoning": (
                    f"[WEATHER] {city_code} {target_date}: "
                    f"Ensemble says {our_prob:.0%} for {temp_low}-{temp_high}°F, "
                    f"market at {market_prob:.0%} ({ref_price}¢). "
                    f"Edge: +{edge:.0%}. "
                    f"Confirmed: {confirmation['verdict']} ({confirmation['agree_count']} agree). "
                    f"{distribution['total_members']} members. "
                    f"Time: {time_reason} ({time_mult}x). "
                    f"Seasonal: {seasonal_regime} ({seasonal_mult:.2f}x). "
                    + (f"Bias adj: {bias_applied:+.1f}°F. " if bias_applied else "")
                ),
                "strategy": "S1-Weather",
            }
        else:
            # Overpriced YES — buy NO
            # Forecast-strike separation: don't bet NO when the forecast
            # is too close to the bucket range (high chance of landing inside)
            forecast_mean = distribution["forecasted_high_mean"]
            if temp_low <= forecast_mean <= temp_high:
                separation = 0
            elif forecast_mean < temp_low:
                separation = temp_low - forecast_mean
            else:
                separation = forecast_mean - temp_high
            if separation < 3:
                print(f"    [STRATEGY] Skipped NO on {city_code} {temp_low}-{temp_high}°F: "
                      f"forecast {forecast_mean:.1f}°F only {separation:.1f}°F away (insufficient_separation)")
                return None

            no_price = smart_price if smart_price else (market.get("no_ask", 0) or (100 - ref_price))
            signal = {
                "signal": "buy_no",
                "side": "no",
                "edge": abs(edge),
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": no_price,
                "confirmation_multiplier": confirmation["size_multiplier"] * time_mult * regime_mult * seasonal_mult,
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["forecasted_high_mean"],
                "seasonal_regime": seasonal_regime,
                "seasonal_multiplier": seasonal_mult,
                "reasoning": (
                    f"[WEATHER] {city_code} {target_date}: "
                    f"Ensemble says {our_prob:.0%} for {temp_low}-{temp_high}°F, "
                    f"market at {market_prob:.0%} ({ref_price}¢). "
                    f"Edge: {edge:.0%} (OVERPRICED). "
                    f"Confirmed: {confirmation['verdict']}. "
                    f"{distribution['total_members']} members. "
                    f"Time: {time_reason} ({time_mult}x). "
                    f"Seasonal: {seasonal_regime} ({seasonal_mult:.2f}x). "
                    + (f"Bias adj: {bias_applied:+.1f}°F. " if bias_applied else "")
                ),
                "strategy": "S1-Weather",
            }

        return signal

    # ═══════════════════════════════════════════════════════
    # ARBITRAGE STRATEGY
    # ═══════════════════════════════════════════════════════

    def _strategy_arbitrage(self, market, yes_ask, no_ask):
        """
        If YES ask + NO ask < 98¢, buying both sides guarantees profit.
        No confirmation needed — this is pure math.
        BUT: must verify this isn't a phantom quote in a dead market.
        """
        if yes_ask <= 0 or no_ask <= 0:
            return None

        total = yes_ask + no_ask
        if total < 98:
            gap = 100 - total

            # CRITICAL: Verify liquidity before trusting arbitrage
            volume_24h = market.get("volume_24h", 0) or 0
            open_interest = market.get("open_interest", 0) or 0
            spread_yes = (yes_ask - (market.get("yes_bid", 0) or 0))
            spread_no = (no_ask - (market.get("no_bid", 0) or 0))

            # If either side has a huge spread or no volume, it's phantom
            if volume_24h == 0 and (market.get("volume", 0) or 0) == 0:
                return None  # Zero activity — phantom quotes
            if spread_yes > 20 or spread_no > 20:
                return None  # One side is likely a stale quote
            if gap > 15 and volume_24h < 10:
                return None  # Too-good-to-be-true + very low volume = phantom
            edge = gap / 100.0

            if yes_ask <= no_ask:
                side, price = "yes", yes_ask
            else:
                side, price = "no", no_ask

            return {
                "signal": f"buy_{side}",
                "side": side,
                "edge": edge,
                "confidence": 0.95,
                "price_cents": price,
                "confirmation_multiplier": 1.5,  # Max confidence for arb
                "confirmation_verdict": "ARBITRAGE",
                "reasoning": (
                    f"[ARBITRAGE] YES({yes_ask}¢) + NO({no_ask}¢) = {total}¢. "
                    f"Guaranteed {gap}¢ profit per contract pair."
                ),
                "strategy": "S2-Arbitrage",
            }
        return None

    # ═══════════════════════════════════════════════════════
    # S&P 500 STRATEGY
    # ═══════════════════════════════════════════════════════

    def _strategy_sp500(self, market, ref_price, spread, volume):
        """
        S&P 500 daily bracket strategy using VIX-implied volatility.
        Steps:
        1. Parse market to identify price bracket + date
        2. Build VIX-based price distribution
        3. Calculate our probability vs market price
        4. If edge >= 6%, get confirmation
        5. Return signal with market_type: "sp500"
        """
        if not self.vol_engine or not self.spx_confirmer:
            return None

        # Step 1: Parse market
        parsed = self.vol_engine.parse_market_bracket(market)
        if not parsed:
            return None  # Not an S&P 500 bracket market

        price_low = parsed["price_low"]
        price_high = parsed["price_high"]
        target_date = parsed["target_date"]

        if target_date is None:
            from datetime import timezone as tz
            target_date = datetime.now(tz.utc).strftime("%Y-%m-%d")

        # Need some activity
        if volume == 0 and (market.get("volume_24h", 0) or 0) == 0:
            return None

        # Step 2: Get VIX-based distribution
        distribution = self.vol_engine.get_price_distribution(target_date)
        if not distribution:
            return None

        # Step 3: Calculate our probability for this bracket
        our_prob = self.vol_engine.calculate_bracket_probability(
            distribution, price_low, price_high
        )
        if our_prob is None:
            return None

        market_prob = ref_price / 100.0
        edge = our_prob - market_prob

        # Need at least 6% edge for SP500
        if abs(edge) < config.SP500_MIN_EDGE:
            return None

        # Step 4: Get confirmation
        confirmation = self.spx_confirmer.confirm_signal(
            distribution=distribution,
            price_low=price_low,
            price_high=price_high,
            our_prob=our_prob,
            market_price_cents=ref_price,
        )

        self.spx_confirmer.print_vote_details(confirmation)

        if confirmation["verdict"] == "REJECT":
            return None

        # Step 5: Build signal
        bracket_desc = f"{price_low:.0f}-{price_high:.0f}" if price_high < 99999 else f"{price_low:.0f}+"

        if edge > 0:
            signal = {
                "signal": "buy_yes",
                "side": "yes",
                "edge": edge,
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": market.get("yes_ask", 0) or ref_price,
                "confirmation_multiplier": confirmation["size_multiplier"],
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["mean"],
                "reasoning": (
                    f"[SP500] {target_date}: "
                    f"VIX-implied says {our_prob:.0%} for bracket {bracket_desc}, "
                    f"market at {market_prob:.0%} ({ref_price}c). "
                    f"Edge: +{edge:.0%}. "
                    f"VIX={distribution['vix']:.1f}, vol=+/-{distribution['std_dev']:.0f}pts. "
                    f"Confirmed: {confirmation['verdict']}."
                ),
                "strategy": "S3-SP500",
                "market_type": "sp500",
            }
        else:
            no_price = market.get("no_ask", 0) or (100 - ref_price)
            signal = {
                "signal": "buy_no",
                "side": "no",
                "edge": abs(edge),
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": no_price,
                "confirmation_multiplier": confirmation["size_multiplier"],
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["mean"],
                "reasoning": (
                    f"[SP500] {target_date}: "
                    f"VIX-implied says {our_prob:.0%} for bracket {bracket_desc}, "
                    f"market at {market_prob:.0%} ({ref_price}c). "
                    f"Edge: {edge:.0%} (OVERPRICED). "
                    f"VIX={distribution['vix']:.1f}. "
                    f"Confirmed: {confirmation['verdict']}."
                ),
                "strategy": "S3-SP500",
                "market_type": "sp500",
            }

        return signal

    # ═══════════════════════════════════════════════════════
    # CONFIRMED OUTCOME DETECTION
    # ═══════════════════════════════════════════════════════

    def _check_confirmed_outcome(self, market, city_code, temp_low, temp_high, target_date, ref_price):
        """
        Check if NWS observations already confirm the outcome is determined.

        Cases where we can take max position:
        1. Today's observed high ALREADY EXCEEDS the bucket upper bound
           → NO on this bucket is near-guaranteed (high won't un-happen)
        2. Today's observed high ALREADY EXCEEDS the bucket lower bound
           AND we're late in the day → YES on higher buckets is dying

        Returns a max-sized signal or None if outcome is not yet confirmed.
        """
        # Only check for today's markets (intraday observations)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if target_date != today:
            return None

        todays_high = self.intel.get_todays_high_so_far(city_code)
        if todays_high is None:
            return None

        ticker = market.get("ticker", "")

        # CASE 1: High already ABOVE the bucket's upper bound
        # If observed high is 86°F and bucket is 83-84°F,
        # the daily high will be >= 86 (it can only go up), so NO on 83-84 wins.
        if todays_high > temp_high:
            # NO side is near-guaranteed — the high already exceeded this bucket
            no_price = market.get("no_ask", 0) or (100 - ref_price)
            if no_price <= 0 or no_price >= 95:
                return None  # No reasonable ask, or already priced in

            edge = (100 - no_price) / 100.0  # Guaranteed profit per dollar
            if edge < 0.05:
                return None  # Not enough margin to bother

            # Confirmed outcome: 25% of bankroll (larger than normal trades)
            bankroll_cents = getattr(self, 'balance_cents', None) or config.MAX_TOTAL_EXPOSURE_CENTS
            max_bet_cents = int(bankroll_cents * config.CONFIRMED_OUTCOME_POSITION_PCT)
            contracts = max(1, int(max_bet_cents / no_price))

            print(f"    [CONFIRMED] {city_code} high already {todays_high}°F > bucket {temp_low}-{temp_high}°F")
            print(f"    [CONFIRMED] NO @ {no_price}¢ is near-guaranteed → MAX POSITION {contracts} contracts")

            return {
                "signal": "buy_no",
                "side": "no",
                "edge": edge,
                "confidence": 0.99,
                "price_cents": no_price,
                "confirmation_multiplier": 1.0,  # Already at max via contract count
                "confirmation_verdict": "CONFIRMED_OUTCOME",
                "predicted_high": todays_high,
                "suggested_contracts": contracts,
                "ticker": ticker,
                "order_type": "limit",
                "quality_score": 1.0,
                "seasonal_regime": "confirmed",
                "seasonal_multiplier": 1.0,
                "reasoning": (
                    f"[CONFIRMED] {city_code}: NWS observed high {todays_high}°F already exceeds "
                    f"bucket {temp_low}-{temp_high}°F. NO @ {no_price}¢ is near-guaranteed. "
                    f"Max position: {contracts} contracts."
                ),
                "strategy": "S1-Weather",
            }

        # CASE 2: High already BELOW the bucket's lower bound AND it's late afternoon
        # If it's past 3 PM, temps are falling, and high hasn't reached the bucket
        utc_now = datetime.now(timezone.utc)
        city_info = CITIES.get(city_code, {})
        tz_name = city_info.get("timezone", "America/New_York")
        offset = -6 if ("Chicago" in tz_name or "Central" in tz_name) else -5
        local_hour = (utc_now.hour + offset) % 24

        if local_hour >= 16 and todays_high < temp_low - 2:
            # After 4 PM and high never reached the bucket → NO side wins
            no_price = market.get("no_ask", 0) or (100 - ref_price)
            if no_price <= 0 or no_price >= 95:
                return None

            edge = (100 - no_price) / 100.0
            if edge < 0.05:
                return None

            bankroll_cents = getattr(self, 'balance_cents', None) or config.MAX_TOTAL_EXPOSURE_CENTS
            max_bet_cents = int(bankroll_cents * config.CONFIRMED_OUTCOME_POSITION_PCT)
            contracts = max(1, int(max_bet_cents / no_price))

            print(f"    [CONFIRMED] {city_code} high only {todays_high}°F at {local_hour}:00, "
                  f"bucket {temp_low}-{temp_high}°F unreachable")
            print(f"    [CONFIRMED] NO @ {no_price}¢ → MAX POSITION {contracts} contracts")

            return {
                "signal": "buy_no",
                "side": "no",
                "edge": edge,
                "confidence": 0.95,
                "price_cents": no_price,
                "confirmation_multiplier": 1.0,
                "confirmation_verdict": "CONFIRMED_OUTCOME",
                "predicted_high": todays_high,
                "suggested_contracts": contracts,
                "ticker": ticker,
                "order_type": "limit",
                "quality_score": 1.0,
                "seasonal_regime": "confirmed",
                "seasonal_multiplier": 1.0,
                "reasoning": (
                    f"[CONFIRMED] {city_code}: {local_hour}:00 local, high only {todays_high}°F, "
                    f"bucket {temp_low}-{temp_high}°F unreachable. NO @ {no_price}¢. "
                    f"Max position: {contracts} contracts."
                ),
                "strategy": "S1-Weather",
            }

        return None

    # ═══════════════════════════════════════════════════════
    # TIME-BASED EDGE THRESHOLDS
    # ═══════════════════════════════════════════════════════

    def _get_et_hour(self):
        """Get approximate current hour in Eastern Time."""
        utc_now = datetime.now(timezone.utc)
        return (utc_now.hour - 5) % 24

    def _get_edge_period(self):
        """Return human-readable label for current edge period."""
        h = self._get_et_hour()
        if h < 6:
            return "overnight"
        elif h < 12:
            return "morning"
        elif h < 16:
            return "afternoon"
        else:
            return "evening"

    def _get_time_adjusted_edge_threshold(self, signal):
        """
        Require higher edge in the morning to preserve capital for
        afternoon confirmed outcomes and arbitrage.

        Confirmed outcomes and arbitrage bypass this entirely.
        Normal forecast-based trades must clear a higher bar early.

        MORNING  (6 AM-12 PM ET): 12% — be selective, save capital
        AFTERNOON (12-4 PM ET):    8% — confirmed outcomes start appearing
        EVENING  (4 PM+ ET):       8% — arbitrage and confirmed outcomes
        OVERNIGHT (12-6 AM ET):   10% — good models but thin liquidity
        """
        # Arbitrage and confirmed outcomes always pass at base threshold
        strategy = signal.get("strategy", "")
        if strategy == "S2-Arbitrage":
            return config.MIN_EDGE
        if signal.get("confirmation_verdict") == "CONFIRMED_OUTCOME":
            return 0.0  # Always take confirmed outcomes

        h = self._get_et_hour()
        if h < 6:
            return 0.10   # Overnight: 10%
        elif h < 12:
            return 0.12   # Morning: 12% — be selective
        else:
            return config.MIN_EDGE  # Afternoon/evening: base 8%

    # ═══════════════════════════════════════════════════════
    # POSITION SIZING (Quarter-Kelly)
    # ═══════════════════════════════════════════════════════

    def _kelly_size(self, edge, confidence, price_cents):
        """
        Quarter-Kelly position sizing.
        Accounts for the binary nature of prediction markets.

        Uses actual Kalshi balance when available for dynamic sizing.
        Per-position cap = MAX_POSITION_PCT (20%) of bankroll.
        No hard contract count cap — size is governed by percentage of capital.
        """
        if edge <= 0 or confidence <= 0 or price_cents <= 0:
            return 0

        prob_win = min(0.95, (price_cents / 100.0) + edge)
        payout = 100 - price_cents  # Profit if correct
        loss = price_cents  # Loss if wrong

        if loss == 0:
            return 0

        kelly = ((prob_win * payout) - ((1 - prob_win) * loss)) / payout
        quarter_kelly = kelly / 4.0

        if quarter_kelly <= 0:
            return 0

        # Use actual balance if available, fall back to exposure cap
        bankroll_cents = getattr(self, 'balance_cents', None) or config.MAX_TOTAL_EXPOSURE_CENTS
        bet_cents = quarter_kelly * bankroll_cents

        # Cap at per-position max (20% of bankroll) and auto-trade limit
        max_per_position = int(bankroll_cents * config.MAX_POSITION_PCT)
        bet_cents = min(bet_cents, max_per_position, config.MAX_AUTO_TRADE_CENTS)

        contracts = max(1, int(bet_cents / price_cents))
        return contracts

    # ═══════════════════════════════════════════════════════
    # UTILITY
    # ═══════════════════════════════════════════════════════

    def _skip(self, reason):
        return {
            "signal": "skip", "edge": 0, "confidence": 0,
            "reasoning": reason, "suggested_contracts": 0,
            "price_cents": 0, "side": "", "ticker": "",
            "strategy": "none", "confirmation_multiplier": 1.0,
        }

    def get_strategy_summary(self):
        # Build active market list
        active = [k for k, v in config.MARKET_TYPES.items() if v]
        lines = [
            "",
            "  Active Strategies:",
            "  ═══════════════════════════════════════════════════════════",
            f"  Markets: {', '.join(active).upper()}",
            "",
        ]

        if config.MARKET_TYPES.get("weather"):
            lines += [
                "  S1: WEATHER ENSEMBLE EDGE (primary)",
                "      → 143 ensemble members (GFS+ECMWF+ICON+GEM)",
                "      → 4-source confirmation voting before every trade",
                "      → Statistical significance test (z-test, p<0.10)",
                "      → Regime detection (stable/transitional/volatile)",
                "      → Seasonal confidence sizing (city+month+anomaly)",
                "      → Bias correction (learns from past accuracy)",
                "      → Quarter-Kelly x all multipliers",
                "      → Limit orders only (maker strategy)",
                "",
            ]

        lines += [
            "  S2: SPREAD ARBITRAGE (secondary)",
            "      → Detects YES+NO < 98c (guaranteed profit)",
            "      → Auto-executes when found",
            "",
        ]

        if config.MARKET_TYPES.get("sp500"):
            lines += [
                "  S3: S&P 500 VIX-IMPLIED BRACKETS",
                "      → VIX-based normal distribution for price probabilities",
                "      → 3-check confirmation (momentum, vol ratio, historical)",
                "      → Intraday vol adjustment as market day progresses",
                "      → yfinance data (free, no API key)",
                "      → Quarter-Kelly sizing",
                "",
            ]

        lines += [
            "  Intelligence Layer:",
            "      → Exit strategy (take profit / cut losses / edge reversal)",
            "      → Settlement P&L tracking with bias learning",
            "      → Dynamic model weighting per city/season",
            "  ═══════════════════════════════════════════════════════════",
            "",
        ]
        return "\n".join(lines)
