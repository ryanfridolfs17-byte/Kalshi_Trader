"""
WEATHER FORECAST ENGINE v3.0
================================
Fetches ensemble forecasts from multiple weather models via Open-Meteo API.
Builds probability distributions across temperature buckets.
Compares against Kalshi market prices to find mispricings.

DATA SOURCES (all free, no API key needed):
  - GFS Ensemble:   31 members (NOAA, US)
  - ECMWF IFS:      51 members (European Centre)
  - ICON-EPS:       40 members (DWD, Germany)
  - GEM Ensemble:   21 members (Canada)
  ─────────────────────────────
  TOTAL:           143 ensemble members

Each member is a slightly different simulation of the atmosphere.
By counting how many members predict each temperature bucket,
we build a probability distribution that's far more robust than
any single forecast.

SETTLEMENT SOURCE:
  Kalshi weather markets settle on NWS Daily Climate Report.
  NWS stations: KNYC (Central Park), KMDW (Chicago Midway),
                KMIA (Miami Intl), KAUS (Austin-Bergstrom),
                KLAX (Los Angeles), KDEN (Denver), KPHL (Philadelphia),
                KATL (Atlanta), KBOS (Boston), KDFW (Dallas-Fort Worth),
                KDCA (Washington DC), KIAH (Houston), KLAS (Las Vegas),
                KMSP (Minneapolis), KMSY (New Orleans), KOKC (Oklahoma City),
                KPHX (Phoenix), KSAT (San Antonio), KSEA (Seattle),
                KSFO (San Francisco)
"""

import requests
import math
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import json
import time

# ─────────────────────────────────────────────────────────
# CITY CONFIGURATION
# ─────────────────────────────────────────────────────────
# Coordinates for the actual NWS stations Kalshi uses
CITIES = {
    "NYC": {
        "name": "New York Central Park",
        "lat": 40.7789,
        "lon": -73.9692,
        "series_ticker": "KXHIGHNY",
        "nws_station": "KNYC",
        "timezone": "America/New_York",
    },
    "CHI": {
        "name": "Chicago Midway",
        "lat": 41.7868,
        "lon": -87.7522,
        "series_ticker": "KXHIGHCHI",
        "nws_station": "KMDW",
        "timezone": "America/Chicago",
    },
    "MIA": {
        "name": "Miami International",
        "lat": 25.7959,
        "lon": -80.2870,
        "series_ticker": "KXHIGHMIA",
        "nws_station": "KMIA",
        "timezone": "America/New_York",
    },
    "AUS": {
        "name": "Austin-Bergstrom",
        "lat": 30.1945,
        "lon": -97.6699,
        "series_ticker": "KXHIGHAUS",
        "nws_station": "KAUS",
        "timezone": "America/Chicago",
    },
    "LAX": {
        "name": "Los Angeles",
        "lat": 33.9425,
        "lon": -118.4081,
        "series_ticker": "KXHIGHLAX",
        "nws_station": "KLAX",
        "timezone": "America/Los_Angeles",
    },
    "DEN": {
        "name": "Denver",
        "lat": 39.8561,
        "lon": -104.6737,
        "series_ticker": "KXHIGHDEN",
        "nws_station": "KDEN",
        "timezone": "America/Denver",
    },
    "PHI": {
        "name": "Philadelphia",
        "lat": 39.8744,
        "lon": -75.2424,
        "series_ticker": "KXHIGHPHIL",
        "nws_station": "KPHL",
        "timezone": "America/New_York",
    },
    "ATL": {
        "name": "Atlanta",
        "lat": 33.6407,
        "lon": -84.4277,
        "series_ticker": "KXHIGHTATL",
        "nws_station": "KATL",
        "timezone": "America/New_York",
    },
    "BOS": {
        "name": "Boston",
        "lat": 42.3656,
        "lon": -71.0096,
        "series_ticker": "KXHIGHTBOS",
        "nws_station": "KBOS",
        "timezone": "America/New_York",
    },
    "DAL": {
        "name": "Dallas",
        "lat": 32.8998,
        "lon": -97.0403,
        "series_ticker": "KXHIGHTDAL",
        "nws_station": "KDFW",
        "timezone": "America/Chicago",
    },
    "DC": {
        "name": "Washington DC",
        "lat": 38.8512,
        "lon": -77.0402,
        "series_ticker": "KXHIGHTDC",
        "nws_station": "KDCA",
        "timezone": "America/New_York",
    },
    "HOU": {
        "name": "Houston Hobby",
        "lat": 29.6458,
        "lon": -95.2772,
        "series_ticker": "KXHIGHTHOU",
        "nws_station": "KHOU",
        "timezone": "America/Chicago",
    },
    "LV": {
        "name": "Las Vegas",
        "lat": 36.0840,
        "lon": -115.1537,
        "series_ticker": "KXHIGHTLV",
        "nws_station": "KLAS",
        "timezone": "America/Los_Angeles",
    },
    "MIN": {
        "name": "Minneapolis",
        "lat": 44.8848,
        "lon": -93.2223,
        "series_ticker": "KXHIGHTMIN",
        "nws_station": "KMSP",
        "timezone": "America/Chicago",
    },
    "NOLA": {
        "name": "New Orleans",
        "lat": 29.9934,
        "lon": -90.2580,
        "series_ticker": "KXHIGHTNOLA",
        "nws_station": "KMSY",
        "timezone": "America/Chicago",
    },
    "OKC": {
        "name": "Oklahoma City",
        "lat": 35.3931,
        "lon": -97.6007,
        "series_ticker": "KXHIGHTOKC",
        "nws_station": "KOKC",
        "timezone": "America/Chicago",
    },
    "PHX": {
        "name": "Phoenix",
        "lat": 33.4373,
        "lon": -112.0078,
        "series_ticker": "KXHIGHTPHX",
        "nws_station": "KPHX",
        "timezone": "America/Phoenix",
    },
    "SATX": {
        "name": "San Antonio",
        "lat": 29.5337,
        "lon": -98.4698,
        "series_ticker": "KXHIGHTSATX",
        "nws_station": "KSAT",
        "timezone": "America/Chicago",
    },
    "SEA": {
        "name": "Seattle",
        "lat": 47.4502,
        "lon": -122.3088,
        "series_ticker": "KXHIGHTSEA",
        "nws_station": "KSEA",
        "timezone": "America/Los_Angeles",
    },
    "SFO": {
        "name": "San Francisco",
        "lat": 37.6213,
        "lon": -122.3790,
        "series_ticker": "KXHIGHTSFO",
        "nws_station": "KSFO",
        "timezone": "America/Los_Angeles",
    },
}

