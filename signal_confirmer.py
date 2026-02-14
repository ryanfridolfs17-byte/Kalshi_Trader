"""
SIGNAL CONFIRMATION ENGINE v3.0
====================================
Before any trade is placed, the signal must be confirmed
by multiple independent sources. This is the "second opinions"
step from the decision flow.

CONFIRMATION SOURCES:
  1. NWS/GFS HRRR   - US Government deterministic forecast
  2. ECMWF IFS       - European deterministic forecast (best global model)
  3. GFS Ensemble    - Does the GFS ensemble agree independently?
  4. ICON Model      - German weather service model
  5. GEM Model       - Canadian weather service model
  6. Bias Tracker    - Historical accuracy of each source for this city

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

        # Count votes
        agree_count = sum(1 for v in votes.values() if v == "AGREE")
        disagree_count = sum(1 for v in votes.values() if v == "DISAGREE")
        abstain_count = sum(1 for v in votes.values() if v == "ABSTAIN")
        total_voted = agree_count + disagree_count  # Abstains don't count

        # Determine verdict
        if total_voted == 0:
            verdict = "WEAK"
            multiplier = 0.5
            summary = "No sources could confirm (all abstained)"
        elif disagree_count > agree_count:
            verdict = "REJECT"
            multiplier = 0.0
            summary = f"{disagree_count}/{total_voted} sources disagree — trade rejected"
        elif agree_count >= 3:
            verdict = "STRONG"
            multiplier = 1.5
            summary = f"{agree_count}/{total_voted} sources agree — STRONG confirmation"
        elif agree_count >= 2:
            verdict = "CONFIRM"
            multiplier = 1.0
            summary = f"{agree_count}/{total_voted} sources agree — confirmed"
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

    def print_vote_details(self, confirmation):
        """Pretty-print the confirmation details."""
        print(f"\n  ┌─ Signal Confirmation: {confirmation['verdict']} "
              f"(size: {confirmation['size_multiplier']}x) ──")
        for source, detail in confirmation.get("vote_details", {}).items():
            vote = confirmation["votes"].get(source, "?")
            icon = {"AGREE": "✓", "DISAGREE": "✗", "ABSTAIN": "~"}.get(vote, "?")
            print(f"  │  {icon} {detail}")
        print(f"  └─ {confirmation['summary']}")
