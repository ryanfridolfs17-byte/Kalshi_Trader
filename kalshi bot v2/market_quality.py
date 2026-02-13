"""
MARKET QUALITY FILTERS v3.3
====================================
Two critical filters that prevent the most common ways bots lose money:

  1. LIQUIDITY FILTER — Prevents trading in illiquid markets where:
     - Wide bid-ask spreads trap you in positions you can't exit
     - Apparent "edges" are just stale/phantom quotes
     - Order sizes exceed available depth (slippage)
     - "Arbitrage" opportunities are mirages

  2. PROBABILITY GUARDRAILS — Prevents longshot seduction where:
     - Cheap contracts (<10¢) look like huge returns but lose 60%+
     - The bot over-allocates to low-probability events
     - Position sizing doesn't account for probability of total loss

ACADEMIC BASIS:
  Whelan & Karl (2025): "Investors who buy contracts costing less
  than 10c lose over 60 percent of their money."
"""

import config


class MarketQualityFilter:
    """
    Pre-screens every market BEFORE strategy evaluation.
    If a market fails quality checks, it never reaches the strategy engine.
    """

    def __init__(self):
        # ─── LIQUIDITY THRESHOLDS ───
        # Note: Kalshi weather markets are inherently low-volume.
        # Thresholds are set for weather-specific trading, not broad markets.
        self.min_volume_24h = 1          # At least 1 trade in 24h (any activity)

        # Minimum total volume (lifetime)
        self.min_volume_total = 1         # At least 1 trade ever

        # Maximum bid-ask spread in cents
        # Wider than this = illiquid, likely can't exit
        self.max_spread_cents = 15

        # Minimum open interest (contracts currently held by traders)
        self.min_open_interest = 20

        # Maximum percentage of open interest our order would represent
        # If we'd be >20% of all open interest, we're too big for this market
        self.max_position_pct_of_oi = 0.20

        # Minimum number of distinct price levels in orderbook
        self.min_orderbook_depth = 3

        # ─── PROBABILITY GUARDRAILS ───
        # NEVER buy contracts priced below this (longshot death zone)
        # Academic research: <10¢ contracts lose 60%+ of money
        self.min_price_cents = 12

        # NEVER buy contracts priced above this (paying too much for certainty)
        # 93¢+ contracts: you risk 93¢ to make 7¢, terrible risk/reward
        self.max_price_cents = 88

        # Maximum contracts at very low prices (12-20¢ range)
        # Even in the "allowed" zone, limit exposure to low-prob bets
        self.max_lowprob_contracts = 2

        # Maximum total cost on any single low-probability trade
        self.max_lowprob_cost_cents = 50  # $0.50

    def check_market(self, market, proposed_contracts=1):
        """
        Run all quality checks on a market.

        Returns:
            (passed: bool, reason: str, quality_score: float 0-1)

        Quality score is used to adjust position sizing:
        - 0.9-1.0: Excellent liquidity, trade full size
        - 0.7-0.9: Good liquidity, trade normally
        - 0.5-0.7: Marginal, reduce size by 50%
        - Below 0.5: Don't trade
        """
        issues = []
        score = 1.0

        # Extract market data
        yes_bid = market.get("yes_bid", 0) or 0
        yes_ask = market.get("yes_ask", 0) or 0
        no_bid = market.get("no_bid", 0) or 0
        no_ask = market.get("no_ask", 0) or 0
        volume = market.get("volume", 0) or 0
        volume_24h = market.get("volume_24h", 0) or 0
        open_interest = market.get("open_interest", 0) or 0
        last_price = market.get("last_price", 0) or 0
        spread = (yes_ask - yes_bid) if (yes_ask > 0 and yes_bid > 0) else 99

        ref_price = yes_ask if yes_ask > 0 else last_price

        # ═══════════════════════════════════════════════════
        # LIQUIDITY CHECKS
        # ═══════════════════════════════════════════════════

        # Check 1: Volume (24h) — any activity at all?
        if volume_24h < self.min_volume_24h:
            if volume_24h == 0 and volume == 0:
                return False, f"Zero volume — no activity ever", 0.0
            # Low but nonzero — penalize, don't reject
            score -= 0.15
            issues.append(f"Low 24h volume ({volume_24h})")

        # Check 2: Total volume
        if volume < self.min_volume_total:
            if volume == 0:
                return False, f"Zero lifetime volume", 0.0
            score -= 0.1
            issues.append(f"Low total volume ({volume})")

        # Check 3: Bid-ask spread
        # IMPORTANT: Kalshi orderbooks only show bids (not asks).
        # yes_bid=0 just means no YES bids, not that the market is dead.
        # A spread of 99 (from yes_bid=0) should NOT auto-reject.
        # Only reject if we can confirm BOTH sides have quotes and they're far apart.
        if yes_bid > 0 and yes_ask > 0:
            # We have both sides — check real spread
            if spread > self.max_spread_cents:
                if spread > 30:
                    return False, f"Spread {spread}¢ is extreme — phantom quotes likely", 0.0
                score -= 0.3
                issues.append(f"Wide spread ({spread}¢)")
            elif spread > 10:
                score -= 0.1
                issues.append(f"Moderate spread ({spread}¢)")
        # If yes_bid=0, we can't calculate a real spread — skip this check

        # Check 4: At least one side of the book has a real price
        if yes_ask == 0 and no_ask == 0 and last_price == 0:
            return False, "No prices at all — completely empty market", 0.0

        # Check 5: Open interest
        if open_interest < self.min_open_interest:
            score -= 0.15
            issues.append(f"Low open interest ({open_interest})")

        # Check 6: Our order vs market size
        if open_interest > 0:
            our_pct = proposed_contracts / open_interest
            if our_pct > self.max_position_pct_of_oi:
                if proposed_contracts > 5 and our_pct > 0.5:
                    # Only hard-reject if we're placing a large order
                    return False, f"Order ({proposed_contracts}) would be {our_pct:.0%} of all open interest — market too thin", 0.0
                elif our_pct > 0.5:
                    score -= 0.15
                    issues.append(f"Order is {our_pct:.0%} of open interest")

        # Check 7: Stale market detection
        # If bid and ask haven't changed and volume is 0, market is stale
        prev_price = market.get("previous_price", 0) or 0
        if prev_price > 0 and prev_price == last_price and volume_24h < 10:
            score -= 0.2
            issues.append("Price unchanged with near-zero volume — possibly stale")

        # Check 8: Arbitrage reality check
        # If YES+NO spread seems too good, verify it's not phantom
        if yes_ask > 0 and no_ask > 0:
            total = yes_ask + no_ask
            if total < 90 and volume_24h == 0 and volume == 0:
                return False, (f"Apparent arb (YES {yes_ask}¢ + NO {no_ask}¢ = {total}¢) "
                              f"but zero volume — likely phantom quotes"), 0.0

        # ═══════════════════════════════════════════════════
        # PROBABILITY GUARDRAILS
        # ═══════════════════════════════════════════════════

        # Check 9: Longshot death zone
        if ref_price < self.min_price_cents:
            return False, (f"Price {ref_price}¢ is in the longshot death zone (<{self.min_price_cents}¢). "
                          f"Research shows these contracts lose 60%+ of money invested."), 0.0

        # Check 10: Overpaying for certainty
        if ref_price > self.max_price_cents:
            return False, (f"Price {ref_price}¢ is too expensive (>{self.max_price_cents}¢). "
                          f"Risking {ref_price}¢ to make {100-ref_price}¢ — terrible risk/reward."), 0.0

        # Check 11: Low-probability position limits
        if ref_price < 20:
            if proposed_contracts > self.max_lowprob_contracts:
                issues.append(f"Low-prob market: capping at {self.max_lowprob_contracts} contracts")
            total_cost = ref_price * proposed_contracts
            if total_cost > self.max_lowprob_cost_cents:
                issues.append(f"Low-prob market: capping cost at ${self.max_lowprob_cost_cents/100:.2f}")

        # Check 12: Expected value sanity check
        # Even if we have an "edge", if the contract is very cheap,
        # the fee structure might eat the edge
        # Kalshi fees: up to 2¢ per contract
        fee_per_contract = 2  # cents
        if ref_price < 15:
            fee_as_pct = fee_per_contract / ref_price
            if fee_as_pct > 0.10:
                score -= 0.15
                issues.append(f"Fees are {fee_as_pct:.0%} of price — edge may not cover costs")

        # ═══════════════════════════════════════════════════
        # FINAL SCORING
        # ═══════════════════════════════════════════════════

        score = max(0.0, min(1.0, score))

        if score < 0.5:
            return False, f"Quality score {score:.2f} — " + "; ".join(issues), score

        if issues:
            reason = f"Quality score {score:.2f} — " + "; ".join(issues)
        else:
            reason = f"Quality score {score:.2f} — All checks passed"

        return True, reason, score

    def adjust_contracts_for_quality(self, contracts, market, quality_score):
        """
        Reduce position size based on market quality.
        Also enforces low-probability position limits.
        """
        ref_price = (market.get("yes_ask", 0) or market.get("last_price", 0) or 50)

        # Scale by quality
        if quality_score >= 0.9:
            adjusted = contracts  # Full size
        elif quality_score >= 0.7:
            adjusted = contracts  # Normal
        elif quality_score >= 0.5:
            adjusted = max(1, int(contracts * 0.5))  # Half size
        else:
            adjusted = 0  # Don't trade

        # Low-probability caps
        if ref_price < 20:
            adjusted = min(adjusted, self.max_lowprob_contracts)
            max_contracts_by_cost = self.max_lowprob_cost_cents // max(ref_price, 1)
            adjusted = min(adjusted, max_contracts_by_cost)

        return max(1, adjusted) if adjusted > 0 else 0

    def print_filter_summary(self, market, passed, reason, score):
        """Print a concise filter result."""
        ticker = market.get("ticker", "???")
        icon = "✓" if passed else "✗"
        color_word = "PASS" if passed else "FAIL"
        print(f"    [{icon}] {color_word} {ticker}: {reason}")
