"""
SIGNAL CONFIRMATION ENGINE v4.0
================================
5-source voting system with NWS veto power.
Returns STRONG, CONFIRM, or REJECT (WEAK eliminated -- 0% win rate).

Sources:
  1. NWS/GFS HRRR   -- US deterministic (Open-Meteo)
  2. ECMWF IFS       -- European deterministic (Open-Meteo)
  3. DWD ICON        -- German model (Open-Meteo)
  4. GEM Canada      -- Canadian model (Open-Meteo)
  5. NWS Point Fcst  -- Settlement source, veto power (api.weather.gov)

Verdicts:
  STRONG:  3+ models agree + NWS agrees  -> 1.5x sizing
  CONFIRM: 2+ agree, NWS neutral/abstain -> 1.0x sizing
  REJECT:  NWS disagrees OR majority disagree -> 0.0x (skip)
"""

import requests
import time
from datetime import datetime

# Open-Meteo deterministic endpoints
_MODEL_APIS = {
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

# Cache durations (seconds)
_MODEL_CACHE_TTL = 1800   # 30 min for model forecasts
_NWS_CACHE_TTL = 3600     # 60 min for NWS point forecast

# Gray zone: forecast within this many deg F of bucket edge -> ABSTAIN
_MODEL_GRAY_ZONE_F = 2.0
_NWS_GRAY_ZONE_F = 2.0


class SignalConfirmer:
    """Confirms or rejects trading signals via 5-source independent voting."""

    def __init__(self):
        self._cache = {}  # {cache_key: (timestamp, result)}

    def confirm_signal(self, city_info, target_date, temp_low, temp_high,
                       our_prob=None, market_price_cents=0,
                       ensemble_prob=None):
        """
        Poll 5 independent sources and return a confirmation verdict.

        Args:
            city_info: dict with name, lat, lon, station, tz
            target_date: "YYYY-MM-DD"
            temp_low: lower bound of temperature bucket (deg F)
            temp_high: upper bound of temperature bucket (deg F)
            our_prob: our ensemble probability (0.0-1.0)
            market_price_cents: Kalshi market price in cents (0-100)
            ensemble_prob: alias for our_prob (backward compat)

        Returns:
            dict with verdict, size_multiplier, votes, details, nws_agrees
        """
        # Support legacy callers that pass ensemble_prob=
        if our_prob is None:
            our_prob = ensemble_prob if ensemble_prob is not None else 0.5
        votes = {}
        details = {}

        # --- 4 model sources from Open-Meteo ---
        for key, info in _MODEL_APIS.items():
            high = self._get_deterministic_high(key, city_info, target_date)
            if high is None:
                votes[key] = "ABSTAIN"
                details[key] = f"{info['name']}: no data -> ABSTAIN"
                continue
            vote = self._cast_vote(high, temp_low, temp_high, our_prob,
                                   gray_zone=_MODEL_GRAY_ZONE_F)
            votes[key] = vote
            details[key] = f"{info['name']}: {high}F [{temp_low}-{temp_high}] -> {vote}"

        # --- 5th source: NWS point forecast (settlement authority) ---
        nws_high = self._get_nws_point_forecast(city_info, target_date)
        if nws_high is None:
            votes["nws_point"] = "ABSTAIN"
            details["nws_point"] = "NWS Point: no data -> ABSTAIN"
        else:
            vote = self._cast_vote(nws_high, temp_low, temp_high, our_prob,
                                   gray_zone=_NWS_GRAY_ZONE_F)
            votes["nws_point"] = vote
            details["nws_point"] = f"NWS Point: {nws_high}F [{temp_low}-{temp_high}] -> {vote}"

        # --- Tally ---
        agree = sum(1 for v in votes.values() if v == "AGREE")
        disagree = sum(1 for v in votes.values() if v == "DISAGREE")
        nws_vote = votes.get("nws_point", "ABSTAIN")
        nws_agrees = (nws_vote == "AGREE")

        # --- Verdict logic ---
        if nws_vote == "DISAGREE":
            verdict, mult = "REJECT", 0.0
            summary = f"NWS DISAGREE overrides {agree} model agree(s) -> REJECT"
        elif disagree > agree:
            verdict, mult = "REJECT", 0.0
            summary = f"Majority disagree ({disagree}>{agree}) -> REJECT"
        elif agree >= 3 and nws_agrees:
            verdict, mult = "STRONG", 1.5
            summary = f"{agree}/5 agree + NWS agrees -> STRONG"
        elif agree >= 2:
            verdict, mult = "CONFIRM", 1.0
            nws_note = "+ NWS agrees" if nws_agrees else "(NWS abstains)"
            summary = f"{agree}/5 agree {nws_note} -> CONFIRM"
        else:
            verdict, mult = "REJECT", 0.0
            summary = f"Only {agree}/5 agree -> REJECT"

        city_name = city_info.get("name", "?")
        print(f"    [CONFIRM] {city_name} {target_date}: {summary}")

        return {
            "verdict": verdict,
            "size_multiplier": mult,
            "votes": votes,
            "details": details,
            "nws_agrees": nws_agrees,
            "agree_count": agree,
            "disagree_count": disagree,
            "summary": summary,
        }

    def _get_deterministic_high(self, source_key, city_info, target_date):
        """
        Fetch a single model's predicted daily high from Open-Meteo.

        Returns temperature in deg F (rounded int) or None.
        """
        cache_key = f"{source_key}_{city_info.get('name', '')}_{target_date}"
        cached = self._cache.get(cache_key)
        if cached:
            ts, temp = cached
            if time.time() - ts < _MODEL_CACHE_TTL:
                return temp

        api_url = _MODEL_APIS[source_key]["url"]
        try:
            resp = requests.get(api_url, params={
                "latitude": city_info["lat"],
                "longitude": city_info["lon"],
                "daily": "temperature_2m_max",
                "temperature_unit": "fahrenheit",
                "timezone": "auto",
                "forecast_days": 3,
            }, timeout=15)
            if resp.status_code != 200:
                return None

            data = resp.json()
            dates = data.get("daily", {}).get("time", [])
            temps = data.get("daily", {}).get("temperature_2m_max", [])

            for i, d in enumerate(dates):
                if d == target_date and i < len(temps) and temps[i] is not None:
                    temp = round(temps[i])
                    self._cache[cache_key] = (time.time(), temp)
                    return temp
        except Exception:
            pass
        return None

    def _get_nws_point_forecast(self, city_info, target_date):
        """
        Fetch NWS point forecast (two-step: gridpoint lookup -> forecast).

        Returns predicted daily high in deg F (int) or None.
        """
        station = city_info.get("station", "")
        cache_key = f"nws_point_{station}_{target_date}"
        cached = self._cache.get(cache_key)
        if cached:
            ts, temp = cached
            if time.time() - ts < _NWS_CACHE_TTL:
                return temp

        headers = {
            "User-Agent": "KalshiBot/4.0 (weather-trading-bot)",
            "Accept": "application/geo+json",
        }
        try:
            # Step 1: resolve lat/lon to forecast grid URL
            points_url = f"https://api.weather.gov/points/{city_info['lat']},{city_info['lon']}"
            r1 = requests.get(points_url, headers=headers, timeout=15)
            if r1.status_code != 200:
                return None
            forecast_url = r1.json().get("properties", {}).get("forecast")
            if not forecast_url:
                return None

            # Step 2: get period forecasts, find matching daytime period
            r2 = requests.get(forecast_url, headers=headers, timeout=15)
            if r2.status_code != 200:
                return None
            periods = r2.json().get("properties", {}).get("periods", [])
            for period in periods:
                start = period.get("startTime", "")
                if target_date in start and period.get("isDaytime", False):
                    temp = period.get("temperature")
                    if temp is not None:
                        self._cache[cache_key] = (time.time(), int(temp))
                        return int(temp)
        except Exception:
            pass
        return None

    def _cast_vote(self, forecast_high, temp_low, temp_high, our_prob,
                   gray_zone=2.0):
        """
        Determine whether a forecast agrees with our signal.

        Returns 'AGREE', 'DISAGREE', or 'ABSTAIN'.

        YES signal (our_prob > 0.5): we think temp lands IN bucket.
          - Forecast in bucket -> AGREE
          - Forecast outside but within gray_zone of edge -> ABSTAIN
          - Forecast clearly outside -> DISAGREE

        NO signal (our_prob <= 0.5): we think temp lands OUTSIDE bucket.
          - Forecast outside bucket by > gray_zone -> AGREE
          - Forecast within gray_zone of edge -> ABSTAIN
          - Forecast in bucket -> DISAGREE
        """
        in_bucket = temp_low <= forecast_high <= temp_high
        dist_to_edge = min(abs(forecast_high - temp_low),
                           abs(forecast_high - temp_high))
        is_yes = our_prob > 0.5

        if is_yes:
            if in_bucket:
                return "AGREE"
            elif dist_to_edge <= gray_zone:
                return "ABSTAIN"
            else:
                return "DISAGREE"
        else:  # NO signal
            if in_bucket:
                if dist_to_edge <= gray_zone:
                    return "ABSTAIN"
                return "DISAGREE"
            else:
                if dist_to_edge <= gray_zone:
                    return "ABSTAIN"
                return "AGREE"
