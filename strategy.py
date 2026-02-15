"""
STRATEGY ENGINE v3.0 — Weather-Focused + Arbitrage
======================================================
Primary: Weather ensemble edge (S4 from v2, massively upgraded)
Secondary: Spread arbitrage (S2 from v2, risk-free)

Decision flow (from your friend's design, adapted for Kalshi):
  1. Scan Kalshi for weather markets
  2. Fetch 143 ensemble forecasts → build probability distribution
  3. Compare distribution vs market prices → detect mispricing (≥8%)
  4. Get second opinions from 4 independent sources
  5. Risk check (daily loss, exposure, streaks)
  6. Size trade with Quarter-Kelly × confirmation multiplier
  7. Execute as LIMIT order (maker strategy)
"""

import math
from datetime import datetime, timezone
from weather_engine import WeatherEngine, CITIES
from signal_confirmer import SignalConfirmer
from trade_intelligence import TradeIntelligence
from quant_analytics import QuantAnalytics
from market_quality import MarketQualityFilter
import config


class Strategy:
    """
    Multi-strategy evaluation engine.
    Weather is primary, arbitrage is secondary.
    """

    def __init__(self, kalshi_client=None):
        self.client = kalshi_client
        self.weather = WeatherEngine()
        self.confirmer = SignalConfirmer()
        self.intel = TradeIntelligence(kalshi_client, self.weather)
        self.quant = QuantAnalytics(self.weather)
        self.quality = MarketQualityFilter()

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

        # Return best signal
        if not signals:
            return self._skip(f"No signals for {ticker}")

        best = max(signals, key=lambda s: s["edge"] * s["confidence"])

        if best["edge"] < config.MIN_EDGE:
            return self._skip(f"Best edge {best['edge']:.1%} below {config.MIN_EDGE:.0%} threshold")

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
        except Exception:
            pass  # Don't let correlation check block trades

        best["suggested_contracts"] = contracts
        best["ticker"] = ticker
        best["order_type"] = "limit"  # Always maker orders
        best["quality_score"] = quality_score

        # Adjust size for market quality
        if quality_score < 0.9:
            contracts = self.quality.adjust_contracts_for_quality(contracts, market, quality_score)
            best["suggested_contracts"] = contracts

        # Liquidity-adjusted sizing: cap contracts for low open_interest markets
        oi = market.get("open_interest", 0) or 0
        if oi < config.LIQUIDITY_TIER_1_OI:
            best["suggested_contracts"] = min(best["suggested_contracts"], 1)
        elif oi < config.LIQUIDITY_TIER_2_OI:
            best["suggested_contracts"] = min(best["suggested_contracts"], 2)

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

        # Step 4d: Smart order pricing
        smart_price = self.quant.calculate_optimal_price(market, "yes" if edge > 0 else "no", abs(edge))

        # Step 5: Build signal
        if edge > 0:
            # Underpriced YES — buy YES
            price = smart_price if smart_price else (market.get("yes_ask", 0) or ref_price)
            signal = {
                "signal": "buy_yes",
                "side": "yes",
                "edge": edge,
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": price,
                "confirmation_multiplier": confirmation["size_multiplier"] * time_mult * regime_mult,
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["forecasted_high_mean"],
                "reasoning": (
                    f"[WEATHER] {city_code} {target_date}: "
                    f"Ensemble says {our_prob:.0%} for {temp_low}-{temp_high}°F, "
                    f"market at {market_prob:.0%} ({ref_price}¢). "
                    f"Edge: +{edge:.0%}. "
                    f"Confirmed: {confirmation['verdict']} ({confirmation['agree_count']} agree). "
                    f"{distribution['total_members']} members. "
                    f"Time: {time_reason} ({time_mult}x). "
                    + (f"Bias adj: {bias_applied:+.1f}°F. " if bias_applied else "")
                ),
                "strategy": "S1-Weather",
            }
        else:
            # Overpriced YES — buy NO
            no_price = smart_price if smart_price else (market.get("no_ask", 0) or (100 - ref_price))
            signal = {
                "signal": "buy_no",
                "side": "no",
                "edge": abs(edge),
                "confidence": min(0.75, distribution["confidence"] * 0.8 + abs(edge)),
                "price_cents": no_price,
                "confirmation_multiplier": confirmation["size_multiplier"] * time_mult * regime_mult,
                "confirmation_verdict": confirmation["verdict"],
                "predicted_high": distribution["forecasted_high_mean"],
                "reasoning": (
                    f"[WEATHER] {city_code} {target_date}: "
                    f"Ensemble says {our_prob:.0%} for {temp_low}-{temp_high}°F, "
                    f"market at {market_prob:.0%} ({ref_price}¢). "
                    f"Edge: {edge:.0%} (OVERPRICED). "
                    f"Confirmed: {confirmation['verdict']}. "
                    f"{distribution['total_members']} members. "
                    f"Time: {time_reason} ({time_mult}x). "
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
    # POSITION SIZING (Quarter-Kelly)
    # ═══════════════════════════════════════════════════════

    def _kelly_size(self, edge, confidence, price_cents):
        """
        Quarter-Kelly position sizing.
        Accounts for the binary nature of prediction markets.
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

        bankroll_cents = config.MAX_TOTAL_EXPOSURE_CENTS
        bet_cents = quarter_kelly * bankroll_cents
        bet_cents = min(bet_cents, config.MAX_AUTO_TRADE_CENTS)

        contracts = max(1, int(bet_cents / price_cents))
        return min(contracts, 15)

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
        return """
  Active Strategies:
  ═══════════════════════════════════════════════════════════
  S1: WEATHER ENSEMBLE EDGE (primary)
      → 143 ensemble members (GFS+ECMWF+ICON+GEM)
      → 4-source confirmation voting before every trade
      → Statistical significance test (z-test, p<0.10)
      → Regime detection (stable/transitional/volatile)
      → Bias correction (learns from past accuracy)
      → Time-of-day sizing (1.3x morning → 0.4x evening)
      → Smart order placement (bid inside spread)
      → Correlation-aware sizing (avoid concentration)
      → Quarter-Kelly × all multipliers
      → Limit orders only (maker strategy)

  S2: SPREAD ARBITRAGE (secondary)
      → Detects YES+NO < 98¢ (guaranteed profit)
      → No confirmation needed (pure math)
      → Auto-executes when found

  Intelligence Layer:
      → Exit strategy (take profit / cut losses / edge reversal)
      → Intraday temperature tracking (NWS live observations)
      → Settlement P&L tracking with bias learning
      → Backtester for strategy validation
      → Dynamic model weighting per city/season
  ═══════════════════════════════════════════════════════════
"""
