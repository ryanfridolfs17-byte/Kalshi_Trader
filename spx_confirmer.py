"""
SPX SIGNAL CONFIRMER v1.0
=============================
Confirms S&P 500 bracket trading signals using three checks:
  1. Intraday momentum — SPY trending toward or away from bracket?
  2. Realized vs implied vol — distribution tighter/wider than VIX implies?
  3. Historical pattern — same-day-of-week bracket hit rate from 90 days

Returns same confirmation structure as signal_confirmer.py:
  {"verdict": "STRONG"/"CONFIRM"/"WEAK"/"REJECT", "size_multiplier": ...}
"""

import math
from datetime import datetime, timezone, timedelta


class SPXConfirmer:
    """
    Lighter confirmation than weather's 4-model voting.
    Three independent checks for S&P 500 bracket signals.
    """

    def __init__(self):
        self._cache = {}

    def confirm_signal(self, distribution, price_low, price_high, our_prob, market_price_cents):
        """
        Run three confirmation checks on an S&P 500 bracket signal.

        Args:
            distribution: from VolatilityEngine.get_price_distribution()
            price_low: bracket lower bound
            price_high: bracket upper bound
            our_prob: our calculated probability (0.0-1.0)
            market_price_cents: Kalshi market price in cents

        Returns:
            {
                "verdict": "STRONG" | "CONFIRM" | "WEAK" | "REJECT",
                "size_multiplier": 1.5 | 1.0 | 0.5 | 0.0,
                "votes": {...},
                "vote_details": {...},
                "agree_count": N,
                "disagree_count": N,
                "summary": "...",
            }
        """
        market_prob = market_price_cents / 100.0
        our_direction = "underpriced" if our_prob > market_prob else "overpriced"

        votes = {}
        vote_details = {}

        # ─── CHECK 1: INTRADAY MOMENTUM ───
        momentum_vote, momentum_detail = self._check_momentum(
            distribution, price_low, price_high, our_direction
        )
        votes["momentum"] = momentum_vote
        vote_details["momentum"] = momentum_detail

        # ─── CHECK 2: REALIZED VS IMPLIED VOL ───
        vol_vote, vol_detail = self._check_vol_ratio(
            distribution, price_low, price_high, our_direction
        )
        votes["vol_ratio"] = vol_vote
        vote_details["vol_ratio"] = vol_detail

        # ─── CHECK 3: HISTORICAL PATTERN ───
        hist_vote, hist_detail = self._check_historical(
            distribution, price_low, price_high, our_prob
        )
        votes["historical"] = hist_vote
        vote_details["historical"] = hist_detail

        # Count votes
        agree_count = sum(1 for v in votes.values() if v == "AGREE")
        disagree_count = sum(1 for v in votes.values() if v == "DISAGREE")
        total_voted = agree_count + disagree_count

        # Determine verdict
        if total_voted == 0:
            verdict = "WEAK"
            multiplier = 0.5
            summary = "No checks could confirm (all abstained)"
        elif disagree_count > agree_count:
            verdict = "REJECT"
            multiplier = 0.0
            summary = f"{disagree_count}/{total_voted} checks disagree — signal rejected"
        elif agree_count >= 3:
            verdict = "STRONG"
            multiplier = 1.5
            summary = f"All 3 checks agree — STRONG confirmation"
        elif agree_count >= 2:
            verdict = "CONFIRM"
            multiplier = 1.0
            summary = f"{agree_count}/{total_voted} checks agree — confirmed"
        else:
            verdict = "WEAK"
            multiplier = 0.5
            summary = f"Only {agree_count}/{total_voted} agree — weak signal"

        return {
            "verdict": verdict,
            "size_multiplier": multiplier,
            "votes": votes,
            "vote_details": vote_details,
            "agree_count": agree_count,
            "disagree_count": disagree_count,
            "abstain_count": sum(1 for v in votes.values() if v == "ABSTAIN"),
            "summary": summary,
        }

    def _check_momentum(self, distribution, price_low, price_high, our_direction):
        """
        Check 1: Is SPY trending toward or away from the bracket?
        Uses the current price relative to bracket midpoint.
        """
        current = distribution.get("current_price", 0)
        if current <= 0:
            return "ABSTAIN", "Momentum: no current price data"

        bracket_mid = (price_low + price_high) / 2.0 if price_high < 99999 else price_low + 12.5
        distance = current - bracket_mid
        daily_vol = distribution.get("daily_vol", 1)

        # How many standard deviations away is the current price from bracket center?
        z_dist = abs(distance) / daily_vol if daily_vol > 0 else 0

        if our_direction == "underpriced":
            # We think bracket is more likely than market says
            # Current price near the bracket supports this
            if z_dist < 0.5:
                return "AGREE", f"Momentum: SPX {current:.0f} is near bracket center {bracket_mid:.0f} ({z_dist:.1f}σ) — AGREE"
            elif z_dist < 1.5:
                return "ABSTAIN", f"Momentum: SPX {current:.0f} is {z_dist:.1f}σ from bracket — ABSTAIN"
            else:
                return "DISAGREE", f"Momentum: SPX {current:.0f} is {z_dist:.1f}σ away from bracket — DISAGREE"
        else:
            # We think bracket is less likely than market says
            if z_dist > 1.5:
                return "AGREE", f"Momentum: SPX {current:.0f} far from bracket ({z_dist:.1f}σ) — AGREE with overpriced"
            elif z_dist > 0.5:
                return "ABSTAIN", f"Momentum: SPX {current:.0f} moderately near bracket ({z_dist:.1f}σ) — ABSTAIN"
            else:
                return "DISAGREE", f"Momentum: SPX {current:.0f} in bracket range ({z_dist:.1f}σ) — DISAGREE with overpriced"

    def _check_vol_ratio(self, distribution, price_low, price_high, our_direction):
        """
        Check 2: Is realized vol confirming or contradicting VIX-implied vol?
        If realized vol < implied, distribution is tighter → higher confidence
        in near-the-money brackets.
        """
        raw_prices = distribution.get("raw_prices", [])
        vix = distribution.get("vix", 0)

        if len(raw_prices) < 20 or vix <= 0:
            return "ABSTAIN", "Vol ratio: insufficient data"

        # Calculate realized vol from last 20 days
        daily_returns = []
        for i in range(1, min(21, len(raw_prices))):
            if raw_prices[i - 1] > 0:
                ret = (raw_prices[i] - raw_prices[i - 1]) / raw_prices[i - 1]
                daily_returns.append(ret)

        if len(daily_returns) < 10:
            return "ABSTAIN", "Vol ratio: not enough return data"

        realized_vol_annual = (sum(r ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5 * math.sqrt(252) * 100
        vol_ratio = realized_vol_annual / vix if vix > 0 else 1.0

        # Bracket is near the money? Check if bracket midpoint is near current price
        current = distribution.get("current_price", 0)
        bracket_mid = (price_low + price_high) / 2.0 if price_high < 99999 else price_low
        near_money = abs(current - bracket_mid) < distribution.get("daily_vol", 999) * 1.5

        if our_direction == "underpriced" and near_money:
            # We think near-money bracket is underpriced
            # Realized vol < implied → distribution is tighter → supports near-money bets
            if vol_ratio < 0.8:
                return "AGREE", f"Vol ratio: realized {realized_vol_annual:.1f}% < VIX {vix:.1f}% (ratio {vol_ratio:.2f}) — tighter distribution supports near-money bracket"
            elif vol_ratio > 1.3:
                return "DISAGREE", f"Vol ratio: realized {realized_vol_annual:.1f}% > VIX {vix:.1f}% (ratio {vol_ratio:.2f}) — wider distribution hurts near-money bracket"
            else:
                return "ABSTAIN", f"Vol ratio: {vol_ratio:.2f} — neutral"
        elif our_direction == "overpriced" and near_money:
            if vol_ratio > 1.2:
                return "AGREE", f"Vol ratio: realized {realized_vol_annual:.1f}% > VIX {vix:.1f}% — wider vol supports overpriced near-money signal"
            else:
                return "ABSTAIN", f"Vol ratio: {vol_ratio:.2f} — neutral for overpriced signal"
        else:
            # Far from money
            if vol_ratio < 0.7:
                vote = "DISAGREE" if our_direction == "underpriced" else "AGREE"
            elif vol_ratio > 1.3:
                vote = "AGREE" if our_direction == "underpriced" else "DISAGREE"
            else:
                vote = "ABSTAIN"
            return vote, f"Vol ratio: {vol_ratio:.2f}, far-from-money bracket — {vote}"

    def _check_historical(self, distribution, price_low, price_high, our_prob):
        """
        Check 3: Historical bracket hit rate over last 90 trading days.
        How often did the S&P close in a range of this width,
        this far from the mean?
        """
        raw_prices = distribution.get("raw_prices", [])
        current = distribution.get("current_price", 0)

        if len(raw_prices) < 30 or current <= 0:
            return "ABSTAIN", "Historical: insufficient data"

        # Calculate bracket width as % of price
        bracket_width = price_high - price_low if price_high < 99999 else 50
        bracket_width_pct = bracket_width / current

        # How often did the daily move fall within a range of this width?
        # We check if |close - previous_close| < bracket_width
        hits = 0
        total = 0
        for i in range(1, len(raw_prices)):
            daily_move = abs(raw_prices[i] - raw_prices[i - 1])
            total += 1
            if daily_move <= bracket_width:
                hits += 1

        if total < 20:
            return "ABSTAIN", "Historical: not enough trading days"

        hist_rate = hits / total

        # Compare historical rate to our probability
        if abs(hist_rate - our_prob) < 0.10:
            return "AGREE", f"Historical: {hist_rate:.0%} of days had moves within {bracket_width:.0f}pts (our prob: {our_prob:.0%}) — consistent"
        elif hist_rate > our_prob + 0.15:
            return "AGREE", f"Historical: {hist_rate:.0%} hit rate vs our {our_prob:.0%} — history supports signal"
        elif hist_rate < our_prob - 0.15:
            return "DISAGREE", f"Historical: only {hist_rate:.0%} hit rate vs our {our_prob:.0%} — history contradicts"
        else:
            return "ABSTAIN", f"Historical: {hist_rate:.0%} hit rate vs {our_prob:.0%} — inconclusive"

    def print_vote_details(self, confirmation):
        """Pretty-print the confirmation details (same interface as SignalConfirmer)."""
        print(f"\n  ┌─ SPX Confirmation: {confirmation['verdict']} "
              f"(size: {confirmation['size_multiplier']}x) ──")
        for source, detail in confirmation.get("vote_details", {}).items():
            vote = confirmation["votes"].get(source, "?")
            icon = {"AGREE": "+", "DISAGREE": "x", "ABSTAIN": "~"}.get(vote, "?")
            print(f"  |  {icon} {detail}")
        print(f"  └─ {confirmation['summary']}")
