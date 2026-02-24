"""
SIGNAL CONFIRMATION ENGINE v3.0
====================================
Before any trade is placed, the signal must be confirmed
by multiple independent sources. This is the "second opinions"
step from the decision flow.

CONFIRMATION SOURCES:
  1. NWS/GFS HRRR   - US Government deterministic forecast
  2. ECMWF IFS       - European deterministic forecast (best global model)
  3. ICON Model      - German weather service model
  4. GEM Model       - Canadian weather service model
  5. NWS Point Fcst  - Actual NWS station-area forecast (most authoritative for settlement)

VOTING SYSTEM:
  Each source votes: AGREE, DISAGREE, or ABSTAIN
  STRONG:   3+ agree  → 1.5x position size
  CONFIRM:  2 agree   → 1.0x position size
  WEAK:     Mixed     → 0.5x position size
  REJECT:   Majority disagree → SKIP trade entirely
"""

import requests
from datetime import datetime, timezone
import math


# Open-Meteo deterministic forecast endpoints
DETERMINISTIC_APIS = {
    "nws_gfs": {
        "url": "https://api.open-meteo.com/v1/gfs",
        "name": "NWS/GFS HRRR",
    },
    "ecmwf": {
        "url": "https://api.open-meteo.com/v1/ecmwf",
        "name": "ECMWF IFS",
    },
    "icon": {
        "url": "https://api.open-meteo.com/v1/dwd-icon",
        "name": "DWD ICON",
    },
    "gem": {
        "url": "https://api.open-meteo.com/v1/gem",
        "name": "GEM Canada",
    },
}


