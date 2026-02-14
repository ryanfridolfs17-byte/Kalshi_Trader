"""
Unit tests for the Strategy class — evaluate_market() behaviour.

The Strategy class pulls in many heavy dependencies (SignalConfirmer,
TradeIntelligence, QuantAnalytics, MarketQualityFilter, config, etc.).
We mock the modules that would otherwise require network access or
complex setup so we can unit-test the evaluation logic in isolation.

Run with:
    python -m unittest tests/test_strategy.py
    python -m pytest  tests/test_strategy.py
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Allow imports from the src directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# ── Stub out heavy dependencies before importing strategy ──
# We need these modules to exist so that ``import strategy`` succeeds,
# but we do not want them to make network calls or load real state.

# signal_confirmer
_signal_confirmer_mod = types.ModuleType("signal_confirmer")


class _FakeConfirmer:
    def confirm_signal(self, **kwargs):
        return {
            "verdict": "CONFIRM",
            "agree_count": 3,
            "disagree_count": 0,
            "size_multiplier": 1.0,
            "votes": [],
        }

    def print_vote_details(self, confirmation):
        pass


_signal_confirmer_mod.SignalConfirmer = _FakeConfirmer
sys.modules["signal_confirmer"] = _signal_confirmer_mod

# trade_intelligence
_trade_intel_mod = types.ModuleType("trade_intelligence")


class _FakeIntel:
    def __init__(self, *a, **kw):
        pass

    def apply_bias_to_distribution(self, dist, city):
        dist["bias_applied"] = 0
        return dist

    def get_time_multiplier(self, city):
        return 1.0, "unit-test"


_trade_intel_mod.TradeIntelligence = _FakeIntel
sys.modules["trade_intelligence"] = _trade_intel_mod

# quant_analytics
_quant_mod = types.ModuleType("quant_analytics")


class _FakeQuant:
    def __init__(self, *a, **kw):
        pass

    def validate_edge_significance(self, our_prob, market_prob, n):
        return {"significant": True, "reason": "ok"}

    def detect_regime(self, dist):
        return {"regime": "stable", "size_multiplier": 1.0, "reason": "unit-test"}

    def calculate_optimal_price(self, market, side, edge):
        return None

    def adjust_for_correlation(self, signal, positions):
        return 1.0


_quant_mod.QuantAnalytics = _FakeQuant
sys.modules["quant_analytics"] = _quant_mod

# market_quality — we use the real implementation since it lives in src/
# and has no network dependencies, but it is imported by strategy
# It is already importable via the sys.path we added above.

# risk_manager (imported lazily inside evaluate_market)
_risk_mod = types.ModuleType("risk_manager")


class _FakeRisk:
    def __init__(self, *a, **kw):
        self.state = {"positions": []}


_risk_mod.RiskManager = _FakeRisk
sys.modules["risk_manager"] = _risk_mod

# Now import the modules under test
from strategy import Strategy  # noqa: E402
from weather_engine import WeatherEngine  # noqa: E402


# ── Helpers ──

def _make_weather_market(
    yes_bid=30,
    yes_ask=35,
    no_bid=60,
    no_ask=65,
    volume=50,
    volume_24h=20,
    open_interest=100,
    ticker="KXHIGHNY-26FEB14-T35T39",
    event_ticker="KXHIGHNY-26FEB14",
    title="High temperature between 35\u00b0F to 39\u00b0F",
):
    """Build a realistic weather market dict for testing."""
    return {
        "ticker": ticker,
        "title": title,
        "subtitle": "",
        "event_ticker": event_ticker,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last_price": yes_ask,
        "previous_price": yes_ask - 2,
        "volume": volume,
        "volume_24h": volume_24h,
        "open_interest": open_interest,
        "status": "open",
    }


class TestEvaluateMarketReturnsDict(unittest.TestCase):
    """evaluate_market() should always return a dict with the expected keys."""

    def setUp(self):
        self.strategy = Strategy(kalshi_client=None)

    def test_evaluate_market_returns_dict(self):
        """Even for a skipped market, the return value must be a dict with
        at least the keys: signal, edge, confidence, reasoning.
        """
        market = _make_weather_market()
        result = self.strategy.evaluate_market(market)

        self.assertIsInstance(result, dict)
        for key in ("signal", "edge", "confidence", "reasoning"):
            self.assertIn(key, result, f"Missing key '{key}' in evaluate_market result")

    def test_evaluate_market_skip_is_dict(self):
        """A market with extreme prices (99c) should be fast-rejected as a skip dict."""
        market = _make_weather_market(yes_ask=99, yes_bid=98, no_bid=0, no_ask=1)
        result = self.strategy.evaluate_market(market)

        self.assertIsInstance(result, dict)
        self.assertEqual(result["signal"], "skip")
        self.assertEqual(result["edge"], 0)


class TestEvaluateMarketNoEdge(unittest.TestCase):
    """Markets where the ensemble probability matches the market price
    (i.e. no edge) should result in a skip."""

    def setUp(self):
        self.strategy = Strategy(kalshi_client=None)

    def test_evaluate_market_no_edge(self):
        """When the weather engine returns a probability that matches the
        market price (e.g. both ~35%), no signal should be generated.
        The result should be a skip dict.
        """
        market = _make_weather_market(yes_ask=35, yes_bid=30)

        # Patch the WeatherEngine to return a distribution where the bucket
        # probability nearly equals the market price (35/100 = 0.35).
        fake_dist = {
            "city": "NYC",
            "target_date": "2026-02-14",
            "forecasted_high_mean": 37.0,
            "forecasted_high_median": 37.0,
            "forecasted_high_min": 35.0,
            "forecasted_high_max": 39.0,
            "spread": 4.0,
            "std_dev": 1.5,
            "total_members": 100,
            "bucket_probs_5f": {"35-39": 0.35},
            "bucket_probs_2f": {},
            "raw_highs": [37] * 35 + [42] * 65,  # 35 in range, 65 out
            "sources_used": ["gfs_ensemble"],
            "confidence": 0.85,
            "bias_applied": 0,
        }

        with patch.object(
            self.strategy.weather,
            "get_temperature_distribution",
            return_value=fake_dist,
        ):
            result = self.strategy.evaluate_market(market)

        self.assertIsInstance(result, dict)
        # Edge is ~0% (0.35 - 0.35 = 0), below MIN_EDGE of 8%
        self.assertEqual(result["signal"], "skip")


class TestEvaluateMarketFilters(unittest.TestCase):
    """Extreme-price markets should be filtered out before strategy evaluation."""

    def setUp(self):
        self.strategy = Strategy(kalshi_client=None)

    def test_extreme_high_price_filtered(self):
        """A market priced at 99c (virtually settled YES) should be skipped."""
        market = _make_weather_market(yes_bid=98, yes_ask=99, no_bid=0, no_ask=2)
        result = self.strategy.evaluate_market(market)

        self.assertEqual(result["signal"], "skip")

    def test_extreme_low_price_filtered(self):
        """A market priced at 1c (virtually settled NO) should be skipped."""
        market = _make_weather_market(yes_bid=0, yes_ask=1, no_bid=98, no_ask=99)
        result = self.strategy.evaluate_market(market)

        self.assertEqual(result["signal"], "skip")

    def test_zero_price_filtered(self):
        """A market with all-zero prices should be skipped."""
        market = _make_weather_market(
            yes_bid=0, yes_ask=0, no_bid=0, no_ask=0,
            volume=0, volume_24h=0,
        )
        market["last_price"] = 0
        result = self.strategy.evaluate_market(market)

        self.assertEqual(result["signal"], "skip")

    def test_dead_market_no_volume_filtered(self):
        """A market with zero volume everywhere and default asks (100c) should be skipped."""
        market = _make_weather_market(
            yes_bid=0, yes_ask=100, no_bid=0, no_ask=100,
            volume=0, volume_24h=0,
        )
        market["last_price"] = 0
        result = self.strategy.evaluate_market(market)

        self.assertEqual(result["signal"], "skip")


if __name__ == "__main__":
    unittest.main()