# Open-Meteo Ensemble API endpoint
ENSEMBLE_API = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Open-Meteo NWS/GFS for deterministic high-res forecast (confirmation)
NWS_FORECAST_API = "https://api.open-meteo.com/v1/gfs"


class WeatherEngine:
    """
    Fetches ensemble forecasts and builds temperature probability distributions.
    This is the core edge generator for weather trading.
    """

    def __init__(self):
        self._cache = {}
        self._nws_cache = {}
        self.last_fetch_time = None
        # Track model accuracy over time for weighting
        self.model_weights = {
            "gfs_ensemble": 1.0,
            "ecmwf_ifs": 1.0,
            "icon_eps": 1.0,
            "gem_ensemble": 1.0,
        }

    # ═══════════════════════════════════════════════════════
    # MAIN ENTRY: Get probability distribution for a city
    # ═══════════════════════════════════════════════════════

    def get_temperature_distribution(self, city_code, target_date=None):
        """
        Fetch all ensemble members and build a probability distribution
        of high temperatures for the given city and date.

        Returns:
        {
            "city": "NYC",
            "target_date": "2026-02-12",
            "forecasted_high_mean": 38.2,
            "forecasted_high_median": 38.0,
            "forecasted_high_min": 32.0,
            "forecasted_high_max": 44.0,
            "spread": 12.0,
            "total_members": 143,
            "bucket_probs": {
                "30-34": 0.12,  # 12% of members predict 30-34°F
                "35-39": 0.45,  # 45% predict 35-39°F
                "40-44": 0.31,  # etc.
                ...
            },
            "raw_highs": [37, 38, 36, 41, ...],  # All 143 values
            "sources_used": ["gfs_ensemble", "ecmwf_ifs", ...],
            "confidence": 0.85,  # How tight the distribution is
        }
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        if target_date is None:
            target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Check cache — models update every 6 hours, so 5-min cache is plenty.
        # At 2-min scan interval this saves ~60% of Open-Meteo API calls.
        cache_key = f"{city_code}_{target_date}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 300:  # 5 minutes
                return cached["data"]

        # Fetch from all ensemble sources
        all_highs = []
        sources_used = []
        model_family_highs = {}  # Track per-model family for disagreement detection

        # Source 1: GFS Ensemble (31 members)
        gfs_highs = self._fetch_ensemble(city, target_date, "gfs_seamless_eps")
        if gfs_highs:
            all_highs.extend(gfs_highs)
            sources_used.append("gfs_ensemble")
            model_family_highs["GFS"] = gfs_highs

        # Source 2: ECMWF IFS Ensemble (51 members)
        ecmwf_highs = self._fetch_ensemble(city, target_date, "ecmwf_ifs025_ensemble")
        if ecmwf_highs:
            all_highs.extend(ecmwf_highs)
            sources_used.append("ecmwf_ifs")
            model_family_highs["ECMWF"] = ecmwf_highs

        # Source 3: ICON-EPS (40 members)
        icon_highs = self._fetch_ensemble(city, target_date, "icon_seamless_eps")
        if icon_highs:
            all_highs.extend(icon_highs)
            sources_used.append("icon_eps")
            model_family_highs["ICON"] = icon_highs

        # Source 4: GEM Ensemble (21 members)
        gem_highs = self._fetch_ensemble(city, target_date, "gem_global_ensemble")
        if gem_highs:
            all_highs.extend(gem_highs)
            sources_used.append("gem_ensemble")
            model_family_highs["GEM"] = gem_highs

        if not all_highs:
            print(f"  [WEATHER] WARN: No ensemble data for {city_code} on {target_date}")
            return None

        # Build the distribution
        result = self._build_distribution(city_code, target_date, all_highs, sources_used)

        # Add per-model family means for disagreement detection
        model_means = {}
        for model_name, highs in model_family_highs.items():
            if highs:
                model_means[model_name] = round(sum(highs) / len(highs), 1)
        result["model_means"] = model_means
        if len(model_means) >= 2:
            means_list = list(model_means.values())
            result["model_spread"] = round(max(means_list) - min(means_list), 1)
        else:
            result["model_spread"] = 0.0

        # Cache it
        self._cache[cache_key] = {
            "data": result,
            "fetched_at": datetime.now(),
        }
        self.last_fetch_time = datetime.now()

        return result

    # ═══════════════════════════════════════════════════════
    # CONFIRMATION: Get NWS deterministic forecast
    # ═══════════════════════════════════════════════════════

    def get_nws_confirmation(self, city_code, target_date=None):
        """
        Get the deterministic NWS/GFS high-res forecast.
        Used as one of the 'second opinion' confirmation sources.

        Returns {"high_temp": 38, "source": "NWS/GFS HRRR"}
        """
        city = CITIES.get(city_code)
        if not city:
            return None

        cache_key = f"nws_{city_code}_{target_date}"
        if cache_key in self._nws_cache:
            cached = self._nws_cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 1800:
                return cached["data"]

        try:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city["timezone"],
                "forecast_days": 3,
            }
            response = requests.get(NWS_FORECAST_API, params=params, timeout=15)
            if response.status_code != 200:
                return None

            data = response.json()
            dates = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            if target_date is None:
                target_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            for i, d in enumerate(dates):
                if d == target_date and i < len(temps) and temps[i] is not None:
                    result = {
                        "high_temp": round(temps[i]),
                        "source": "NWS/GFS HRRR",
                    }
                    self._nws_cache[cache_key] = {
                        "data": result,
                        "fetched_at": datetime.now(),
                    }
                    return result

        except Exception as e:
            print(f"  [WEATHER] NWS confirmation fetch error: {e}")

        return None

    # ═══════════════════════════════════════════════════════
    # INTERNAL: Fetch ensemble data from Open-Meteo
    # ═══════════════════════════════════════════════════════

    def _fetch_ensemble(self, city, target_date, model):
        """
        Fetch hourly temperature_2m from a specific ensemble model.
        Extract the daily high for target_date from each member.
        Returns list of high temperatures (one per ensemble member).
        """
        try:
            params = {
                "latitude": city["lat"],
                "longitude": city["lon"],
                "hourly": "temperature_2m",
                "models": model,
                "temperature_unit": "fahrenheit",
                "timezone": city["timezone"],
                "forecast_days": 3,
            }

            response = requests.get(ENSEMBLE_API, params=params, timeout=20)

            if response.status_code != 200:
                return None

            data = response.json()
            hourly = data.get("hourly", {})
            times = hourly.get("time", [])

            if not times:
                return None

            # Find indices for the target date (daytime hours 6 AM - 11 PM)
            target_indices = []
            for i, t in enumerate(times):
                if t.startswith(target_date):
                    # Extract hour
                    hour = int(t[11:13])
                    if 6 <= hour <= 23:  # Daytime hours only
                        target_indices.append(i)

            if not target_indices:
                return None

            # Extract high temp for each ensemble member
            member_highs = []

            # Ensemble data comes as temperature_2m_member01, member02, etc.
            # OR as a single key with array of arrays
            member_keys = [k for k in hourly.keys() if k.startswith("temperature_2m")]
            member_keys = [k for k in member_keys if k != "time"]

            for key in member_keys:
                values = hourly[key]
                if not values:
                    continue

                # Get max temp during daytime hours for this member
                daytime_temps = []
                for idx in target_indices:
                    if idx < len(values) and values[idx] is not None:
                        daytime_temps.append(values[idx])

                if daytime_temps:
                    member_highs.append(round(max(daytime_temps), 1))

            return member_highs if member_highs else None

        except Exception as e:
            # Silently fail — we have multiple sources
            return None

    # ═══════════════════════════════════════════════════════
    # INTERNAL: Build probability distribution from raw highs
    # ═══════════════════════════════════════════════════════

    def _build_distribution(self, city_code, target_date, all_highs, sources_used):
        """
        Take all ensemble member high temps and build:
        1. Statistics (mean, median, min, max, spread)
        2. Bucket probabilities (matching Kalshi's 5°F brackets)
        3. Confidence score
        """
        n = len(all_highs)
        sorted_highs = sorted(all_highs)

        mean_temp = sum(all_highs) / n
        median_temp = sorted_highs[n // 2]
        min_temp = sorted_highs[0]
        max_temp = sorted_highs[-1]
        spread = max_temp - min_temp

        # Standard deviation for confidence
        variance = sum((t - mean_temp) ** 2 for t in all_highs) / n
        std_dev = math.sqrt(variance)

        # Build 5°F bucket probabilities (Kalshi uses ~5°F brackets)
        # Buckets: ..., 20-24, 25-29, 30-34, 35-39, 40-44, ...
        bucket_counts = defaultdict(int)

        for temp in all_highs:
            bucket_floor = int(temp // 5) * 5
            bucket_label = f"{bucket_floor}-{bucket_floor + 4}"
            bucket_counts[bucket_label] += 1

        # Also build 2°F buckets for finer resolution
        # (Kalshi sometimes uses 2°F brackets like 36-37, 38-39, etc.)
        fine_bucket_counts = defaultdict(int)
        for temp in all_highs:
            bucket_floor = int(temp // 2) * 2
            fine_bucket_label = f"{bucket_floor}-{bucket_floor + 1}"
            fine_bucket_counts[fine_bucket_label] += 1

        # Convert to probabilities
        bucket_probs = {k: v / n for k, v in sorted(bucket_counts.items())}
        fine_bucket_probs = {k: v / n for k, v in sorted(fine_bucket_counts.items())}

        # Confidence: tighter spread = higher confidence
        # If std_dev < 2°F, very confident. If > 5°F, uncertain.
        confidence = max(0.3, min(0.95, 1.0 - (std_dev / 10.0)))

        return {
            "city": city_code,
            "target_date": target_date,
            "forecasted_high_mean": round(mean_temp, 1),
            "forecasted_high_median": round(median_temp, 1),
            "forecasted_high_min": round(min_temp, 1),
            "forecasted_high_max": round(max_temp, 1),
            "spread": round(spread, 1),
            "std_dev": round(std_dev, 1),
            "total_members": n,
            "bucket_probs_5f": bucket_probs,
            "bucket_probs_2f": fine_bucket_probs,
            "raw_highs": sorted_highs,
            "sources_used": sources_used,
            "confidence": round(confidence, 3),
        }

    # ═══════════════════════════════════════════════════════
    # UTILITY: Match a market title to a temperature bucket
    # ═══════════════════════════════════════════════════════

    def parse_market_bucket(self, market):
        """
        Parse a Kalshi weather market to extract:
        - city code
        - temperature range (low, high)
        - target date

        Returns dict or None if not a weather market.
        """
        ticker = market.get("ticker", "").upper()
        title = market.get("title", "")
        subtitle = market.get("subtitle", "")
        event_ticker = market.get("event_ticker", "").upper()

        # Identify city from ticker/event
        city_code = None
        for code, info in CITIES.items():
            if info["series_ticker"].upper() in ticker or info["series_ticker"].upper() in event_ticker:
                city_code = code
                break

        if not city_code:
            return None

        # Extract temperature range from title/subtitle
        # Patterns: "35°F to 39°F", "35 - 39", "above 40", "below 30"
        import re

        temp_low = None
        temp_high = None

        # Try range pattern: "XX°F to YY°F" or "XX - YY" or "XX to YY"
        range_match = re.search(r'(\d+)\s*(?:°F?\s*)?(?:to|-)\s*(\d+)', title + " " + subtitle)
        if range_match:
            temp_low = int(range_match.group(1))
            temp_high = int(range_match.group(2))
        else:
            # Try threshold pattern: "above XX" or "below XX" or "at least XX"
            # Also handle Kalshi subtitle patterns like "48° or above", "39° or below"
            above_match = re.search(r'(?:above|over|higher than|at least|≥|>=|>)\s*(\d+)', title + " " + subtitle, re.I)
            if not above_match:
                above_match = re.search(r'(\d+)°?\s*or\s*above', title + " " + subtitle, re.I)
            below_match = re.search(r'(?:below|under|lower than|less than|≤|<=|<)\s*(\d+)', title + " " + subtitle, re.I)
            if not below_match:
                below_match = re.search(r'(\d+)°?\s*or\s*below', title + " " + subtitle, re.I)

            if above_match:
                threshold = int(above_match.group(1))
                # Check if this was the inclusive "X or above" pattern vs strict "above X"
                matched_text = (title + " " + subtitle)[above_match.start():above_match.end()]
                if re.search(r'\d+°?\s*or\s*above', matched_text, re.I):
                    temp_low = threshold      # inclusive: "48° or above" → ≥48
                else:
                    temp_low = threshold + 1  # strict: "above 47°" → ≥48
                temp_high = 200  # Effectively infinity
            elif below_match:
                threshold = int(below_match.group(1))
                # Check if this was the inclusive "X or below" pattern vs strict "below X"
                matched_text = (title + " " + subtitle)[below_match.start():below_match.end()]
                if re.search(r'\d+°?\s*or\s*below', matched_text, re.I):
                    temp_high = threshold      # inclusive: "77° or below" → ≤77
                else:
                    temp_high = threshold - 1  # strict: "below 78°" → ≤77
                temp_low = -100  # Effectively negative infinity

        # Fallback: parse bucket/threshold directly from ticker suffix.
        # Ticker format: KXHIGH...-26FEB20-B77.5 (bucket) or -T55 (threshold)
        # This handles positions with empty titles (e.g., after reconciliation).
        if temp_low is None:
            bucket_match = re.search(r'-B(\d+\.?\d*)', ticker)
            thresh_match = re.search(r'-T(\d+\.?\d*)', ticker)
            if bucket_match:
                midpoint = float(bucket_match.group(1))
                temp_low = int(midpoint - 0.5)
                temp_high = int(midpoint + 0.5)
            elif thresh_match:
                threshold = int(float(thresh_match.group(1)))
                temp_low = threshold
                temp_high = 200
            else:
                return None

        # Extract date from event ticker (format: KXHIGHNY-26FEB12)
        date_match = re.search(r'-(\d{2})([A-Z]{3})(\d{2})', event_ticker)
        if date_match:
            year = 2000 + int(date_match.group(1))
            month_str = date_match.group(2)
            day = int(date_match.group(3))
            months = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
                       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
            month = months.get(month_str, 0)
            if month > 0:
                target_date = f"{year}-{month:02d}-{day:02d}"
            else:
                target_date = None
        else:
            target_date = None

        return {
            "city_code": city_code,
            "temp_low": temp_low,
            "temp_high": temp_high,
            "target_date": target_date,
        }

    def calculate_bucket_probability(self, distribution, temp_low, temp_high):
        """
        Given a distribution and a temperature range, calculate the
        probability that the actual high falls in that range.

        Uses the raw ensemble member highs for precision.
        """
        if not distribution or not distribution.get("raw_highs"):
            return None

        highs = distribution["raw_highs"]
        n = len(highs)

        # Count members in range
        in_range = sum(1 for t in highs if temp_low <= t <= temp_high)
        prob = in_range / n

        return prob

    # ═══════════════════════════════════════════════════════
    # STATUS: Print current state
    # ═══════════════════════════════════════════════════════

    def print_status(self):
        """Print what's currently cached."""
        if not self._cache:
            print("  [WEATHER] No forecasts cached yet")
            return

        for key, cached in self._cache.items():
            data = cached["data"]
            age_min = (datetime.now() - cached["fetched_at"]).total_seconds() / 60
            print(f"  [WEATHER] {data['city']} {data['target_date']}: "
                  f"mean={data['forecasted_high_mean']}°F, "
                  f"spread=±{data['std_dev']}°F, "
                  f"{data['total_members']} members, "
                  f"cached {age_min:.0f}min ago")
