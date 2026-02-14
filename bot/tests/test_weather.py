"""
Unit tests for WeatherEngine — parse_market_bucket() and calculate_bucket_probability().

Run with:
    python -m unittest tests/test_weather.py
    python -m pytest  tests/test_weather.py
"""

import os
import sys
import unittest

# Allow imports from the src directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from weather_engine import WeatherEngine


class TestParseMarketBucket(unittest.TestCase):
    """Tests for WeatherEngine.parse_market_bucket()."""

    def setUp(self):
        self.engine = WeatherEngine()

    # ── Range format: "35\u00b0F to 39\u00b0F" ──

    def test_parse_bucket_range(self):
        """Parsing '35\u00b0F to 39\u00b0F' should yield temp_low=35, temp_high=39."""
        market = {
            "ticker": "KXHIGHNY-26FEB14-T35T39",
            "title": "High temperature between 35\u00b0F to 39\u00b0F",
            "subtitle": "",
            "event_ticker": "KXHIGHNY-26FEB14",
        }
        result = self.engine.parse_market_bucket(market)

        self.assertIsNotNone(result)
        self.assertEqual(result["temp_low"], 35)
        self.assertEqual(result["temp_high"], 39)

    # ── Above format: "48\u00b0 or above" ──

    def test_parse_bucket_above(self):
        """Parsing '48\u00b0 or above' should yield temp_low=49, temp_high=200.

        The code does `threshold + 1` for the low end because the regex
        captures the threshold and interprets "above" as strictly greater.
        """
        market = {
            "ticker": "KXHIGHNY-26FEB14-B48",
            "title": "",
            "subtitle": "48\u00b0 or above",
            "event_ticker": "KXHIGHNY-26FEB14",
        }
        result = self.engine.parse_market_bucket(market)

        self.assertIsNotNone(result)
        self.assertEqual(result["temp_low"], 49)
        self.assertEqual(result["temp_high"], 200)

    # ── Below format: "23\u00b0 or below" ──

    def test_parse_bucket_below(self):
        """Parsing '23\u00b0 or below' should yield temp_low=-100, temp_high=22.

        The code does `threshold - 1` for the high end.
        """
        market = {
            "ticker": "KXHIGHNY-26FEB14-B23",
            "title": "",
            "subtitle": "23\u00b0 or below",
            "event_ticker": "KXHIGHNY-26FEB14",
        }
        result = self.engine.parse_market_bucket(market)

        self.assertIsNotNone(result)
        self.assertEqual(result["temp_low"], -100)
        self.assertEqual(result["temp_high"], 22)

    # ── City detection from ticker ──

    def test_parse_bucket_city_detection(self):
        """Verify that each supported city is detected from its series ticker prefix."""
        city_tickers = {
            "NYC": "KXHIGHNY",
            "CHI": "KXHIGHCHI",
            "MIA": "KXHIGHMIA",
            "AUS": "KXHIGHAUS",
        }
        for expected_city, series in city_tickers.items():
            market = {
                "ticker": f"{series}-26FEB14-T35T39",
                "title": "High temperature between 35\u00b0F to 39\u00b0F",
                "subtitle": "",
                "event_ticker": f"{series}-26FEB14",
            }
            result = self.engine.parse_market_bucket(market)

            self.assertIsNotNone(result, f"Expected a result for city {expected_city}")
            self.assertEqual(
                result["city_code"],
                expected_city,
                f"City detection failed for ticker prefix {series}",
            )

    # ── Date extraction from event_ticker ──

    def test_parse_bucket_date_extraction(self):
        """Parsing event_ticker 'KXHIGHNY-26FEB14' extracts date 2026-02-14."""
        market = {
            "ticker": "KXHIGHNY-26FEB14-T35T39",
            "title": "High temperature between 35\u00b0F to 39\u00b0F",
            "subtitle": "",
            "event_ticker": "KXHIGHNY-26FEB14",
        }
        result = self.engine.parse_market_bucket(market)

        self.assertIsNotNone(result)
        self.assertEqual(result["target_date"], "2026-02-14")

    def test_parse_bucket_date_extraction_different_month(self):
        """Check date extraction for a different month (March)."""
        market = {
            "ticker": "KXHIGHCHI-26MAR05-T40T44",
            "title": "High temperature between 40\u00b0F to 44\u00b0F",
            "subtitle": "",
            "event_ticker": "KXHIGHCHI-26MAR05",
        }
        result = self.engine.parse_market_bucket(market)

        self.assertIsNotNone(result)
        self.assertEqual(result["target_date"], "2026-03-05")
        self.assertEqual(result["city_code"], "CHI")


class TestCalculateBucketProbability(unittest.TestCase):
    """Tests for WeatherEngine.calculate_bucket_probability()."""

    def setUp(self):
        self.engine = WeatherEngine()

    def test_all_members_in_range(self):
        """If all ensemble members fall in the range, probability should be 1.0."""
        distribution = {"raw_highs": [36, 37, 38, 37, 36, 38, 37, 36, 38, 37]}
        prob = self.engine.calculate_bucket_probability(distribution, 35, 39)
        self.assertAlmostEqual(prob, 1.0)

    def test_no_members_in_range(self):
        """If no ensemble members fall in the range, probability should be 0.0."""
        distribution = {"raw_highs": [50, 51, 52, 53, 54]}
        prob = self.engine.calculate_bucket_probability(distribution, 35, 39)
        self.assertAlmostEqual(prob, 0.0)

    def test_partial_members_in_range(self):
        """Half the members in range should give 0.5 probability."""
        distribution = {"raw_highs": [36, 37, 50, 51, 36, 37, 50, 51, 36, 37]}
        prob = self.engine.calculate_bucket_probability(distribution, 35, 39)
        # 6 out of 10 are in [35, 39]
        self.assertAlmostEqual(prob, 0.6)

    def test_none_distribution_returns_none(self):
        """Passing None distribution should return None."""
        prob = self.engine.calculate_bucket_probability(None, 35, 39)
        self.assertIsNone(prob)

    def test_empty_raw_highs_returns_none(self):
        """Distribution with empty raw_highs should return None."""
        distribution = {"raw_highs": []}
        prob = self.engine.calculate_bucket_probability(distribution, 35, 39)
        self.assertIsNone(prob)

    def test_single_member(self):
        """A single ensemble member either matches (1.0) or does not (0.0)."""
        distribution = {"raw_highs": [37]}
        prob = self.engine.calculate_bucket_probability(distribution, 35, 39)
        self.assertAlmostEqual(prob, 1.0)

        prob_miss = self.engine.calculate_bucket_probability(distribution, 40, 44)
        self.assertAlmostEqual(prob_miss, 0.0)


if __name__ == "__main__":
    unittest.main()