class SignalConfirmer:
    """
    Confirms or rejects trading signals by polling multiple
    independent weather sources.
    """

    def __init__(self):
        self._cache = {}
        # Track how often each source was correct (for future weighting)
        self.accuracy_log = {
            "nws_gfs": {"correct": 0, "total": 0},
            "ecmwf": {"correct": 0, "total": 0},
            "icon": {"correct": 0, "total": 0},
            "gem": {"correct": 0, "total": 0},
            "nws_point": {"correct": 0, "total": 0},
        }

    def confirm_signal(self, city_info, target_date, temp_low, temp_high, ensemble_prob, market_price_cents):
        """
        Ask independent sources whether they agree that the market
        is mispriced for this temperature bucket.

        Args:
            city_info: dict with lat, lon, timezone
            target_date: "YYYY-MM-DD"
            temp_low: lower bound of bucket (°F)
            temp_high: upper bound of bucket (°F)
            ensemble_prob: our ensemble probability (0.0-1.0)
            market_price_cents: Kalshi market price in cents

        Returns:
            {
                "verdict": "STRONG" | "CONFIRM" | "WEAK" | "REJECT",
                "size_multiplier": 1.5 | 1.0 | 0.5 | 0.0,
                "votes": {"nws_gfs": "AGREE", "ecmwf": "DISAGREE", ...},
                "details": "3/4 sources agree signal is valid",
            }
        """
        market_prob = market_price_cents / 100.0
        our_prob = ensemble_prob

        # The signal says: our_prob is significantly different from market_prob
        # We're asking each source: do you also think the market is wrong?

        votes = {}
        vote_details = {}

        for source_key, source_info in DETERMINISTIC_APIS.items():
            forecast_high = self._get_deterministic_high(
                source_key, source_info["url"], city_info, target_date
            )

            if forecast_high is None:
                votes[source_key] = "ABSTAIN"
                vote_details[source_key] = f"{source_info['name']}: no data"
                continue

            # Does this forecast agree with our ensemble signal?
            in_bucket = temp_low <= forecast_high <= temp_high
            # If our signal says "this bucket is more likely than market thinks"
            # and this source's forecast falls IN the bucket, it agrees.
            # If our signal says "less likely" and forecast is OUTSIDE bucket, agrees.

            our_direction = "underpriced" if our_prob > market_prob else "overpriced"

            if our_direction == "underpriced":
                if in_bucket:
                    votes[source_key] = "AGREE"
                    vote_details[source_key] = (
                        f"{source_info['name']}: {forecast_high}°F "
                        f"(in bucket {temp_low}-{temp_high}) → AGREE"
                    )
                else:
                    # How close is it?
                    dist = min(abs(forecast_high - temp_low), abs(forecast_high - temp_high))
                    if dist <= 3:  # Within 3°F of bucket edge
                        votes[source_key] = "ABSTAIN"
                        vote_details[source_key] = (
                            f"{source_info['name']}: {forecast_high}°F "
                            f"(near bucket, {dist}°F away) → ABSTAIN"
                        )
                    else:
                        votes[source_key] = "DISAGREE"
                        vote_details[source_key] = (
                            f"{source_info['name']}: {forecast_high}°F "
                            f"(outside bucket by {dist}°F) → DISAGREE"
                        )
            else:  # overpriced
                if not in_bucket:
                    votes[source_key] = "AGREE"
                    vote_details[source_key] = (
                        f"{source_info['name']}: {forecast_high}°F "
                        f"(outside bucket) → AGREE with overpriced signal"
                    )
                else:
                    votes[source_key] = "DISAGREE"
                    vote_details[source_key] = (
                        f"{source_info['name']}: {forecast_high}°F "
                        f"(in bucket) → DISAGREE with overpriced signal"
                    )

        # 5th source: NWS station point forecast (most authoritative for settlement)
        nws_point_high = self._get_nws_point_forecast(city_info, target_date)
        if nws_point_high is not None:
            in_bucket = temp_low <= nws_point_high <= temp_high
            our_direction = "underpriced" if our_prob > market_prob else "overpriced"

            if our_direction == "underpriced":
                if in_bucket:
                    votes["nws_point"] = "AGREE"
                    vote_details["nws_point"] = (
                        f"NWS Station Forecast: {nws_point_high}°F "
                        f"(in bucket {temp_low}-{temp_high}) → AGREE"
                    )
                else:
                    dist = min(abs(nws_point_high - temp_low), abs(nws_point_high - temp_high))
                    if dist <= 2:  # Tighter abstain zone (NWS is authoritative)
                        votes["nws_point"] = "ABSTAIN"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(near bucket, {dist}°F away) → ABSTAIN"
                        )
                    else:
                        votes["nws_point"] = "DISAGREE"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(outside bucket by {dist}°F) → DISAGREE"
                        )
            else:  # overpriced
                if not in_bucket:
                    dist = min(abs(nws_point_high - temp_low), abs(nws_point_high - temp_high))
                    if dist <= 2:  # NWS within 2°F of bucket = too close to call
                        votes["nws_point"] = "ABSTAIN"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(near bucket, {dist}°F away) → ABSTAIN"
                        )
                    else:
                        votes["nws_point"] = "AGREE"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(outside bucket by {dist}°F) → AGREE with overpriced signal"
                        )
                else:
                    # NWS says temp IS in the bucket, which contradicts overpriced signal.
                    # But if barely inside (within 2°F of bucket edge), ABSTAIN — NWS
                    # rounding means the actual settlement temp could be outside.
                    dist_to_edge = min(abs(nws_point_high - temp_low), abs(nws_point_high - temp_high))
                    if dist_to_edge <= 2:
                        votes["nws_point"] = "ABSTAIN"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(barely in bucket, {dist_to_edge}°F from edge) → ABSTAIN"
                        )
                    else:
                        votes["nws_point"] = "DISAGREE"
                        vote_details["nws_point"] = (
                            f"NWS Station Forecast: {nws_point_high}°F "
                            f"(in bucket) → DISAGREE with overpriced signal"
                        )
        else:
            votes["nws_point"] = "ABSTAIN"
            vote_details["nws_point"] = "NWS Station Forecast: API failed/no data → ABSTAIN"
            print(f"    [CONFIRM] NWS point forecast unavailable for {city_info.get('nws_station', '?')} on {target_date}")

        # Count votes
        agree_count = sum(1 for v in votes.values() if v == "AGREE")
        disagree_count = sum(1 for v in votes.values() if v == "DISAGREE")
        abstain_count = sum(1 for v in votes.values() if v == "ABSTAIN")
        total_voted = agree_count + disagree_count  # Abstains don't count

        # NWS is the settlement source — its vote carries special weight.
        # DISAGREE = hard REJECT (NWS says settlement will contradict our thesis)
        # ABSTAIN  = cap at CONFIRM (too close to call, never STRONG)
        # AGREE    = eligible for STRONG
        nws_vote = votes.get("nws_point", "ABSTAIN")

        # Determine verdict
        if nws_vote == "DISAGREE":
            # Settlement source explicitly contradicts our thesis — hard block
            verdict = "REJECT"
            multiplier = 0.0
            summary = (f"NWS DISAGREE — settlement source contradicts signal "
                       f"({agree_count} model(s) agree but NWS overrides)")
        elif total_voted == 0:
            verdict = "REJECT"
            multiplier = 0.0
            summary = "No sources could confirm (all abstained) — REJECT (0% win rate on WEAK)"
        elif disagree_count > agree_count:
            verdict = "REJECT"
            multiplier = 0.0
            summary = f"{disagree_count}/{total_voted} sources disagree — trade rejected"
        elif agree_count >= 3 and nws_vote == "AGREE":
            verdict = "STRONG"
            multiplier = 1.5
            summary = f"{agree_count}/{total_voted} sources agree + NWS — STRONG confirmation"
        elif agree_count >= 3 and nws_vote == "ABSTAIN":
            verdict = "CONFIRM"
            multiplier = 1.0
            summary = (f"{agree_count}/{total_voted} sources agree but NWS abstained "
                       f"— capped at CONFIRM")
        elif agree_count >= 2:
            verdict = "CONFIRM"
            multiplier = 1.0
            summary = f"{agree_count}/{total_voted} sources agree — confirmed"
        else:
            verdict = "REJECT"
            multiplier = 0.0
            summary = f"Only {agree_count}/{total_voted} agree — REJECT (0% win rate on WEAK)"

        return {
            "verdict": verdict,
            "size_multiplier": multiplier,
            "votes": votes,
            "vote_details": vote_details,
            "agree_count": agree_count,
            "disagree_count": disagree_count,
            "abstain_count": abstain_count,
            "summary": summary,
        }

    def _get_deterministic_high(self, source_key, api_url, city_info, target_date):
        """
        Fetch deterministic high temperature from a single model.
        Returns temperature in °F or None.
        """
        cache_key = f"{source_key}_{city_info['lat']}_{target_date}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 1800:  # 30 min cache
                return cached["temp"]

        try:
            params = {
                "latitude": city_info["lat"],
                "longitude": city_info["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": city_info.get("timezone", "auto"),
                "forecast_days": 3,
            }

            response = requests.get(api_url, params=params, timeout=15)
            if response.status_code != 200:
                return None

            data = response.json()
            dates = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            for i, d in enumerate(dates):
                if d == target_date and i < len(temps) and temps[i] is not None:
                    temp = round(temps[i])
                    self._cache[cache_key] = {
                        "temp": temp,
                        "fetched_at": datetime.now(),
                    }
                    return temp

        except Exception:
            pass

        return None

    def _get_nws_point_forecast(self, city_info, target_date):
        """
        Fetch the actual NWS grid-point forecast for the station's coordinates.
        This is the same forecast system NWS uses for their Daily Climate Reports —
        the most authoritative source for Kalshi settlement.

        Returns temperature in °F (integer) or None.
        """
        cache_key = f"nws_point_{city_info.get('nws_station', '')}_{target_date}"
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            age = (datetime.now() - cached["fetched_at"]).total_seconds()
            if age < 3600:  # 1 hour cache (NWS updates less frequently)
                return cached["temp"]

        try:
            headers = {
                "User-Agent": "KalshiBot/3.2 (trading-bot@example.com)",
                "Accept": "application/geo+json",
            }

            # Step 1: Get the forecast grid endpoint for this lat/lon
            points_url = f"https://api.weather.gov/points/{city_info['lat']},{city_info['lon']}"
            resp = requests.get(points_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None

            forecast_url = resp.json().get("properties", {}).get("forecast")
            if not forecast_url:
                return None

            # Step 2: Get the actual period forecast
            resp2 = requests.get(forecast_url, headers=headers, timeout=15)
            if resp2.status_code != 200:
                return None

            periods = resp2.json().get("properties", {}).get("periods", [])
            for period in periods:
                start = period.get("startTime", "")
                if target_date in start and period.get("isDaytime", False):
                    temp = period.get("temperature")
                    if temp is not None:
                        self._cache[cache_key] = {
                            "temp": temp,
                            "fetched_at": datetime.now(),
                        }
                        return temp

        except Exception:
            pass

        return None

    def print_vote_details(self, confirmation):
        """Pretty-print the confirmation details."""
        print(f"\n  ┌─ Signal Confirmation: {confirmation['verdict']} "
              f"(size: {confirmation['size_multiplier']}x) ──")
        for source, detail in confirmation.get("vote_details", {}).items():
            vote = confirmation["votes"].get(source, "?")
            icon = {"AGREE": "✓", "DISAGREE": "✗", "ABSTAIN": "~"}.get(vote, "?")
            print(f"  │  {icon} {detail}")
        print(f"  └─ {confirmation['summary']}")
